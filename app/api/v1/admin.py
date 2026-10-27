"""Admin panel API (dashboard /admin). Every change is audited and takes effect on the user's next
request — no redeploy. Access: the panel login defined in env (ADMIN_EMAIL…), or a Supabase user
with is_admin."""

import time
import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1._common import credits_out
from app.api.v1.channels import get_session_factory
from app.core import admin_auth
from app.core.config import get_settings
from app.core.deps import AdminActor, admin_actor
from app.core.errors import AppError, Conflict, NotFound, RateLimited, Unauthorized
from app.core.ratelimit import RateLimiter, client_key
from app.db.session import get_db
from app.models import AuditLog, Channel, HookTemplate, Profile, WaitlistEntry
from app.modules.admin import ops
from app.modules.discovery_analysis import channels as channel_svc
from app.modules.discovery_analysis.providers import ChannelProvider, get_channel_provider
from app.modules.plans_credits import service as credits
from app.schemas import (
    AdminCreditsIn,
    AdminFlagsIn,
    AdminPlanIn,
    AdminUserOut,
    HookCategory,
    MeOut,
    WaitlistEntryOut,
)

router = APIRouter(prefix="/admin", tags=["admin"])

# Panel login: 5 tries / 15 min per IP, and at most 30 failures / hour in total (slows guessing
# from many IPs). Successful logins don't count towards the global budget.
login_ip_limiter = RateLimiter(limit=5, window_seconds=900)
login_fail_limiter = RateLimiter(limit=30, window_seconds=3600)


class AdminLoginDisabled(AppError):
    status_code = 404
    code = "admin_login_disabled"


# ------------------------------------------------------------------------------------- session


class AdminLoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=200)


class AdminSessionOut(BaseModel):
    token: str | None = None
    expires_at: datetime | None = None
    email: str | None
    source: Literal["panel", "supabase"]


@router.post("/login", response_model=AdminSessionOut)
async def login(body: AdminLoginIn, request: Request) -> AdminSessionOut:
    settings = get_settings()
    if not settings.admin_login_enabled:
        raise AdminLoginDisabled("Admin panel login isn't configured on the server.")
    login_ip_limiter.check(client_key(request))
    if login_fail_limiter.exhausted("global"):
        raise RateLimited(
            "Too many failed admin logins. Try again in an hour.", details={"retry_after": 3600}
        )
    if not admin_auth.check_login(settings, body.email, body.password):
        login_fail_limiter.hits["global"].append(time.monotonic())
        raise Unauthorized("Wrong email or password.", code="invalid_credentials")
    token, exp = admin_auth.issue_token(settings)
    return AdminSessionOut(
        token=token, expires_at=exp, email=settings.admin_email.strip().lower(), source="panel"
    )


@router.get("/session", response_model=AdminSessionOut)
async def session(actor: AdminActor = Depends(admin_actor)) -> AdminSessionOut:
    return AdminSessionOut(email=actor.email, source=actor.source)


# ------------------------------------------------------------------------------------ overview


