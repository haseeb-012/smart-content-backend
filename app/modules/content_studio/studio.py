"""Create module: hooks, idea generator, scripts (+ versions, expand), projects, export history.

Credit rules (PRD §4.4 / D8): generating a script = 1 credit, charged only after the AI succeeded, in
the same commit that stores it. Expanding a script, generating hooks and generating ideas are free
(rate-limited per user). Saving/editing/restoring never costs anything.
"""

import uuid
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import Conflict, NotFound
from app.models import (
    Channel,
    ExportRecord,
    Idea,
    Profile,
    Project,
    ProjectItem,
    SavedHook,
    Script,
    ScriptVersion,
    Video,
    VideoAnalysis,
)
from app.modules.discovery_analysis.analysis import get_workspace_video
from app.modules.discovery_analysis.llm import LLMProvider, ScriptBlocks
from app.modules.discovery_analysis.thumbnails import display_url
from app.modules.identity.service import get_workspace_for
from app.modules.plans_credits.service import consume, ensure_available

MAX_VERSIONS = 20
EMPTY_BLOCKS = {"hook": "", "intro": "", "main": "", "story": "", "cta": "", "ending": ""}


# ------------------------------------------------------------------ saved hooks


async def save_hook(
    db: AsyncSession, profile: Profile, ws_id: uuid.UUID, text: str, category: str | None, source: str
) -> tuple[SavedHook, bool]:
    await get_workspace_for(db, profile, ws_id, write=True)
    text = " ".join(text.split())
    existing = await db.scalar(
        select(SavedHook).where(SavedHook.workspace_id == ws_id, SavedHook.text == text)
    )
    if existing:
        return existing, False
    h = SavedHook(workspace_id=ws_id, text=text, category=category, source=source, created_by=profile.id)
    db.add(h)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        again = await db.scalar(
            select(SavedHook).where(SavedHook.workspace_id == ws_id, SavedHook.text == text)
        )
        if again is None:
            raise
        return again, False
    return h, True


# ------------------------------------------------------------------ ideas (generator)


async def generate_ideas(
    db: AsyncSession, profile: Profile, ws_id: uuid.UUID, llm: LLMProvider, topic: str, count: int
) -> list[Idea]:
    await get_workspace_for(db, profile, ws_id, write=True)
    # Context: this workspace's analyzed outliers (titles + formats), so ideas fit what works for them.
    rows = (
        await db.execute(
            select(Video.caption, VideoAnalysis.format)
            .join(VideoAnalysis, VideoAnalysis.video_id == Video.id)
            .where(VideoAnalysis.workspace_id == ws_id, VideoAnalysis.status == "complete")
            .order_by(VideoAnalysis.completed_at.desc())
            .limit(5)
        )
    ).all()
    context = "; ".join(f"{c[:80]} ({f[:80] if f else ''})" for c, f in rows) or None
    drafts = await llm.generate_ideas(topic, count, context)
    ideas = [
        Idea(
            workspace_id=ws_id,
            source="generated",
            title=d.title,
            positioning=d.positioning,
            format=d.format,
            contrast=d.contrast,
            personalization=d.personalization,
            created_by=profile.id,
        )
        for d in drafts
    ]
    db.add_all(ideas)
    await db.commit()
    return ideas


# ------------------------------------------------------------------ scripts


async def get_script(
    db: AsyncSession, profile: Profile, ws_id: uuid.UUID, script_id: uuid.UUID, *, write: bool = False
) -> Script:
    await get_workspace_for(db, profile, ws_id, write=write)
    s = await db.scalar(
        select(Script)
        .where(Script.id == script_id, Script.workspace_id == ws_id)
        .options(selectinload(Script.versions))
        .execution_options(populate_existing=True)
    )
    if s is None:
        raise NotFound("Script not found.")
    return s


async def list_scripts(db: AsyncSession, profile: Profile, ws_id: uuid.UUID) -> list[tuple[Script, int]]:
    await get_workspace_for(db, profile, ws_id)
    counts = (
        select(ScriptVersion.script_id, func.count().label("n")).group_by(ScriptVersion.script_id).subquery()
    )
    rows = await db.execute(
        select(Script, func.coalesce(counts.c.n, 0))
        .outerjoin(counts, counts.c.script_id == Script.id)
        .where(Script.workspace_id == ws_id)
        .order_by(Script.updated_at.desc())
    )
    return [(s, int(n)) for s, n in rows.all()]


async def _script_context(
    db: AsyncSession,
    profile: Profile,
    ws_id: uuid.UUID,
    video_id: uuid.UUID | None,
    idea_id: uuid.UUID | None,
) -> str | None:
    parts: list[str] = []
    if idea_id:
        idea = await db.get(Idea, idea_id)
        if idea is None or idea.workspace_id != ws_id:
            raise NotFound("Idea not found.")
        parts.append(f"Idea: {idea.title}. Format: {idea.format}. Contrast: {idea.contrast}.")
        video_id = video_id or idea.video_id
    if video_id:
        await get_workspace_video(db, profile, ws_id, video_id)
        a = await db.scalar(
            select(VideoAnalysis).where(
                VideoAnalysis.workspace_id == ws_id,
                VideoAnalysis.video_id == video_id,
                VideoAnalysis.status == "complete",
            )
        )
        if a:
            parts.append(f"Reference video: {a.summary} Hook: {a.hook} Format: {a.format}")
    return " ".join(parts) or None


