"""Read-side queries for the admin panel: overview numbers, user/channel/analysis lists, billing and
system status. Writes (plan, credits, flags, collections, hooks) live with their own modules."""

import time
import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import (
    AuditLog,
    Channel,
    Collection,
    CreditLedger,
    HookTemplate,
    Idea,
    Profile,
    Script,
    SocialAccount,
    StripeEvent,
    Video,
    VideoAnalysis,
    WaitlistEntry,
    Workspace,
    WorkspaceChannel,
)
from app.modules.discovery_analysis.thumbnails import display_url


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


async def _count(db: AsyncSession, stmt: Any) -> int:
    return int(await db.scalar(stmt) or 0)


def _by_day(stamps: Sequence[datetime | None], days: int) -> list[dict[str, Any]]:
    today = datetime.now(UTC).date()
    counts = Counter(_aware(s).date() for s in stamps if s)  # type: ignore[union-attr]
    return [
        {"date": (today - timedelta(days=i)).isoformat(), "value": counts.get(today - timedelta(days=i), 0)}
        for i in range(days - 1, -1, -1)
    ]


def _like(q: str) -> str:
    return f"%{q.replace('%', '').replace('_', '').strip()}%"


# ---------------------------------------------------------------------------------------- overview


async def overview(db: AsyncSession) -> dict[str, Any]:
    now = datetime.now(UTC)
    week = now - timedelta(days=7)
    month = now - timedelta(days=30)

    plans = dict((await db.execute(select(Profile.plan, func.count()).group_by(Profile.plan))).all())
    subs = dict(
        (
            await db.execute(
                select(Profile.subscription_status, func.count())
                .where(Profile.subscription_status.is_not(None))
                .group_by(Profile.subscription_status)
            )
        ).all()
    )
    sync = dict(
        (await db.execute(select(Channel.sync_status, func.count()).group_by(Channel.sync_status))).all()
    )
    analyses = dict(
        (await db.execute(select(VideoAnalysis.status, func.count()).group_by(VideoAnalysis.status))).all()
    )
    signups = list(await db.scalars(select(Profile.created_at).where(Profile.created_at >= month)))
    analysis_days = list(
        await db.scalars(select(VideoAnalysis.created_at).where(VideoAnalysis.created_at >= month))
    )
    credits_used_30d = -int(
        await db.scalar(
            select(func.coalesce(func.sum(CreditLedger.amount), 0)).where(
                CreditLedger.kind == "usage", CreditLedger.created_at >= month
            )
        )
        or 0
    )

    return {
        "users": {
            "total": sum(plans.values()),
            "new_7d": await _count(
                db, select(func.count()).select_from(Profile).where(Profile.created_at >= week)
            ),
            "by_plan": {p: plans.get(p, 0) for p in ("pro", "visionary", "titan")},
            "admins": await _count(
                db, select(func.count()).select_from(Profile).where(Profile.is_admin.is_(True))
            ),
            "mcp_enabled": await _count(
                db, select(func.count()).select_from(Profile).where(Profile.mcp_enabled.is_(True))
            ),
            "subscriptions": subs,
        },
        "content": {
            "workspaces": await _count(db, select(func.count()).select_from(Workspace)),
            "channels": sum(sync.values()),
            "channels_by_status": sync,
            "videos": await _count(db, select(func.count()).select_from(Video)),
            "analyses": sum(analyses.values()),
            "analyses_by_status": analyses,
            "scripts": await _count(db, select(func.count()).select_from(Script)),
            "ideas": await _count(db, select(func.count()).select_from(Idea)),
        },
        "library": {
            "collections": await _count(db, select(func.count()).select_from(Collection)),
            "collections_published": await _count(
                db, select(func.count()).select_from(Collection).where(Collection.is_published.is_(True))
            ),
            "hooks": await _count(db, select(func.count()).select_from(HookTemplate)),
        },
        "waitlist": await _count(db, select(func.count()).select_from(WaitlistEntry)),
        "credits_used_30d": credits_used_30d,
        "signups_by_day": _by_day(signups, 30),
        "analyses_by_day": _by_day(analysis_days, 30),
    }


# ------------------------------------------------------------------------------------------- users


