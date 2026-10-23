"""Social verification (PRD §9.13) and own-account analytics (PRD §9.3 Dashboard).

Verification: the user adds a one-time code to their public bio; we read the bio through the
ChannelProvider. No passwords, no OAuth. A handle can be verified by one user at a time.
Analytics only ever cover the user's verified accounts.
"""

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, Conflict, NotFound, PlanLimitError, RateLimited
from app.models import AuditLog, Channel, ChannelStat, Profile, SocialAccount, Video
from app.modules.discovery_analysis.channels import _get_or_create_channel, normalize_handle
from app.modules.discovery_analysis.providers import ChannelProvider, Platform
from app.modules.plans_credits.plans import limits_for

CHECK_COOLDOWN = timedelta(seconds=20)
MAX_PENDING = 10
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


class CodeNotFound(AppError):
    status_code = 409
    code = "code_not_found"


def new_code() -> str:
    return "SC-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def list_accounts(db: AsyncSession, profile: Profile) -> list[SocialAccount]:
    return list(
        await db.scalars(
            select(SocialAccount)
            .where(SocialAccount.user_id == profile.id)
            .order_by(SocialAccount.created_at)
        )
    )


async def _verified_count(db: AsyncSession, profile: Profile) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(SocialAccount)
            .where(SocialAccount.user_id == profile.id, SocialAccount.status == "verified")
        )
        or 0
    )


async def start(db: AsyncSession, profile: Profile, platform: Platform, handle: str) -> SocialAccount:
    """Create (or return) a pending claim with a fresh code."""
    handle = normalize_handle(handle)
    existing = await db.scalar(
        select(SocialAccount).where(
            SocialAccount.user_id == profile.id,
            SocialAccount.platform == platform,
            SocialAccount.handle == handle,
        )
    )
    if existing:
        return existing
    limits = limits_for(profile.plan)
    if await _verified_count(db, profile) >= limits.verified_channels:
        raise PlanLimitError(
            f"Your {limits.name} plan includes {limits.verified_channels} verified accounts.",
            details={"limit": limits.verified_channels, "resource": "verified_channels"},
        )
    pending = await db.scalar(
        select(func.count())
        .select_from(SocialAccount)
        .where(SocialAccount.user_id == profile.id, SocialAccount.status == "pending")
    )
    if (pending or 0) >= MAX_PENDING:
        raise PlanLimitError(
            "Finish or remove a pending verification first.", details={"resource": "pending"}
        )
    acc = SocialAccount(user_id=profile.id, platform=platform, handle=handle, code=new_code())
    db.add(acc)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        again = await db.scalar(
            select(SocialAccount).where(
                SocialAccount.user_id == profile.id,
                SocialAccount.platform == platform,
                SocialAccount.handle == handle,
            )
        )
        if again is None:
            raise
        return again
    return acc


async def _own(db: AsyncSession, profile: Profile, account_id: uuid.UUID) -> SocialAccount:
    acc = await db.get(SocialAccount, account_id)
    if acc is None or acc.user_id != profile.id:
        raise NotFound("Account not found.")
    return acc


async def verify(
    db: AsyncSession, profile: Profile, account_id: uuid.UUID, provider: ChannelProvider
) -> tuple[SocialAccount, bool]:
    """Check the bio. Returns (account, newly_verified). Raises CodeNotFound if the code isn't there yet."""
    acc = await _own(db, profile, account_id)
    if acc.status == "verified":
        return acc, False
    now = datetime.now(UTC)
    if acc.last_checked_at and now - _aware(acc.last_checked_at) < CHECK_COOLDOWN:
        raise RateLimited("We just checked — give it a few seconds and try again.")
    acc.last_checked_at = now
    platform = cast(Platform, acc.platform)
    found = await provider.bio_has_code(platform, acc.handle, acc.code)
    if found is None:
        await db.commit()
        raise NotFound(
            f"We couldn't read the bio of @{acc.handle} on {acc.platform.title()}. Check the handle."
        )
    if not found:
        await db.commit()
        raise CodeNotFound(
            f"We couldn't find {acc.code} in @{acc.handle}'s bio yet. Save your bio, wait a minute, then try again.",
            details={"code": acc.code},
        )
    taken = await db.scalar(
        select(SocialAccount.id).where(
            SocialAccount.platform == acc.platform,
            SocialAccount.handle == acc.handle,
            SocialAccount.status == "verified",
            SocialAccount.user_id != profile.id,
        )
    )
    if taken:
        raise Conflict("This account is already verified by another user. Contact support if it's yours.")
    if await _verified_count(db, profile) >= limits_for(profile.plan).verified_channels:
        raise PlanLimitError("You've reached the verified accounts limit on your plan.")
    ch = await _get_or_create_channel(db, provider, platform, acc.handle)
    acc.status, acc.verified_at, acc.channel_id = "verified", now, ch.id
    db.add(
        AuditLog(
            actor_id=profile.id,
            target_user_id=profile.id,
            action="social.verified",
            details={"platform": acc.platform, "handle": acc.handle},
        )
    )
    await db.commit()
    return acc, True


