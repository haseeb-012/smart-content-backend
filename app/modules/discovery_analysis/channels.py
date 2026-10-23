"""Channel watchlists, ingestion and the Discover feed (PRD §9.4–9.5).

Ingestion only stores public metadata + counts. It never transcribes or AI-analyses scraped videos
(master prompt hard rule) — that happens only on a user's Deep Analyze click (Phase 5 step 4).
"""

import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from urllib.parse import urlparse

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError, NotFound, PlanLimitError, RateLimited
from app.models import AuditLog, Channel, Profile, Video, WorkspaceChannel
from app.modules.discovery_analysis.outlier import compute_outliers, engagement_rate
from app.modules.discovery_analysis.providers import ChannelProvider, Platform, ProviderError
from app.modules.identity.service import get_workspace_for
from app.modules.plans_credits.plans import limits_for

log = logging.getLogger("smart_content.ingestion")

HANDLE_RE = re.compile(r"^[a-z0-9._-]{1,100}$")
MANUAL_REFRESH_COOLDOWN = timedelta(minutes=10)


class InvalidChannel(AppError):
    status_code = 422
    code = "invalid_channel"


class ChannelNotFound(AppError):
    status_code = 404
    code = "channel_not_found"


def normalize_handle(raw: str) -> str:
    h = raw.strip().lstrip("@").lower()
    if not HANDLE_RE.match(h):
        raise InvalidChannel("That doesn't look like a valid handle.")
    return h


