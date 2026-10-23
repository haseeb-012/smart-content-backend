"""Channels watchlist, creator search/suggestions and the Discover feed (PRD §9.4–9.5)."""

import uuid
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1._common import feed_video
from app.core.deps import current_profile
from app.core.ratelimit import RateLimiter, client_key
from app.db.session import get_db, get_sessionmaker
from app.models import Channel, Profile, WorkspaceChannel
from app.modules.discovery_analysis import channels as svc
from app.modules.discovery_analysis.llm import LLMProvider, get_llm_provider
from app.modules.discovery_analysis.providers import ChannelProvider, get_channel_provider
from app.modules.identity.service import get_workspace_for
from app.schemas import (
    ChannelAdd,
    ChannelAddResult,
    ChannelOut,
    ChannelSearchResult,
    FeedPage,
    NicheSearchIn,
    Platform,
    WatchlistItem,
)

router = APIRouter(tags=["channels"])
search_limiter = RateLimiter(limit=30, window_seconds=60)
niche_limiter = RateLimiter(limit=20, window_seconds=3600)


def get_session_factory() -> svc.SessionFactory:
    """Session factory for background jobs (overridden in tests)."""
    return get_sessionmaker()


@router.get("/workspaces/{workspace_id}/channels", response_model=list[WatchlistItem])
async def watchlist(
    workspace_id: uuid.UUID, profile: Profile = Depends(current_profile), db: AsyncSession = Depends(get_db)
):
    rows = await svc.list_watchlist(db, profile, workspace_id)
    return [
        WatchlistItem(
            **ChannelOut.model_validate(r.channel).model_dump(),
            outlier_videos=r.outliers,
            added_at=r.added_at,
        )
        for r in rows
    ]


@router.post("/workspaces/{workspace_id}/channels", response_model=ChannelAddResult)
async def add_channel(
    workspace_id: uuid.UUID,
    body: ChannelAdd,
    response: Response,
    background: BackgroundTasks,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
    provider: ChannelProvider = Depends(get_channel_provider),
    session_factory: svc.SessionFactory = Depends(get_session_factory),
):
    if body.url:
        platform, handle = svc.parse_channel_url(body.url)
    elif body.platform and body.handle:
        platform, handle = body.platform, body.handle
    else:
        raise svc.InvalidChannel("Send a profile URL, or a platform and handle.")
    ch, added = await svc.add_to_watchlist(db, profile, workspace_id, provider, platform, handle)
    # First time we see this channel (or its last sync failed): fetch its videos in the background.
    if ch.last_synced_at is None or ch.sync_status == "error":
        background.add_task(svc.sync_channel, session_factory, provider, ch.id)
    response.status_code = status.HTTP_201_CREATED if added else status.HTTP_200_OK
    return ChannelAddResult(channel=ChannelOut.model_validate(ch), added=added)


@router.delete("/workspaces/{workspace_id}/channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_channel(
    workspace_id: uuid.UUID,
    channel_id: uuid.UUID,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await svc.remove_from_watchlist(db, profile, workspace_id, channel_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/workspaces/{workspace_id}/channels/{channel_id}/refresh", status_code=status.HTTP_202_ACCEPTED)
async def refresh_channel(
    workspace_id: uuid.UUID,
    channel_id: uuid.UUID,
    background: BackgroundTasks,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
    provider: ChannelProvider = Depends(get_channel_provider),
    session_factory: svc.SessionFactory = Depends(get_session_factory),
) -> dict[str, bool]:
    await svc.check_refresh_allowed(db, profile, workspace_id, channel_id)
    background.add_task(svc.sync_channel, session_factory, provider, channel_id)
    return {"queued": True}


@router.get("/workspaces/{workspace_id}/channels/suggested", response_model=list[ChannelOut])
async def suggested(
    workspace_id: uuid.UUID, profile: Profile = Depends(current_profile), db: AsyncSession = Depends(get_db)
):
    return [ChannelOut.model_validate(c) for c in await svc.suggested_channels(db, profile, workspace_id)]


@router.get("/workspaces/{workspace_id}/channels/search", response_model=list[ChannelSearchResult])
async def search(
    workspace_id: uuid.UUID,
    request: Request,
    q: str = Query(min_length=2, max_length=100),
    platform: Platform | None = None,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
    provider: ChannelProvider = Depends(get_channel_provider),
):
    """Search known creators by name/handle + exact-handle lookup on the platforms."""
    search_limiter.check(f"{profile.id}:{client_key(request)}")
    await get_workspace_for(db, profile, workspace_id)
    return await _search_out(db, workspace_id, await svc.search_channels(db, provider, q, platform))


@router.get("/workspaces/{workspace_id}/videos", response_model=FeedPage)
async def discover(
    workspace_id: uuid.UUID,
    channel_id: list[uuid.UUID] | None = Query(default=None),
    platform: Platform | None = None,
    min_outlier: float | None = Query(default=None, ge=0),
    min_views: int | None = Query(default=None, ge=0),
    max_views: int | None = Query(default=None, ge=0),
    min_engagement: float | None = Query(default=None, ge=0),
    posted_within_days: int | None = Query(default=None, ge=1, le=3650),
    q: str | None = Query(default=None, max_length=100),
    sort: Literal["outlier", "views", "engagement", "newest"] = "outlier",
    limit: int = Query(default=48, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
):
    """Discover feed: outlier videos from the workspace's watchlist. Free (no credits)."""
    f = svc.FeedFilters(
        channel_ids=channel_id,
        platform=platform,
        min_outlier=min_outlier,
        min_views=min_views,
        max_views=max_views,
        min_engagement=min_engagement,
        posted_within_days=posted_within_days,
        q=q,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    rows, total = await svc.discover_feed(db, profile, workspace_id, f)
    return FeedPage(
        items=[feed_video(v, c) for v, c in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


async def _search_out(db: AsyncSession, workspace_id: uuid.UUID, found: list) -> list[ChannelSearchResult]:  # type: ignore[type-arg]
    tracked = set(
        (
            await db.scalars(
                select(WorkspaceChannel.channel_id).where(WorkspaceChannel.workspace_id == workspace_id)
            )
        ).all()
    )
    out: list[ChannelSearchResult] = []
    for r in found:
        if isinstance(r, Channel):
            out.append(
                ChannelSearchResult(
                    platform=r.platform,  # type: ignore[arg-type]
                    handle=r.handle,
                    display_name=r.display_name,
                    followers=r.followers,
                    channel_id=r.id,
                    tracked=r.id in tracked,
                )
            )
        else:
            p, h, name, followers = r
            out.append(
                ChannelSearchResult(
                    platform=p,
                    handle=h,
                    display_name=name,
                    followers=followers,
                    channel_id=None,
                    tracked=False,
                )
            )
    return out


@router.post("/workspaces/{workspace_id}/channels/niche-search", response_model=list[ChannelSearchResult])
async def niche_search(
    workspace_id: uuid.UUID,
    body: NicheSearchIn,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
    provider: ChannelProvider = Depends(get_channel_provider),
    llm: LLMProvider = Depends(get_llm_provider),
):
    """'Describe your niche' → AI-suggested creators, each verified to exist. Free (rate-limited)."""
    await get_workspace_for(db, profile, workspace_id)
    niche_limiter.check(str(profile.id))
    return await _search_out(
        db, workspace_id, await svc.niche_search(db, provider, llm, body.description, body.platform)
    )