async def remove(db: AsyncSession, profile: Profile, account_id: uuid.UUID) -> None:
    acc = await _own(db, profile, account_id)
    await db.delete(acc)
    await db.commit()


# ------------------------------------------------------------------ analytics


@dataclass
class Point:
    date: date
    value: int


@dataclass
class Analytics:
    accounts: list[tuple[SocialAccount, Channel | None]]
    days: int
    total_followers: int | None
    followers_gained: int | None
    views: int
    posts: int
    views_by_day: list[Point]
    followers_by_day: list[Point]
    recent: list[tuple[Video, Channel]]


async def analytics(db: AsyncSession, profile: Profile, days: int) -> Analytics:
    accs = [a for a in await list_accounts(db, profile) if a.status == "verified" and a.channel_id]
    channel_ids = [a.channel_id for a in accs if a.channel_id]
    channels = (
        {c.id: c for c in await db.scalars(select(Channel).where(Channel.id.in_(channel_ids)))}
        if channel_ids
        else {}
    )
    today = datetime.now(UTC).date()
    start_day = today - timedelta(days=days - 1)
    span = [start_day + timedelta(days=i) for i in range(days)]

    # Views of posts published each day (what the creator posted in the period and how it did).
    views_day: dict[date, int] = {d: 0 for d in span}
    posts = 0
    recent: list[tuple[Video, Channel]] = []
    if channel_ids:
        rows = (
            await db.execute(
                select(Video, Channel)
                .join(Channel, Channel.id == Video.channel_id)
                .where(Video.channel_id.in_(channel_ids))
                .order_by(Video.posted_at.desc().nulls_last())
            )
        ).all()
        recent = [(v, c) for v, c in rows[:6]]
        for v, _ in rows:
            if v.posted_at is None:
                continue
            d = _aware(v.posted_at).date()
            if d in views_day:
                views_day[d] += v.views
                posts += 1

    # Followers: daily snapshots, summed across accounts; gained = change between consecutive days.
    followers_day: dict[date, int] = {}
    if channel_ids:
        stats = await db.scalars(
            select(ChannelStat).where(
                ChannelStat.channel_id.in_(channel_ids),
                ChannelStat.captured_on >= start_day - timedelta(days=1),
            )
        )
        for s in stats:
            if s.followers is not None:
                followers_day[s.captured_on] = followers_day.get(s.captured_on, 0) + s.followers
    gained: list[Point] = []
    prev: int | None = followers_day.get(start_day - timedelta(days=1))
    for d in span:
        cur = followers_day.get(d)
        gained.append(Point(d, (cur - prev) if (cur is not None and prev is not None) else 0))
        if cur is not None:
            prev = cur
    known = [followers_day[d] for d in sorted(followers_day) if d >= start_day]
    total = sum(c.followers or 0 for c in channels.values()) if channels else None
    return Analytics(
        accounts=[(a, channels.get(a.channel_id) if a.channel_id else None) for a in accs],
        days=days,
        total_followers=total,
        followers_gained=(known[-1] - known[0]) if len(known) >= 2 else None,
        views=sum(views_day.values()),
        posts=posts,
        views_by_day=[Point(d, views_day[d]) for d in span],
        followers_by_day=gained,
        recent=recent,
    )
