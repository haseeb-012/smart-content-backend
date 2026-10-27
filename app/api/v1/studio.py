"""Create module API: hook library + watchlist + generator, idea generator, scripts, projects, exports."""

import json
import re
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AdminActor, admin_actor, current_profile
from app.core.errors import Conflict, NotFound
from app.core.ratelimit import RateLimiter
from app.db.session import get_db
from app.models import AuditLog, ExportRecord, HookTemplate, Profile, Project, SavedHook, Script
from app.modules.content_studio import studio as svc
from app.modules.discovery_analysis.llm import LLMProvider, get_llm_provider
from app.modules.identity.service import get_workspace_for
from app.schemas import (
    BlocksIO,
    ExportIn,
    ExportOut,
    GeneratedHook,
    HookGenerateIn,
    HookIn,
    HookOut,
    IdeaGenerateIn,
    IdeaOut,
    ProjectIn,
    ProjectItemIn,
    ProjectItemOut,
    ProjectOut,
    ProjectPatch,
    SavedHookIn,
    SavedHookOut,
    ScriptCreate,
    ScriptOut,
    ScriptSummary,
    ScriptUpdate,
    ScriptVersionOut,
)

router = APIRouter(tags=["create"])
admin_router = APIRouter(prefix="/admin", tags=["admin"])
# Free AI actions (hooks, ideas, expand) — per-user caps so "free" can't run up the AI bill.
free_ai_limiter = RateLimiter(limit=60, window_seconds=3600)
script_limiter = RateLimiter(limit=10, window_seconds=60)
KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,80}$")


# ------------------------------------------------------------------ hook library (public, free)