def parse_channel_url(url: str) -> tuple[Platform, str]:
    """Supported: tiktok.com/@handle, instagram.com/handle, youtube.com/@handle (FR-CH-01 add via URL)."""
    p = urlparse(url.strip())
    host = (p.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    parts = [s for s in p.path.split("/") if s]
    if p.scheme not in ("http", "https") or not parts:
        raise InvalidChannel("Paste a full profile link, e.g. https://www.tiktok.com/@creator")
    if host == "tiktok.com" and parts[0].startswith("@"):
        return "tiktok", normalize_handle(parts[0])
    if host == "instagram.com" and parts[0] not in ("p", "reel", "reels", "explore", "stories"):
        return "instagram", normalize_handle(parts[0])
    if host == "youtube.com" and parts[0].startswith("@"):
        return "youtube", normalize_handle(parts[0])
    raise InvalidChannel("Use a TikTok, Instagram or YouTube profile link (not a single video).")


# ------------------------------------------------------------------------------------- watchlist


@dataclass
class WatchRow:
    channel: Channel
    outliers: int  # videos with outlier_score ≥ 5
    added_at: datetime


async def list_watchlist(db: AsyncSession, profile: Profile, workspace_id: uuid.UUID) -> list[WatchRow]:
    await get_workspace_for(db, profile, workspace_id)
    outliers = (
        select(func.count(Video.id))
        .where(Video.channel_id == Channel.id, Video.outlier_score >= 5)
        .correlate(Channel)
        .scalar_subquery()
    )
    rows = await db.execute(
        select(Channel, outliers, WorkspaceChannel.created_at)
        .join(WorkspaceChannel, WorkspaceChannel.channel_id == Channel.id)
        .where(WorkspaceChannel.workspace_id == workspace_id)
        .order_by(WorkspaceChannel.created_at.desc())
    )
    return [WatchRow(c, int(n or 0), at) for c, n, at in rows.all()]


async def _get_or_create_channel(
    db: AsyncSession, provider: ChannelProvider, platform: Platform, handle: str
) -> Channel:
    existing = await db.scalar(select(Channel).where(Channel.platform == platform, Channel.handle == handle))
    if existing:
        return existing
    info = await provider.lookup(platform, handle)
    if info is None:
        raise ChannelNotFound(f"We couldn't find @{handle} on {platform.title()}.")
    ch = Channel(
        platform=platform,
        handle=handle,
        display_name=info.display_name or handle,
        external_id=info.external_id,
        avatar_url=info.avatar_url,
        followers=info.followers,
        total_views=info.total_views,
    )
    db.add(ch)
    try:
        await db.flush()
    except IntegrityError:  # created concurrently by another workspace
        await db.rollback()
        found = await db.scalar(select(Channel).where(Channel.platform == platform, Channel.handle == handle))
        assert found is not None  # noqa: S101
        return found
    return ch


async def add_to_watchlist(
    db: AsyncSession,
    profile: Profile,
    workspace_id: uuid.UUID,
    provider: ChannelProvider,
    platform: Platform,
    handle: str,
) -> tuple[Channel, bool]:
    """Returns (channel, newly_added). Idempotent: adding a tracked channel is a no-op."""
    await get_workspace_for(db, profile, workspace_id, write=True)
    plan = profile.plan  # read before any rollback (which would expire the profile)
    limits = limits_for(plan)
    handle = normalize_handle(handle)
    ch = await _get_or_create_channel(db, provider, platform, handle)
    already = await db.scalar(
        select(WorkspaceChannel).where(
            WorkspaceChannel.workspace_id == workspace_id, WorkspaceChannel.channel_id == ch.id
        )
    )
    if already:
        await db.commit()
        return ch, False
    count = await db.scalar(
        select(func.count())
        .select_from(WorkspaceChannel)
        .where(WorkspaceChannel.workspace_id == workspace_id)
    )
    if (count or 0) >= limits.competitors_per_workspace:
        await db.rollback()
        raise PlanLimitError(
            f"Your {limits.name} plan tracks up to {limits.competitors_per_workspace} channels per workspace.",
            details={"limit": limits.competitors_per_workspace, "plan": plan, "resource": "channels"},
        )
    db.add(WorkspaceChannel(workspace_id=workspace_id, channel_id=ch.id, added_by=profile.id))
    db.add(
        AuditLog(
            actor_id=profile.id,
            action="channel.track",
            details={"workspace_id": str(workspace_id), "platform": platform, "handle": handle},
        )
    )
    await db.commit()
    return ch, True


async def remove_from_watchlist(
    db: AsyncSession, profile: Profile, workspace_id: uuid.UUID, channel_id: uuid.UUID
) -> None:
    await get_workspace_for(db, profile, workspace_id, write=True)
    link = await db.scalar(
        select(WorkspaceChannel).where(
            WorkspaceChannel.workspace_id == workspace_id, WorkspaceChannel.channel_id == channel_id
        )
    )
    if not link:
        raise NotFound("That channel isn't on this watchlist.")
    await db.delete(link)
    await db.commit()


async def check_refresh_allowed(
    db: AsyncSession, profile: Profile, workspace_id: uuid.UUID, channel_id: uuid.UUID
) -> None:
    await get_workspace_for(db, profile, workspace_id, write=True)
    ch = await db.scalar(
        select(Channel)
        .join(WorkspaceChannel, WorkspaceChannel.channel_id == Channel.id)
        .where(WorkspaceChannel.workspace_id == workspace_id, Channel.id == channel_id)
    )
    if not ch:
        raise NotFound("That channel isn't on this watchlist.")
    last = ch.last_synced_at
    if (
        last
        and (last if last.tzinfo else last.replace(tzinfo=UTC)) > datetime.now(UTC) - MANUAL_REFRESH_COOLDOWN
    ):
        raise RateLimited("This channel was refreshed recently. Try again in a few minutes.")


# ------------------------------------------------------------------------------------- ingestion

SessionFactory = Callable[[], AsyncSession]


async def sync_channel(
    session_factory: SessionFactory, provider: ChannelProvider, channel_id: uuid.UUID
) -> None:
    """Fetch recent videos, upsert them, recompute outlier scores for the whole channel.
    Runs in the background (own DB session). Never raises — failures are recorded on the channel."""
    async with session_factory() as db:
        ch = await db.get(Channel, channel_id)
        if ch is None:
            return
        try:
            platform = cast(Platform, ch.platform)  # DB check constraint guarantees the value
            fetched = await provider.recent_videos(
                platform, ch.handle, get_settings().ingest_videos_per_channel
            )
            existing = {
                v.external_id: v
                for v in (await db.scalars(select(Video).where(Video.channel_id == ch.id))).all()
            }
            now = datetime.now(UTC)
            for f in fetched:
                v = existing.get(f.external_id)
                if v is None:
                    v = Video(channel_id=ch.id, platform=ch.platform, external_id=f.external_id, url=f.url)
                    db.add(v)
                    existing[f.external_id] = v
                v.url, v.caption, v.thumbnail_url = f.url, f.caption[:5000], f.thumbnail_url
                v.posted_at, v.duration_sec = f.posted_at, f.duration_sec
                v.views, v.likes, v.comments, v.shares = f.views, f.likes, f.comments, f.shares
                v.engagement_rate = engagement_rate(f.views, f.likes, f.comments, f.shares)
                v.fetched_at = now
            all_videos = list(existing.values())
            for v, score in zip(all_videos, compute_outliers(all_videos), strict=True):
                v.outlier_score = score
            ch.video_count = len(all_videos)
            ch.total_views = ch.total_views or sum(v.views for v in all_videos)
            ch.sync_status, ch.sync_error, ch.last_synced_at = "ok", None, now
            await db.commit()
        except ProviderError as e:
            await db.rollback()
            await _mark_error(session_factory, channel_id, e.message)
        except Exception:
            log.exception("sync_channel %s failed", channel_id)
            await db.rollback()
            await _mark_error(session_factory, channel_id, "Unexpected error while syncing.")


async def _mark_error(session_factory: SessionFactory, channel_id: uuid.UUID, message: str) -> None:
    async with session_factory() as db:
        ch = await db.get(Channel, channel_id)
        if ch:
            ch.sync_status, ch.sync_error = "error", message[:300]
            await db.commit()


async def channels_due_for_refresh(db: AsyncSession, older_than: timedelta) -> list[uuid.UUID]:
    """Tracked channels whose data is older than `older_than` (daily refresh job)."""
    cutoff = datetime.now(UTC) - older_than
    tracked = select(WorkspaceChannel.channel_id).distinct()
    rows = await db.scalars(
        select(Channel.id).where(
            Channel.id.in_(tracked), or_(Channel.last_synced_at.is_(None), Channel.last_synced_at < cutoff)
        )
    )
    return list(rows)


# ------------------------------------------------------------------------------------- discovery


async def search_channels(
    db: AsyncSession, provider: ChannelProvider, q: str, platform: Platform | None
) -> list[Channel | tuple[Platform, str, str, int | None]]:
    """Known channels matching the query, plus an exact-handle lookup on the platform(s).
    Returns Channel rows (known) or (platform, handle, display_name, followers) tuples (not yet stored)."""
    term = q.strip().lstrip("@").lower()
    if len(term) < 2:
        return []
    like = f"%{term}%"
    stmt = select(Channel).where(or_(Channel.handle.like(like), func.lower(Channel.display_name).like(like)))
    if platform:
        stmt = stmt.where(Channel.platform == platform)
    known = list((await db.scalars(stmt.order_by(Channel.followers.desc().nullslast()).limit(20))).all())
    results: list[Channel | tuple[Platform, str, str, int | None]] = list(known)
    if HANDLE_RE.match(term):
        for p in [platform] if platform else ["tiktok", "instagram", "youtube"]:
            if any(c.platform == p and c.handle == term for c in known):
                continue
            try:
                info = await provider.lookup(p, term)  # type: ignore[arg-type]
            except ProviderError:
                continue
            if info:
                results.append((info.platform, info.handle, info.display_name, info.followers))
    return results


async def suggested_channels(
    db: AsyncSession, profile: Profile, workspace_id: uuid.UUID, limit: int = 12
) -> list[Channel]:
    """Channels other workspaces track most, that this workspace doesn't track yet."""
    await get_workspace_for(db, profile, workspace_id)
    mine = select(WorkspaceChannel.channel_id).where(WorkspaceChannel.workspace_id == workspace_id)
    popularity = func.count(WorkspaceChannel.id)
    rows = await db.scalars(
        select(Channel)
        .join(WorkspaceChannel, WorkspaceChannel.channel_id == Channel.id)
        .where(Channel.id.not_in(mine))
        .group_by(Channel.id)
        .order_by(popularity.desc(), Channel.followers.desc().nullslast())
        .limit(limit)
    )
    return list(rows)


FeedSort = Literal["outlier", "views", "engagement", "newest"]


@dataclass
class FeedFilters:
    channel_ids: list[uuid.UUID] | None = None
    platform: Platform | None = None
    min_outlier: float | None = None
    min_views: int | None = None
    max_views: int | None = None
    min_engagement: float | None = None
    posted_within_days: int | None = None
    q: str | None = None
    sort: FeedSort = "outlier"
    limit: int = 48
    offset: int = 0


async def discover_feed(
    db: AsyncSession, profile: Profile, workspace_id: uuid.UUID, f: FeedFilters
) -> tuple[list[tuple[Video, Channel]], int]:
    """Videos from the workspace's watchlist. All filters combine with AND (FR-VID-02)."""
    await get_workspace_for(db, profile, workspace_id)
    tracked = select(WorkspaceChannel.channel_id).where(WorkspaceChannel.workspace_id == workspace_id)
    stmt = (
        select(Video, Channel)
        .join(Channel, Channel.id == Video.channel_id)
        .where(Video.channel_id.in_(tracked))
    )
    if f.channel_ids:
        stmt = stmt.where(Video.channel_id.in_(f.channel_ids))
    if f.platform:
        stmt = stmt.where(Video.platform == f.platform)
    if f.min_outlier is not None:
        stmt = stmt.where(Video.outlier_score >= f.min_outlier)
    if f.min_views is not None:
        stmt = stmt.where(Video.views >= f.min_views)
    if f.max_views is not None:
        stmt = stmt.where(Video.views <= f.max_views)
    if f.min_engagement is not None:
        stmt = stmt.where(Video.engagement_rate >= f.min_engagement)
    if f.posted_within_days:
        stmt = stmt.where(Video.posted_at >= datetime.now(UTC) - timedelta(days=f.posted_within_days))
    if f.q and f.q.strip():
        like = f"%{f.q.strip().lower()}%"
        stmt = stmt.where(or_(func.lower(Video.caption).like(like), Channel.handle.like(like)))
    total = int(await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    orders: dict[str, ColumnElement[Any]] = {
        "outlier": Video.outlier_score.desc().nullslast(),
        "views": Video.views.desc(),
        "engagement": Video.engagement_rate.desc(),
        "newest": Video.posted_at.desc().nullslast(),
    }
    rows = await db.execute(stmt.order_by(orders[f.sort], Video.id).limit(f.limit).offset(f.offset))
    return [(v, c) for v, c in rows.all()], total