async def list_users(
    db: AsyncSession, *, q: str | None, plan: str | None, limit: int, offset: int
) -> tuple[list[dict[str, Any]], int]:
    stmt = select(Profile)
    if q:
        stmt = stmt.where(or_(Profile.email.ilike(_like(q)), Profile.full_name.ilike(_like(q))))
    if plan:
        stmt = stmt.where(Profile.plan == plan)
    total = await _count(db, select(func.count()).select_from(stmt.subquery()))
    ws_count = (
        select(func.count(Workspace.id))
        .where(Workspace.owner_id == Profile.id)
        .correlate(Profile)
        .scalar_subquery()
    )
    an_count = (
        select(func.count(VideoAnalysis.id))
        .where(VideoAnalysis.requested_by == Profile.id)
        .correlate(Profile)
        .scalar_subquery()
    )
    last_seen = (
        select(func.max(CreditLedger.created_at))
        .where(CreditLedger.user_id == Profile.id)
        .correlate(Profile)
        .scalar_subquery()
    )
    rows = (
        await db.execute(
            stmt.add_columns(ws_count, an_count, last_seen)
            .order_by(Profile.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [
        {"profile": p, "workspaces": w or 0, "analyses": a or 0, "last_credit_use": last}
        for p, w, a, last in rows
    ], total


async def user_usage(db: AsyncSession, p: Profile) -> dict[str, Any]:
    workspaces = list(
        await db.scalars(select(Workspace).where(Workspace.owner_id == p.id).order_by(Workspace.created_at))
    )
    ws_ids = [w.id for w in workspaces] or [uuid.uuid4()]
    ledger = list(
        await db.scalars(
            select(CreditLedger)
            .where(CreditLedger.user_id == p.id)
            .order_by(CreditLedger.created_at.desc())
            .limit(25)
        )
    )
    socials = list(await db.scalars(select(SocialAccount).where(SocialAccount.user_id == p.id)))
    return {
        "workspaces": [
            {"id": w.id, "name": w.name, "is_active": w.is_active, "created_at": w.created_at}
            for w in workspaces
        ],
        "channels": await _count(
            db,
            select(func.count())
            .select_from(WorkspaceChannel)
            .where(WorkspaceChannel.workspace_id.in_(ws_ids)),
        ),
        "analyses": await _count(
            db, select(func.count()).select_from(VideoAnalysis).where(VideoAnalysis.requested_by == p.id)
        ),
        "scripts": await _count(
            db, select(func.count()).select_from(Script).where(Script.workspace_id.in_(ws_ids))
        ),
        "ideas": await _count(
            db, select(func.count()).select_from(Idea).where(Idea.workspace_id.in_(ws_ids))
        ),
        "ledger": [
            {"kind": e.kind, "amount": e.amount, "action": e.action, "created_at": e.created_at}
            for e in ledger
        ],
        "social_accounts": [
            {"platform": s.platform, "handle": s.handle, "status": s.status, "verified_at": s.verified_at}
            for s in socials
        ],
        "billing": {
            "stripe_customer": bool(p.stripe_customer_id),
            "subscription_status": p.subscription_status,
            "billing_cycle": p.billing_cycle,
        },
    }


# ---------------------------------------------------------------------------------------- channels


async def list_channels(
    db: AsyncSession, *, status: str | None, platform: str | None, q: str | None, limit: int, offset: int
) -> tuple[list[dict[str, Any]], int]:
    stmt = select(Channel)
    if status:
        stmt = stmt.where(Channel.sync_status == status)
    if platform:
        stmt = stmt.where(Channel.platform == platform)
    if q:
        stmt = stmt.where(or_(Channel.handle.ilike(_like(q)), Channel.display_name.ilike(_like(q))))
    total = await _count(db, select(func.count()).select_from(stmt.subquery()))
    trackers = (
        select(func.count(WorkspaceChannel.id))
        .where(WorkspaceChannel.channel_id == Channel.id)
        .correlate(Channel)
        .scalar_subquery()
    )
    rows = (
        await db.execute(
            stmt.add_columns(trackers)
            .order_by(Channel.last_synced_at.desc().nulls_last(), Channel.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [{"channel": c, "workspaces": t or 0} for c, t in rows], total


# --------------------------------------------------------------------------------------- analyses


async def list_analyses(
    db: AsyncSession, *, status: str | None, limit: int, offset: int
) -> tuple[list[dict[str, Any]], int]:
    stmt = (
        select(VideoAnalysis, Video, Channel, Profile.email)
        .join(Video, Video.id == VideoAnalysis.video_id)
        .join(Channel, Channel.id == Video.channel_id)
        .outerjoin(Profile, Profile.id == VideoAnalysis.requested_by)
    )
    if status:
        stmt = stmt.where(VideoAnalysis.status == status)
    count = select(func.count()).select_from(VideoAnalysis)
    total = await _count(db, count.where(VideoAnalysis.status == status) if status else count)
    rows = (
        await db.execute(stmt.order_by(VideoAnalysis.created_at.desc()).limit(limit).offset(offset))
    ).all()
    return [
        {
            "id": a.id,
            "status": a.status,
            "error": a.error,
            "model": a.model,
            "summary": a.summary,
            "created_at": a.created_at,
            "completed_at": a.completed_at,
            "user_email": email,
            "video": {"id": v.id, "url": v.url, "platform": v.platform, "caption": v.caption[:140]},
            "channel": {"handle": c.handle, "platform": c.platform},
        }
        for a, v, c, email in rows
    ], total


# ----------------------------------------------------------------------------------------- videos


async def search_videos(
    db: AsyncSession, *, q: str | None, platform: str | None, min_outlier: float | None, limit: int
) -> list[dict[str, Any]]:
    """Best performers across every tracked channel — the source for collection examples."""
    stmt = select(Video, Channel).join(Channel, Channel.id == Video.channel_id)
    if platform:
        stmt = stmt.where(Video.platform == platform)
    if min_outlier:
        stmt = stmt.where(Video.outlier_score >= min_outlier)
    if q:
        stmt = stmt.where(
            or_(
                Video.caption.ilike(_like(q)),
                Channel.handle.ilike(_like(q)),
                Channel.display_name.ilike(_like(q)),
            )
        )
    rows = (
        await db.execute(
            stmt.order_by(Video.outlier_score.desc().nulls_last(), Video.views.desc()).limit(limit)
        )
    ).all()
    ids = [v.id for v, _ in rows] or [uuid.uuid4()]
    done: dict[uuid.UUID, VideoAnalysis] = {}
    for a in await db.scalars(
        select(VideoAnalysis).where(VideoAnalysis.video_id.in_(ids), VideoAnalysis.status == "complete")
    ):
        done.setdefault(a.video_id, a)
    out = []
    for v, c in rows:
        found = done.get(v.id)
        out.append(
            {
                "id": v.id,
                "platform": v.platform,
                "url": v.url,
                "caption": v.caption,
                "thumbnail_url": display_url(v),
                "views": v.views,
                "outlier_score": v.outlier_score,
                "posted_at": v.posted_at,
                "creator_handle": c.handle,
                "creator_name": c.display_name,
                "analysis": None
                if found is None
                else {
                    "summary": found.summary,
                    "hook": found.hook,
                    "format": found.format,
                    "transcript": "\n".join(
                        f"[{t.get('t', '')}] {t.get('text', '')}" for t in found.transcript or []
                    ),
                },
            }
        )
    return out


# ---------------------------------------------------------------------------------------- billing


async def billing(db: AsyncSession, settings: Settings) -> dict[str, Any]:
    subs = list(
        await db.scalars(
            select(Profile)
            .where(or_(Profile.stripe_subscription_id.is_not(None), Profile.subscription_status.is_not(None)))
            .order_by(Profile.updated_at.desc())
            .limit(200)
        )
    )
    events = list(await db.scalars(select(StripeEvent).order_by(StripeEvent.created_at.desc()).limit(30)))
    return {
        "provider": settings.billing_provider,
        "test_mode": True,
        "prices_configured": len(settings.stripe_prices),
        "webhook_configured": settings.stripe_webhook_secret.startswith("whsec_"),
        "webhook_url": f"{settings.public_api_url.rstrip('/')}/api/v1/billing/webhook",
        "subscriptions": [
            {
                "user_id": p.id,
                "email": p.email,
                "plan": p.plan,
                "status": p.subscription_status,
                "cycle": p.billing_cycle,
                "updated_at": p.updated_at,
            }
            for p in subs
        ],
        "events": [{"id": e.id, "type": e.type, "created_at": e.created_at} for e in events],
    }


# ----------------------------------------------------------------------------------------- system

_apify_cache: dict[str, Any] = {"at": 0.0, "data": None}


async def _apify_usage(settings: Settings) -> dict[str, Any] | None:
    """Apify spend this billing cycle (free metadata call, cached 5 min). Never returns the token."""
    if not settings.apify_token:
        return None
    if _apify_cache["data"] is not None and time.monotonic() - _apify_cache["at"] < 300:
        return _apify_cache["data"]  # type: ignore[no-any-return]
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                "https://api.apify.com/v2/users/me/limits",
                headers={"Authorization": f"Bearer {settings.apify_token}"},
            )
        if r.status_code != 200:
            return {"error": f"Apify answered {r.status_code}"}
        d = r.json().get("data", {})
        used = (d.get("current") or {}).get("monthlyUsageUsd")
        limit = (d.get("limits") or {}).get("maxMonthlyUsageUsd")
        data = {
            "used_usd": round(used, 2) if isinstance(used, (int, float)) else None,
            "limit_usd": limit,
            "remaining_usd": round(limit - used, 2)
            if isinstance(used, (int, float)) and isinstance(limit, (int, float))
            else None,
            "cycle_ends": (d.get("monthlyUsageCycle") or {}).get("endAt"),
        }
    except (httpx.HTTPError, ValueError):
        return {"error": "Couldn't reach Apify."}
    _apify_cache.update(at=time.monotonic(), data=data)
    return data


async def system(db: AsyncSession, settings: Settings) -> dict[str, Any]:
    try:
        await db.execute(select(1))
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    failed_syncs = await _count(
        db, select(func.count()).select_from(Channel).where(Channel.sync_status == "error")
    )
    stuck = await _count(
        db,
        select(func.count())
        .select_from(VideoAnalysis)
        .where(
            VideoAnalysis.status.in_(("queued", "processing")),
            VideoAnalysis.created_at < datetime.now(UTC) - timedelta(minutes=15),
        ),
    )
    return {
        "env": settings.env,
        "version": settings.version,
        "database": "ok" if db_ok else "error",
        "providers": {
            "llm": settings.llm_provider,
            "llm_model": settings.gemini_model if settings.llm_provider == "gemini" else "demo",
            "channels": settings.channel_provider,
            "billing": settings.billing_provider,
        },
        "keys": {
            "gemini": bool(settings.gemini_api_key),
            "apify": bool(settings.apify_token),
            "stripe": bool(settings.stripe_secret_key),
            "stripe_webhook": settings.stripe_webhook_secret.startswith("whsec_"),
            "supabase": bool(settings.supabase_url),
        },
        "apify_usage": await _apify_usage(settings) if settings.channel_provider == "apify" else None,
        "limits": {
            "analysis_daily_limit": settings.analysis_daily_limit,
            "videos_per_channel": settings.ingest_videos_per_channel,
            "admin_session_hours": settings.admin_session_hours,
        },
        "urls": {"api": settings.public_api_url, "dashboard": settings.dashboard_url},
        "health": {"failed_channel_syncs": failed_syncs, "stuck_analyses": stuck},
    }


async def audit(db: AsyncSession, *, limit: int, action: str | None) -> list[dict[str, Any]]:
    actor = select(Profile.email).where(Profile.id == AuditLog.actor_id).correlate(AuditLog).scalar_subquery()
    target = (
        select(Profile.email)
        .where(Profile.id == AuditLog.target_user_id)
        .correlate(AuditLog)
        .scalar_subquery()
    )
    stmt = select(AuditLog, actor, target)
    if action:
        stmt = stmt.where(AuditLog.action.ilike(_like(action)))
    rows = (await db.execute(stmt.order_by(AuditLog.created_at.desc()).limit(limit))).all()
    return [
        {
            "id": a.id,
            "action": a.action,
            "details": a.details,
            "created_at": a.created_at,
            "actor_id": a.actor_id,
            "actor": ae or ("Admin (panel login)" if a.actor_id is None else "deleted user"),
            "target_user_id": a.target_user_id,
            "target": te,
        }
        for a, ae, te in rows
    ]