@router.get("/hooks", response_model=list[HookOut])
async def hook_library(
    category: str | None = None,
    niche: str | None = None,
    q: str | None = Query(default=None, max_length=100),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(HookTemplate).where(HookTemplate.is_published.is_(True))
    if category:
        stmt = stmt.where(HookTemplate.category == category)
    if niche:
        stmt = stmt.where(HookTemplate.niche == niche)
    if q:
        stmt = stmt.where(HookTemplate.text.ilike(f"%{q.replace('%', '').replace('_', '')}%"))
    rows = await db.scalars(
        stmt.order_by(HookTemplate.views.desc().nulls_last(), HookTemplate.text).limit(500)
    )
    return [HookOut.model_validate(h) for h in rows]


@admin_router.post("/hooks", response_model=HookOut, status_code=status.HTTP_201_CREATED)
async def admin_add_hook(
    body: HookIn, admin: AdminActor = Depends(admin_actor), db: AsyncSession = Depends(get_db)
):
    h = HookTemplate(**body.model_dump())
    db.add(h)
    db.add(AuditLog(actor_id=admin.id, action="hooks.create", details={"text": body.text[:120]}))
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise Conflict("That hook is already in the library.", code="duplicate_hook") from e
    return HookOut.model_validate(h)


@admin_router.delete("/hooks/{hook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_hook(
    hook_id: uuid.UUID, admin: AdminActor = Depends(admin_actor), db: AsyncSession = Depends(get_db)
) -> Response:
    h = await db.get(HookTemplate, hook_id)
    if h is None:
        raise NotFound("Hook not found.")
    await db.delete(h)
    db.add(AuditLog(actor_id=admin.id, action="hooks.delete", details={"id": str(hook_id)}))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------ hook generator + watchlist


@router.post("/workspaces/{workspace_id}/hooks/generate", response_model=list[GeneratedHook])
async def generate_hooks(
    workspace_id: uuid.UUID,
    body: HookGenerateIn,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
    llm: LLMProvider = Depends(get_llm_provider),
):
    """Free. Results are not stored until the user saves one."""
    await get_workspace_for(db, profile, workspace_id)
    free_ai_limiter.check(str(profile.id))
    hooks = await llm.generate_hooks(body.topic, body.category, body.count)
    return [GeneratedHook(text=h.text, category=h.category) for h in hooks]


@router.get("/workspaces/{workspace_id}/saved-hooks", response_model=list[SavedHookOut])
async def saved_hooks(
    workspace_id: uuid.UUID, profile: Profile = Depends(current_profile), db: AsyncSession = Depends(get_db)
):
    await get_workspace_for(db, profile, workspace_id)
    rows = await db.scalars(
        select(SavedHook).where(SavedHook.workspace_id == workspace_id).order_by(SavedHook.created_at.desc())
    )
    return [SavedHookOut.model_validate(h) for h in rows]


@router.post("/workspaces/{workspace_id}/saved-hooks", response_model=SavedHookOut)
async def save_hook(
    workspace_id: uuid.UUID,
    body: SavedHookIn,
    response: Response,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
):
    h, created = await svc.save_hook(db, profile, workspace_id, body.text, body.category, body.source)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return SavedHookOut.model_validate(h)


@router.delete("/workspaces/{workspace_id}/saved-hooks/{hook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unsave_hook(
    workspace_id: uuid.UUID,
    hook_id: uuid.UUID,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await get_workspace_for(db, profile, workspace_id, write=True)
    h = await db.get(SavedHook, hook_id)
    if h is None or h.workspace_id != workspace_id:
        raise NotFound("Saved hook not found.")
    await db.delete(h)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------ idea generator


@router.post("/workspaces/{workspace_id}/ideas/generate", response_model=list[IdeaOut], status_code=201)
async def generate_ideas(
    workspace_id: uuid.UUID,
    body: IdeaGenerateIn,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
    llm: LLMProvider = Depends(get_llm_provider),
):
    """Free. Generated ideas are saved to the workspace (source=generated)."""
    free_ai_limiter.check(str(profile.id))
    ideas = await svc.generate_ideas(db, profile, workspace_id, llm, body.topic, body.count)
    return [IdeaOut.model_validate(i) for i in ideas]


# ------------------------------------------------------------------ scripts


def _summary(s: Script, n: int) -> ScriptSummary:
    return ScriptSummary(
        id=s.id,
        title=s.title,
        topic=s.topic,
        hook=(s.blocks or {}).get("hook", ""),
        expanded=s.expanded,
        version_count=n,
        source_video_id=s.source_video_id,
        source_idea_id=s.source_idea_id,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )


def _full(s: Script) -> ScriptOut:
    return ScriptOut(
        **_summary(s, len(s.versions)).model_dump(),
        blocks=BlocksIO(**{**svc.EMPTY_BLOCKS, **s.blocks}),
        versions=[ScriptVersionOut.model_validate(v) for v in s.versions],
    )


@router.get("/workspaces/{workspace_id}/scripts", response_model=list[ScriptSummary])
async def list_scripts(
    workspace_id: uuid.UUID, profile: Profile = Depends(current_profile), db: AsyncSession = Depends(get_db)
):
    return [_summary(s, n) for s, n in await svc.list_scripts(db, profile, workspace_id)]


@router.post("/workspaces/{workspace_id}/scripts", response_model=ScriptOut)
async def create_script(
    workspace_id: uuid.UUID,
    body: ScriptCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
    llm: LLMProvider = Depends(get_llm_provider),
):
    """1 credit, charged only if the AI succeeds. Send `Idempotency-Key` so retries never charge twice."""
    script_limiter.check(str(profile.id))
    key = idempotency_key if idempotency_key and KEY_RE.match(idempotency_key) else None
    s, created = await svc.create_script(
        db,
        profile,
        workspace_id,
        llm,
        title=body.title,
        topic=body.topic,
        hook=body.hook,
        source_video_id=body.source_video_id,
        source_idea_id=body.source_idea_id,
        request_key=key,
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return _full(await svc.get_script(db, profile, workspace_id, s.id))


@router.get("/workspaces/{workspace_id}/scripts/{script_id}", response_model=ScriptOut)
async def get_script(
    workspace_id: uuid.UUID,
    script_id: uuid.UUID,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
):
    return _full(await svc.get_script(db, profile, workspace_id, script_id))


@router.patch("/workspaces/{workspace_id}/scripts/{script_id}", response_model=ScriptOut)
async def update_script(
    workspace_id: uuid.UUID,
    script_id: uuid.UUID,
    body: ScriptUpdate,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
):
    """Saving creates a new version (last 20 kept). Free."""
    blocks = body.blocks.model_dump() if body.blocks else None
    return _full(await svc.update_script(db, profile, workspace_id, script_id, body.title, blocks))


@router.post("/workspaces/{workspace_id}/scripts/{script_id}/expand", response_model=ScriptOut)
async def expand_script(
    workspace_id: uuid.UUID,
    script_id: uuid.UUID,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
    llm: LLMProvider = Depends(get_llm_provider),
):
    """Free (no credit)."""
    free_ai_limiter.check(str(profile.id))
    return _full(await svc.expand_script(db, profile, workspace_id, script_id, llm))


@router.post(
    "/workspaces/{workspace_id}/scripts/{script_id}/versions/{version_id}/restore", response_model=ScriptOut
)
async def restore_version(
    workspace_id: uuid.UUID,
    script_id: uuid.UUID,
    version_id: uuid.UUID,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
):
    return _full(await svc.restore_version(db, profile, workspace_id, script_id, version_id))


@router.delete("/workspaces/{workspace_id}/scripts/{script_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_script(
    workspace_id: uuid.UUID,
    script_id: uuid.UUID,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """The credit used to generate it isn't refunded."""
    await db.delete(await svc.get_script(db, profile, workspace_id, script_id, write=True))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/workspaces/{workspace_id}/scripts/{script_id}/export")
async def export_script(
    workspace_id: uuid.UUID,
    script_id: uuid.UUID,
    format: Literal["markdown", "json", "text"] = "markdown",
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
) -> Response:
    s = await svc.get_script(db, profile, workspace_id, script_id)
    slug = re.sub(r"[^a-z0-9]+", "-", s.title.lower()).strip("-")[:60] or "script"
    if format == "json":
        body, mime, ext = (
            json.dumps({"title": s.title, **{**svc.EMPTY_BLOCKS, **s.blocks}}, indent=2),
            "application/json",
            "json",
        )
    elif format == "text":
        body, mime, ext = svc.script_text(s), "text/plain; charset=utf-8", "txt"
    else:
        body, mime, ext = svc.script_markdown(s), "text/markdown; charset=utf-8", "md"
    await svc.log_export(db, profile, workspace_id, "Script", s.title, format.title())
    return Response(
        body, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{slug}.{ext}"'}
    )


# ------------------------------------------------------------------ projects


async def _project_out(db: AsyncSession, p: Project) -> ProjectOut:
    return ProjectOut(
        id=p.id,
        name=p.name,
        description=p.description,
        created_at=p.created_at,
        items=[ProjectItemOut(**r) for r in await svc.resolve_items(db, list(p.items))],
    )


@router.get("/workspaces/{workspace_id}/projects", response_model=list[ProjectOut])
async def list_projects(
    workspace_id: uuid.UUID, profile: Profile = Depends(current_profile), db: AsyncSession = Depends(get_db)
):
    return [await _project_out(db, p) for p in await svc.list_projects(db, profile, workspace_id)]


@router.post(
    "/workspaces/{workspace_id}/projects", response_model=ProjectOut, status_code=status.HTTP_201_CREATED
)
async def create_project(
    workspace_id: uuid.UUID,
    body: ProjectIn,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
):
    await get_workspace_for(db, profile, workspace_id, write=True)
    p = Project(
        workspace_id=workspace_id, name=body.name, description=body.description, created_by=profile.id
    )
    db.add(p)
    await db.commit()
    return await _project_out(db, await svc.get_project(db, profile, workspace_id, p.id))


@router.get("/workspaces/{workspace_id}/projects/{project_id}", response_model=ProjectOut)
async def get_project(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
):
    return await _project_out(db, await svc.get_project(db, profile, workspace_id, project_id))


@router.patch("/workspaces/{workspace_id}/projects/{project_id}", response_model=ProjectOut)
async def update_project(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    body: ProjectPatch,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
):
    p = await svc.get_project(db, profile, workspace_id, project_id, write=True)
    if body.name is not None:
        p.name = body.name
    if body.description is not None:
        p.description = body.description
    await db.commit()
    return await _project_out(db, await svc.get_project(db, profile, workspace_id, project_id))


@router.delete("/workspaces/{workspace_id}/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Deletes the folder only — videos, ideas and scripts inside stay in the workspace."""
    await db.delete(await svc.get_project(db, profile, workspace_id, project_id, write=True))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/workspaces/{workspace_id}/projects/{project_id}/items", response_model=ProjectOut)
async def add_project_item(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    body: ProjectItemIn,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
):
    p = await svc.add_item(db, profile, workspace_id, project_id, body.kind, body.ref_id)
    return await _project_out(db, p)


@router.delete(
    "/workspaces/{workspace_id}/projects/{project_id}/items/{kind}/{ref_id}", response_model=ProjectOut
)
async def remove_project_item(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    kind: Literal["video", "idea", "script"],
    ref_id: uuid.UUID,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
):
    p = await svc.remove_item(db, profile, workspace_id, project_id, kind, ref_id)
    return await _project_out(db, p)


# ------------------------------------------------------------------ exports


@router.get("/workspaces/{workspace_id}/exports", response_model=list[ExportOut])
async def list_exports(
    workspace_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
):
    await get_workspace_for(db, profile, workspace_id)
    rows = await db.scalars(
        select(ExportRecord)
        .where(ExportRecord.workspace_id == workspace_id)
        .order_by(ExportRecord.created_at.desc())
        .limit(limit)
    )
    return [ExportOut.model_validate(e) for e in rows]


@router.post(
    "/workspaces/{workspace_id}/exports", response_model=ExportOut, status_code=status.HTTP_201_CREATED
)
async def log_export(
    workspace_id: uuid.UUID,
    body: ExportIn,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
):
    """Record a client-side export (CSV, copy-for-LLM, transcript download) in the history."""
    return ExportOut.model_validate(
        await svc.log_export(db, profile, workspace_id, body.kind, body.title, body.format)
    )