@router.get("/stats")
async def stats(_: AdminActor = Depends(admin_actor), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await ops.overview(db)


@router.get("/system")
async def system(_: AdminActor = Depends(admin_actor), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await ops.system(db, get_settings())


@router.get("/billing")
async def billing(_: AdminActor = Depends(admin_actor), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await ops.billing(db, get_settings())


# --------------------------------------------------------------------------------------- users


async def _target(db: AsyncSession, user_id: uuid.UUID) -> Profile:
    p = await db.get(Profile, user_id)
    if not p:
        raise NotFound("User not found.")
    return p


async def _user_out(db: AsyncSession, p: Profile) -> AdminUserOut:
    return AdminUserOut(
        profile=MeOut.model_validate(p), credits=credits_out(await credits.get_balance(db, p))
    )


@router.get("/users")
async def list_users(
    q: str | None = Query(None, max_length=100),
    plan: Literal["pro", "visionary", "titan"] | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: AdminActor = Depends(admin_actor),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    rows, total = await ops.list_users(db, q=q, plan=plan, limit=limit, offset=offset)
    items = []
    for r in rows:
        p: Profile = r["profile"]
        items.append(
            {
                "profile": MeOut.model_validate(p).model_dump(mode="json"),
                "credits": credits_out(await credits.get_balance(db, p)).model_dump(mode="json"),
                "subscription_status": p.subscription_status,
                "workspaces": r["workspaces"],
                "analyses": r["analyses"],
                "last_credit_use": r["last_credit_use"],
            }
        )
    return {"items": items, "total": total}


@router.get("/users/{user_id}", response_model=AdminUserOut)
async def get_user(
    user_id: uuid.UUID, _: AdminActor = Depends(admin_actor), db: AsyncSession = Depends(get_db)
):
    return await _user_out(db, await _target(db, user_id))


@router.get("/users/{user_id}/usage")
async def user_usage(
    user_id: uuid.UUID, _: AdminActor = Depends(admin_actor), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    return await ops.user_usage(db, await _target(db, user_id))


@router.patch("/users/{user_id}/plan", response_model=AdminUserOut)
async def set_plan(
    user_id: uuid.UUID,
    body: AdminPlanIn,
    actor: AdminActor = Depends(admin_actor),
    db: AsyncSession = Depends(get_db),
):
    target = await _target(db, user_id)
    old = target.plan
    target.plan = body.plan
    db.add(
        AuditLog(
            actor_id=actor.id,
            target_user_id=target.id,
            action="plan.set",
            details={"from": old, "to": body.plan},
        )
    )
    await db.commit()
    return await _user_out(db, target)


@router.post("/users/{user_id}/credits", response_model=AdminUserOut)
async def adjust_credits(
    user_id: uuid.UUID,
    body: AdminCreditsIn,
    actor: AdminActor = Depends(admin_actor),
    db: AsyncSession = Depends(get_db),
):
    target = await _target(db, user_id)
    await credits.adjust(db, actor=actor, target=target, amount=body.amount, reason=body.reason)
    return await _user_out(db, target)


@router.patch("/users/{user_id}/flags", response_model=AdminUserOut)
async def set_flags(
    user_id: uuid.UUID,
    body: AdminFlagsIn,
    actor: AdminActor = Depends(admin_actor),
    db: AsyncSession = Depends(get_db),
):
    target = await _target(db, user_id)
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(target, k, v)
    if changes:
        db.add(AuditLog(actor_id=actor.id, target_user_id=target.id, action="flags.set", details=changes))
    await db.commit()
    return await _user_out(db, target)


# ------------------------------------------------------------------------------------ channels


@router.get("/channels")
async def list_channels(
    status: Literal["pending", "ok", "error"] | None = None,
    platform: Literal["tiktok", "instagram", "youtube"] | None = None,
    q: str | None = Query(None, max_length=100),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: AdminActor = Depends(admin_actor),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    rows, total = await ops.list_channels(
        db, status=status, platform=platform, q=q, limit=limit, offset=offset
    )
    return {
        "items": [
            {
                "id": c.id,
                "platform": c.platform,
                "handle": c.handle,
                "display_name": c.display_name,
                "avatar_url": c.avatar_url,
                "followers": c.followers,
                "video_count": c.video_count,
                "sync_status": c.sync_status,
                "sync_error": c.sync_error,
                "last_synced_at": c.last_synced_at,
                "workspaces": r["workspaces"],
            }
            for r in rows
            for c in [r["channel"]]
        ],
        "total": total,
    }


@router.post("/channels/{channel_id}/resync", status_code=202)
async def resync_channel(
    channel_id: uuid.UUID,
    background: BackgroundTasks,
    actor: AdminActor = Depends(admin_actor),
    db: AsyncSession = Depends(get_db),
    provider: ChannelProvider = Depends(get_channel_provider),
    session_factory: channel_svc.SessionFactory = Depends(get_session_factory),
) -> dict[str, bool]:
    ch = await db.get(Channel, channel_id)
    if ch is None:
        raise NotFound("Channel not found.")
    db.add(
        AuditLog(
            actor_id=actor.id, action="channel.resync", details={"channel": f"{ch.platform}:{ch.handle}"}
        )
    )
    await db.commit()
    background.add_task(channel_svc.sync_channel, session_factory, provider, channel_id)
    return {"queued": True}


# ------------------------------------------------------------------------------ analyses, videos


@router.get("/analyses")
async def list_analyses(
    status: Literal["queued", "processing", "complete", "failed"] | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: AdminActor = Depends(admin_actor),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    items, total = await ops.list_analyses(db, status=status, limit=limit, offset=offset)
    return {"items": items, "total": total}


@router.get("/videos")
async def search_videos(
    q: str | None = Query(None, max_length=100),
    platform: Literal["tiktok", "instagram", "youtube"] | None = None,
    min_outlier: float | None = Query(None, ge=0),
    limit: int = Query(40, ge=1, le=100),
    _: AdminActor = Depends(admin_actor),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    return await ops.search_videos(db, q=q, platform=platform, min_outlier=min_outlier, limit=limit)


# --------------------------------------------------------------------------------------- hooks


class AdminHookOut(BaseModel):
    id: uuid.UUID
    text: str
    category: str
    niche: str
    views: int | None
    source_url: str | None
    is_published: bool
    created_at: datetime


class AdminHookPatch(BaseModel):
    text: str | None = Field(default=None, min_length=3, max_length=300)
    category: HookCategory | None = None
    niche: str | None = Field(default=None, min_length=1, max_length=60)
    views: int | None = Field(default=None, ge=0)
    source_url: str | None = Field(default=None, max_length=500)
    is_published: bool | None = None


def _hook_out(h: HookTemplate) -> AdminHookOut:
    return AdminHookOut(
        id=h.id,
        text=h.text,
        category=h.category,
        niche=h.niche,
        views=h.views,
        source_url=h.source_url,
        is_published=h.is_published,
        created_at=h.created_at,
    )


@router.get("/hooks", response_model=list[AdminHookOut])
async def list_hooks(_: AdminActor = Depends(admin_actor), db: AsyncSession = Depends(get_db)):
    rows = await db.scalars(select(HookTemplate).order_by(HookTemplate.created_at.desc()))
    return [_hook_out(h) for h in rows]


@router.patch("/hooks/{hook_id}", response_model=AdminHookOut)
async def update_hook(
    hook_id: uuid.UUID,
    body: AdminHookPatch,
    actor: AdminActor = Depends(admin_actor),
    db: AsyncSession = Depends(get_db),
):
    h = await db.get(HookTemplate, hook_id)
    if h is None:
        raise NotFound("Hook not found.")
    changes = body.model_dump(exclude_unset=True)
    for k, v in changes.items():
        setattr(h, k, v)
    db.add(
        AuditLog(
            actor_id=actor.id,
            action="hooks.update",
            details={"id": str(hook_id), **{k: str(v)[:80] for k, v in changes.items()}},
        )
    )
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise Conflict("That hook is already in the library.", code="duplicate_hook") from e
    return _hook_out(h)


# ------------------------------------------------------------------------------- audit, waitlist


@router.get("/audit")
async def audit_log(
    limit: int = Query(100, ge=1, le=500),
    action: str | None = Query(None, max_length=64),
    _: AdminActor = Depends(admin_actor),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    return await ops.audit(db, limit=limit, action=action)


@router.get("/waitlist", response_model=list[WaitlistEntryOut])
async def waitlist(_: AdminActor = Depends(admin_actor), db: AsyncSession = Depends(get_db)):
    rows = await db.scalars(select(WaitlistEntry).order_by(WaitlistEntry.created_at.desc()))
    return [WaitlistEntryOut.model_validate(r) for r in rows]