async def create_script(
    db: AsyncSession,
    profile: Profile,
    ws_id: uuid.UUID,
    llm: LLMProvider,
    *,
    title: str,
    topic: str,
    hook: str | None,
    source_video_id: uuid.UUID | None,
    source_idea_id: uuid.UUID | None,
    request_key: str | None,
) -> tuple[Script, bool]:
    """Returns (script, created). 1 credit, charged in the same commit that stores the script."""
    await get_workspace_for(db, profile, ws_id, write=True)
    if request_key:
        prior = await db.scalar(
            select(Script).where(Script.workspace_id == ws_id, Script.request_key == request_key)
        )
        if prior:
            return prior, False
    await ensure_available(db, profile, 1)
    context = await _script_context(db, profile, ws_id, source_video_id, source_idea_id)
    blocks = (await llm.write_script(topic, hook, context)).model_dump()  # ProviderError → nothing charged
    s = Script(
        workspace_id=ws_id,
        title=title,
        topic=topic,
        blocks=blocks,
        source_video_id=source_video_id,
        source_idea_id=source_idea_id,
        request_key=request_key,
        created_by=profile.id,
    )
    db.add(s)
    await db.flush()
    db.add(ScriptVersion(script_id=s.id, title=title, blocks=blocks, note="generated"))
    try:
        _, charged = await consume(
            db, profile, idempotency_key=f"script:{s.id}", action="script_generation", workspace_id=ws_id
        )
        if not charged:
            await db.commit()
    except IntegrityError:  # same request_key raced us
        await db.rollback()
        prior = await db.scalar(
            select(Script).where(Script.workspace_id == ws_id, Script.request_key == request_key)
        )
        if prior is None:
            raise
        return prior, False
    return s, True


async def _add_version(db: AsyncSession, s: Script, note: str) -> None:
    db.add(ScriptVersion(script_id=s.id, title=s.title, blocks=dict(s.blocks), note=note))
    await db.flush()
    keep = (
        select(ScriptVersion.id)
        .where(ScriptVersion.script_id == s.id)
        .order_by(ScriptVersion.created_at.desc(), ScriptVersion.id)
        .limit(MAX_VERSIONS)
    )
    await db.execute(
        delete(ScriptVersion).where(ScriptVersion.script_id == s.id, ScriptVersion.id.not_in(keep))
    )


async def update_script(
    db: AsyncSession,
    profile: Profile,
    ws_id: uuid.UUID,
    script_id: uuid.UUID,
    title: str | None,
    blocks: dict[str, str] | None,
) -> Script:
    s = await get_script(db, profile, ws_id, script_id, write=True)
    changed = False
    if title is not None and title != s.title:
        s.title, changed = title, True
    if blocks is not None and blocks != s.blocks:
        s.blocks, changed = {**EMPTY_BLOCKS, **blocks}, True
    if changed:
        await _add_version(db, s, "edit")
        await db.commit()
    return await get_script(db, profile, ws_id, script_id)


async def expand_script(
    db: AsyncSession, profile: Profile, ws_id: uuid.UUID, script_id: uuid.UUID, llm: LLMProvider
) -> Script:
    """Free (PRD: expanding never costs a credit)."""
    s = await get_script(db, profile, ws_id, script_id, write=True)
    expanded = await llm.expand_script(ScriptBlocks.model_validate({**EMPTY_BLOCKS, **s.blocks}))
    s.blocks, s.expanded = expanded.model_dump(), True
    await _add_version(db, s, "expand")
    await db.commit()
    return await get_script(db, profile, ws_id, script_id)


async def restore_version(
    db: AsyncSession, profile: Profile, ws_id: uuid.UUID, script_id: uuid.UUID, version_id: uuid.UUID
) -> Script:
    s = await get_script(db, profile, ws_id, script_id, write=True)
    v = next((v for v in s.versions if v.id == version_id), None)
    if v is None:
        raise NotFound("Version not found.")
    s.title, s.blocks = v.title, dict(v.blocks)
    await _add_version(db, s, "restore")
    await db.commit()
    return await get_script(db, profile, ws_id, script_id)


def script_markdown(s: Script) -> str:
    b = {**EMPTY_BLOCKS, **s.blocks}
    labels = [
        ("HOOK", "hook"),
        ("INTRO", "intro"),
        ("MAIN", "main"),
        ("STORY", "story"),
        ("CTA", "cta"),
        ("ENDING", "ending"),
    ]
    return "\n".join([f"# {s.title}", ""] + [f"**{label}:** {b[k]}\n" for label, k in labels])


def script_text(s: Script) -> str:
    b = {**EMPTY_BLOCKS, **s.blocks}
    return "\n".join([s.title, ""] + [f"{k.upper()}: {b[k]}" for k in EMPTY_BLOCKS])


