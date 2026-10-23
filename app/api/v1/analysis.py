"""Video detail, Deep Analysis and ideas (PRD §9.6–9.7)."""

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1._common import feed_video
from app.api.v1.channels import get_session_factory
from app.core.deps import current_profile
from app.core.errors import NotFound
from app.core.ratelimit import RateLimiter
from app.db.session import get_db
from app.models import Idea, Profile
from app.modules.discovery_analysis import analysis as svc
from app.modules.discovery_analysis.channels import SessionFactory
from app.modules.discovery_analysis.llm import LLMProvider, get_llm_provider
from app.modules.discovery_analysis.providers import ChannelProvider, get_channel_provider
from app.modules.identity.service import get_workspace_for
from app.schemas import AnalysisOut, IdeaOut, IdeaPatch, VideoDetail

router = APIRouter(tags=["analysis"])
analyze_limiter = RateLimiter(limit=10, window_seconds=60)


@router.get("/workspaces/{workspace_id}/videos/{video_id}", response_model=VideoDetail)
async def video_detail(
    workspace_id: uuid.UUID,
    video_id: uuid.UUID,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
):
    """Video + this workspace's analysis (if any) + the idea it produced. Free; poll while running."""
    v, c = await svc.get_workspace_video(db, profile, workspace_id, video_id)
    a = await svc.get_analysis(db, workspace_id, video_id)
    idea = await svc.get_idea_for(db, a)
    return VideoDetail(
        video=feed_video(v, c),
        analysis=AnalysisOut.model_validate(a) if a else None,
        idea=IdeaOut.model_validate(idea) if idea else None,
    )


@router.post("/workspaces/{workspace_id}/videos/{video_id}/analysis", response_model=AnalysisOut)
async def analyze(
    workspace_id: uuid.UUID,
    video_id: uuid.UUID,
    response: Response,
    background: BackgroundTasks,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
    llm: LLMProvider = Depends(get_llm_provider),
    channel_provider: ChannelProvider = Depends(get_channel_provider),
    session_factory: SessionFactory = Depends(get_session_factory),
):
    """Start (or return) the Deep Analysis. 1 credit, charged only when it completes.
    202 = queued/running (poll the video detail), 200 = already complete (free)."""
    analyze_limiter.check(str(profile.id))
    a, should_run = await svc.start_analysis(db, profile, workspace_id, video_id)
    if should_run:
        background.add_task(svc.run_analysis, session_factory, llm, channel_provider, a.id)
    response.status_code = status.HTTP_200_OK if a.status == "complete" else status.HTTP_202_ACCEPTED
    return AnalysisOut.model_validate(a)


@router.get("/workspaces/{workspace_id}/ideas", response_model=list[IdeaOut])
async def list_ideas(
    workspace_id: uuid.UUID,
    shortlisted: bool | None = None,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
):
    await get_workspace_for(db, profile, workspace_id)
    q = select(Idea).where(Idea.workspace_id == workspace_id)
    if shortlisted is not None:
        q = q.where(Idea.shortlisted == shortlisted)
    return [IdeaOut.model_validate(i) for i in await db.scalars(q.order_by(Idea.created_at.desc()))]


async def _idea(db: AsyncSession, profile: Profile, workspace_id: uuid.UUID, idea_id: uuid.UUID) -> Idea:
    await get_workspace_for(db, profile, workspace_id, write=True)
    idea = await db.get(Idea, idea_id)
    if idea is None or idea.workspace_id != workspace_id:
        raise NotFound("Idea not found.")
    return idea


@router.patch("/workspaces/{workspace_id}/ideas/{idea_id}", response_model=IdeaOut)
async def update_idea(
    workspace_id: uuid.UUID,
    idea_id: uuid.UUID,
    body: IdeaPatch,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
):
    idea = await _idea(db, profile, workspace_id, idea_id)
    if body.shortlisted is not None:
        idea.shortlisted = body.shortlisted
    if body.title is not None:
        idea.title = body.title
    await db.commit()
    return IdeaOut.model_validate(idea)


@router.delete("/workspaces/{workspace_id}/ideas/{idea_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_idea(
    workspace_id: uuid.UUID,
    idea_id: uuid.UUID,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await db.delete(await _idea(db, profile, workspace_id, idea_id))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
