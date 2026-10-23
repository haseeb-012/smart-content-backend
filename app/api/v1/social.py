"""Social verification + own-account analytics (per user, not per workspace)."""

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1._common import feed_video
from app.api.v1.channels import get_session_factory
from app.core.deps import current_profile
from app.core.errors import AppError
from app.db.session import get_db
from app.models import Channel, Profile, SocialAccount
from app.modules.discovery_analysis.channels import SessionFactory, sync_channel
from app.modules.discovery_analysis.providers import ChannelProvider, get_channel_provider
from app.modules.identity import social as svc
from app.schemas import (
    AnalyticsOut,
    ChannelOut,
    McpPermissionsIn,
    McpStatus,
    SeriesPoint,
    SocialAccountOut,
    SocialStart,
)

router = APIRouter(prefix="/me", tags=["social"])


class InvalidParam(AppError):
    status_code = 422
    code = "validation_error"


def _out(a: SocialAccount, ch: Channel | None) -> SocialAccountOut:
    return SocialAccountOut(
        id=a.id,
        platform=a.platform,  # type: ignore[arg-type]
        handle=a.handle,
        code=a.code,
        status=a.status,  # type: ignore[arg-type]
        verified_at=a.verified_at,
        channel=ChannelOut.model_validate(ch) if ch else None,
    )


@router.get("/social", response_model=list[SocialAccountOut])
async def list_social(profile: Profile = Depends(current_profile), db: AsyncSession = Depends(get_db)):
    out = []
    for a in await svc.list_accounts(db, profile):
        out.append(_out(a, await db.get(Channel, a.channel_id) if a.channel_id else None))
    return out


@router.post("/social", response_model=SocialAccountOut, status_code=status.HTTP_201_CREATED)
async def start_verification(
    body: SocialStart, profile: Profile = Depends(current_profile), db: AsyncSession = Depends(get_db)
):
    """Returns a one-time code to paste into the bio. Idempotent per (platform, handle)."""
    return _out(await svc.start(db, profile, body.platform, body.handle), None)


@router.post("/social/{account_id}/verify", response_model=SocialAccountOut)
async def verify(
    account_id: uuid.UUID,
    background: BackgroundTasks,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
    provider: ChannelProvider = Depends(get_channel_provider),
    session_factory: SessionFactory = Depends(get_session_factory),
):
    """Reads the public bio. `code_not_found` (409) until the code is there; then imports the account."""
    acc, newly = await svc.verify(db, profile, account_id, provider)
    ch = await db.get(Channel, acc.channel_id) if acc.channel_id else None
    if newly and acc.channel_id:
        background.add_task(sync_channel, session_factory, provider, acc.channel_id)
    return _out(acc, ch)


@router.delete("/social/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove(
    account_id: uuid.UUID, profile: Profile = Depends(current_profile), db: AsyncSession = Depends(get_db)
) -> Response:
    await svc.remove(db, profile, account_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/analytics", response_model=AnalyticsOut)
async def analytics(
    days: int = 30,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
):
    """Dashboard numbers for the user's verified accounts only. Free. `days`: 7, 30 or 90."""
    if days not in (7, 30, 90):
        raise InvalidParam("days must be 7, 30 or 90.")
    a = await svc.analytics(db, profile, days)
    return AnalyticsOut(
        days=a.days,
        accounts=[_out(acc, ch) for acc, ch in a.accounts],
        total_followers=a.total_followers,
        followers_gained=a.followers_gained,
        views=a.views,
        posts=a.posts,
        views_by_day=[SeriesPoint(date=p.date, value=p.value) for p in a.views_by_day],
        followers_by_day=[SeriesPoint(date=p.date, value=p.value) for p in a.followers_by_day],
        recent_videos=[feed_video(v, c) for v, c in a.recent],
    )


@router.get("/mcp", response_model=McpStatus)
async def mcp_status(profile: Profile = Depends(current_profile)):
    """Connector settings: whether MCP is on for this account, the server URL and tool permissions."""
    from app.core.config import get_settings
    from app.modules.mcp.tools import TOOLS
    from app.modules.plans_credits.plans import limits_for

    perms = profile.mcp_tool_permissions or {}
    return McpStatus(
        enabled=profile.mcp_enabled,
        plan_includes_mcp="mcp" in limits_for(profile.plan).features,
        server_url=f"{get_settings().public_api_url.rstrip('/')}/mcp",
        tools=[
            {
                "name": t.name,
                "title": t.title,
                "description": t.description,
                "permission": perms.get(t.name, "allow"),
            }
            for t in TOOLS
        ],
    )


@router.patch("/mcp", response_model=McpStatus)
async def mcp_permissions(
    body: McpPermissionsIn, profile: Profile = Depends(current_profile), db: AsyncSession = Depends(get_db)
):
    from app.modules.mcp.tools import TOOL_NAMES

    unknown = set(body.permissions) - set(TOOL_NAMES)
    if unknown:
        raise InvalidParam(f"Unknown tools: {', '.join(sorted(unknown))}")
    profile.mcp_tool_permissions = {**(profile.mcp_tool_permissions or {}), **body.permissions}
    await db.commit()
    return await mcp_status(profile)
