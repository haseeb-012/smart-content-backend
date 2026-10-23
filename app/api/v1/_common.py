from dataclasses import asdict

from app.models import Channel, Profile, Video
from app.modules.plans_credits.plans import PlanLimits, limits_for
from app.modules.plans_credits.service import Balance
from app.schemas import ChannelOut, CreditsOut, FeedVideo, PlanOut


def plan_out(p: PlanLimits) -> PlanOut:
    d = asdict(p)
    d["features"] = sorted(p.features)
    return PlanOut(**d)


def credits_out(b: Balance) -> CreditsOut:
    return CreditsOut(**asdict(b))


def profile_plan(profile: Profile) -> PlanOut:
    return plan_out(limits_for(profile.plan))


def feed_video(v: Video, c: Channel) -> FeedVideo:
    return FeedVideo(
        id=v.id,
        platform=v.platform,  # type: ignore[arg-type]
        url=v.url,
        caption=v.caption,
        thumbnail_url=v.thumbnail_url,
        posted_at=v.posted_at,
        duration_sec=v.duration_sec,
        views=v.views,
        likes=v.likes,
        comments=v.comments,
        shares=v.shares,
        engagement_rate=v.engagement_rate,
        outlier_score=v.outlier_score,
        channel=ChannelOut.model_validate(c),
    )
