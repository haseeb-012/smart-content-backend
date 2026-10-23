"""Deep Analysis (PRD §9.6). Runs only when a user clicks Analyze on one video — never in bulk.

Credit rules (PRD §4.4): check the balance before starting, charge 1 credit ONLY after the AI
succeeded, in the same commit that stores the result. A failed run stores the error and charges
nothing; the user can retry. Ledger key `analysis:{id}` makes the charge at most once per row.
A completed analysis is reused (re-opening it is free). The idea it produces is created free.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import CreditsExhausted, NotFound, RateLimited
from app.models import Channel, Idea, Profile, Video, VideoAnalysis, WorkspaceChannel
from app.modules.discovery_analysis.channels import SessionFactory
from app.modules.discovery_analysis.llm import LLMProvider, VideoContext
from app.modules.discovery_analysis.providers import ChannelProvider, Platform, ProviderError
from app.modules.identity.service import get_workspace_for
from app.modules.plans_credits.service import consume, ensure_available

log = logging.getLogger("smart_content.analysis")

# A run stuck longer than this (worker restart, crash) may be started again.
STALE_AFTER = timedelta(minutes=15)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def get_workspace_video(
    db: AsyncSession, profile: Profile, workspace_id: uuid.UUID, video_id: uuid.UUID, *, write: bool = False
) -> tuple[Video, Channel]:
    """A video is visible to a workspace if it tracks the channel, or already analyzed the video."""
    await get_workspace_for(db, profile, workspace_id, write=write)
    row = (
        await db.execute(
            select(Video, Channel)
            .join(Channel, Channel.id == Video.channel_id)
            .outerjoin(
                WorkspaceChannel,
                and_(
                    WorkspaceChannel.channel_id == Channel.id, WorkspaceChannel.workspace_id == workspace_id
                ),
            )
            .outerjoin(
                VideoAnalysis,
                and_(VideoAnalysis.video_id == Video.id, VideoAnalysis.workspace_id == workspace_id),
            )
            .where(Video.id == video_id, (WorkspaceChannel.id.is_not(None)) | (VideoAnalysis.id.is_not(None)))
        )
    ).first()
    if not row:
        raise NotFound("Video not found.")
    return row[0], row[1]


async def get_analysis(
    db: AsyncSession, workspace_id: uuid.UUID, video_id: uuid.UUID
) -> VideoAnalysis | None:
    return await db.scalar(
        select(VideoAnalysis).where(
            VideoAnalysis.workspace_id == workspace_id, VideoAnalysis.video_id == video_id
        )
    )


async def get_idea_for(db: AsyncSession, analysis: VideoAnalysis | None) -> Idea | None:
    if analysis is None:
        return None
    return await db.scalar(select(Idea).where(Idea.analysis_id == analysis.id))


def _is_running(a: VideoAnalysis) -> bool:
    return a.status in ("queued", "processing") and datetime.now(UTC) - _aware(a.updated_at) < STALE_AFTER


async def start_analysis(
    db: AsyncSession, profile: Profile, workspace_id: uuid.UUID, video_id: uuid.UUID
) -> tuple[VideoAnalysis, bool]:
    """Returns (analysis, should_run). Idempotent: a complete or running analysis is returned as is."""
    await get_workspace_video(db, profile, workspace_id, video_id, write=True)
    existing = await get_analysis(db, workspace_id, video_id)
    if existing and (existing.status == "complete" or _is_running(existing)):
        return existing, False

    since = datetime.now(UTC) - timedelta(days=1)
    started_today = await db.scalar(
        select(func.count())
        .select_from(VideoAnalysis)
        .where(VideoAnalysis.requested_by == profile.id, VideoAnalysis.updated_at >= since)
    )
    limit = get_settings().analysis_daily_limit
    if (started_today or 0) >= limit:
        raise RateLimited(
            f"You've started {limit} analyses in the last 24 hours. Try again later.",
            details={"limit": limit},
        )
    await ensure_available(db, profile, 1)

    if existing:  # failed or stale → retry the same row (still charged at most once)
        existing.status, existing.error = "queued", None
        existing.requested_by = profile.id
        existing.attempts += 1
        existing.updated_at = datetime.now(UTC)
        await db.commit()
        return existing, True

    a = VideoAnalysis(
        workspace_id=workspace_id, video_id=video_id, requested_by=profile.id, status="queued", transcript=[]
    )
    db.add(a)
    try:
        await db.commit()
    except IntegrityError:  # double click raced us — return the other request's row
        await db.rollback()
        other = await get_analysis(db, workspace_id, video_id)
        if other is None:
            raise
        return other, False
    return a, True


async def _mark_failed(session_factory: SessionFactory, analysis_id: uuid.UUID, message: str) -> None:
    async with session_factory() as db:
        a = await db.get(VideoAnalysis, analysis_id)
        if a:
            a.status, a.error = "failed", message[:300]
            await db.commit()


async def run_analysis(
    session_factory: SessionFactory,
    llm: LLMProvider,
    channel_provider: ChannelProvider,
    analysis_id: uuid.UUID,
) -> None:
    """Background job. Never raises — failures are stored on the row and nothing is charged."""
    async with session_factory() as db:
        a = await db.get(VideoAnalysis, analysis_id)
        if a is None or a.status != "queued":
            return
        a.status, a.started_at = "processing", datetime.now(UTC)
        await db.commit()
        video = await db.get(Video, a.video_id)
        channel = await db.get(Channel, video.channel_id) if video else None
        profile = await db.get(Profile, a.requested_by) if a.requested_by else None
        if video is None or channel is None or profile is None:
            await _mark_failed(session_factory, analysis_id, "This video is no longer available.")
            return
        try:
            platform = cast(Platform, video.platform)
            media = await channel_provider.media_url(platform, video.url)
            result = await llm.analyze_video(
                VideoContext(
                    platform=video.platform,
                    url=video.url,
                    caption=video.caption,
                    creator_handle=channel.handle,
                    duration_sec=video.duration_sec,
                    views=video.views,
                    outlier_score=video.outlier_score,
                    media_url=media,
                )
            )
        except ProviderError as e:
            await db.rollback()
            await _mark_failed(session_factory, analysis_id, e.message)
            return
        except Exception:
            log.exception("run_analysis %s failed", analysis_id)
            await db.rollback()
            await _mark_failed(session_factory, analysis_id, "Something went wrong. No credit was charged.")
            return

        a.provider, a.model = llm.name, llm.model
        a.summary, a.hook, a.hook_category = result.summary, result.hook, result.hook_category
        a.format, a.visual, a.idea = result.format, result.visual, result.idea
        a.transcript = [line.model_dump() for line in result.transcript]
        a.status, a.error, a.completed_at = "complete", None, datetime.now(UTC)
        if await db.scalar(select(Idea.id).where(Idea.analysis_id == a.id)) is None:
            d = result.idea_draft
            db.add(
                Idea(
                    workspace_id=a.workspace_id,
                    video_id=a.video_id,
                    analysis_id=a.id,
                    source="analysis",
                    title=d.title,
                    positioning=d.positioning,
                    format=d.format,
                    contrast=d.contrast,
                    personalization=d.personalization,
                    created_by=profile.id,
                )
            )
        try:
            # Charge + store the result in ONE commit (consume() commits the session).
            _, charged_now = await consume(
                db,
                profile,
                idempotency_key=f"analysis:{a.id}",
                action="deep_analysis",
                workspace_id=a.workspace_id,
            )
            if not charged_now:  # already paid for this row earlier — still save the result
                await db.commit()
        except CreditsExhausted:
            await db.rollback()
            await _mark_failed(
                session_factory,
                analysis_id,
                "You ran out of credits before the analysis finished. No credit was charged.",
            )
        except Exception:
            log.exception("run_analysis %s: saving failed", analysis_id)
            await db.rollback()
            await _mark_failed(
                session_factory, analysis_id, "Couldn't save the analysis. No credit was charged."
            )