# ------------------------------------------------------------------ projects


async def get_project(
    db: AsyncSession, profile: Profile, ws_id: uuid.UUID, project_id: uuid.UUID, *, write: bool = False
) -> Project:
    await get_workspace_for(db, profile, ws_id, write=write)
    p = await db.scalar(
        select(Project)
        .where(Project.id == project_id, Project.workspace_id == ws_id)
        .options(selectinload(Project.items))
        .execution_options(populate_existing=True)
    )
    if p is None:
        raise NotFound("Project not found.")
    return p


async def list_projects(db: AsyncSession, profile: Profile, ws_id: uuid.UUID) -> list[Project]:
    await get_workspace_for(db, profile, ws_id)
    return list(
        await db.scalars(
            select(Project)
            .where(Project.workspace_id == ws_id)
            .options(selectinload(Project.items))
            .order_by(Project.created_at.desc())
        )
    )


async def _check_ref(
    db: AsyncSession, profile: Profile, ws_id: uuid.UUID, kind: str, ref_id: uuid.UUID
) -> None:
    if kind == "video":
        await get_workspace_video(db, profile, ws_id, ref_id)
        return
    model: Any = Idea if kind == "idea" else Script
    obj = await db.get(model, ref_id)
    if obj is None or obj.workspace_id != ws_id:
        raise NotFound(f"{kind.title()} not found.")


async def add_item(
    db: AsyncSession, profile: Profile, ws_id: uuid.UUID, project_id: uuid.UUID, kind: str, ref_id: uuid.UUID
) -> Project:
    """An item belongs to one project per workspace: adding it here moves it from any other."""
    await get_project(db, profile, ws_id, project_id, write=True)
    await _check_ref(db, profile, ws_id, kind, ref_id)
    existing = await db.scalar(
        select(ProjectItem).where(
            ProjectItem.workspace_id == ws_id, ProjectItem.kind == kind, ProjectItem.ref_id == ref_id
        )
    )
    if existing:
        existing.project_id = project_id
    else:
        db.add(ProjectItem(workspace_id=ws_id, project_id=project_id, kind=kind, ref_id=ref_id))
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise Conflict("That item was just added somewhere else. Refresh and try again.") from e
    return await get_project(db, profile, ws_id, project_id)


async def remove_item(
    db: AsyncSession, profile: Profile, ws_id: uuid.UUID, project_id: uuid.UUID, kind: str, ref_id: uuid.UUID
) -> Project:
    await get_project(db, profile, ws_id, project_id, write=True)
    await db.execute(
        delete(ProjectItem).where(
            ProjectItem.project_id == project_id, ProjectItem.kind == kind, ProjectItem.ref_id == ref_id
        )
    )
    await db.commit()
    return await get_project(db, profile, ws_id, project_id)


async def resolve_items(db: AsyncSession, items: list[ProjectItem]) -> list[dict[str, Any]]:
    """Turn item refs into display rows. Items whose target was deleted are skipped."""
    ids: dict[str, list[uuid.UUID]] = {"video": [], "idea": [], "script": []}
    for it in items:
        ids[it.kind].append(it.ref_id)
    videos = (
        {
            v.id: (v, c)
            for v, c in (
                await db.execute(
                    select(Video, Channel)
                    .join(Channel, Channel.id == Video.channel_id)
                    .where(Video.id.in_(ids["video"]))
                )
            ).all()
        }
        if ids["video"]
        else {}
    )
    ideas = (
        {i.id: i for i in await db.scalars(select(Idea).where(Idea.id.in_(ids["idea"])))}
        if ids["idea"]
        else {}
    )
    scripts = (
        {s.id: s for s in await db.scalars(select(Script).where(Script.id.in_(ids["script"])))}
        if ids["script"]
        else {}
    )
    out: list[dict[str, Any]] = []
    for it in items:
        base = {"kind": it.kind, "ref_id": it.ref_id, "added_at": it.created_at}
        if it.kind == "video" and it.ref_id in videos:
            v, c = videos[it.ref_id]
            out.append(
                base
                | {
                    "title": (v.caption.split("\n")[0][:140] or "Untitled video"),
                    "subtitle": f"@{c.handle} · {v.views:,} views",
                    "thumbnail_url": display_url(v),
                    "platform": v.platform,
                }
            )
        elif it.kind == "idea" and it.ref_id in ideas:
            i = ideas[it.ref_id]
            out.append(base | {"title": i.title, "subtitle": i.format})
        elif it.kind == "script" and it.ref_id in scripts:
            s = scripts[it.ref_id]
            out.append(base | {"title": s.title, "subtitle": (s.blocks.get("hook") or "")[:140]})
    return out


# ------------------------------------------------------------------ exports


async def log_export(
    db: AsyncSession, profile: Profile, ws_id: uuid.UUID, kind: str, title: str, fmt: str
) -> ExportRecord:
    await get_workspace_for(db, profile, ws_id)
    e = ExportRecord(workspace_id=ws_id, kind=kind, title=title[:200], format=fmt, created_by=profile.id)
    db.add(e)
    await db.commit()
    return e
