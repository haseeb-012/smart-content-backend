"""Credit ledger service.

Rules (PRD §4.4, FR-BILL-02/03):
- 1 credit = 1 chargeable action; allowance comes from the plan.
- Charge ONLY after the action succeeded: call `ensure_available()` before starting the work,
  then `consume()` once it completed. Failed work never calls `consume()`.
- Idempotent: `consume()` with an existing (user, idempotency_key) returns the original entry.
- Monthly reset on the account anniversary day; no rollover (each period starts at the allowance).
"""

import calendar
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import AdminActor
from app.core.errors import CreditsExhausted
from app.models import AuditLog, CreditLedger, Profile
from app.modules.plans_credits.plans import limits_for


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def period_start(anchor: datetime, now: datetime | None = None) -> date:
    """Most recent anniversary of `anchor`'s day-of-month on or before `now` (clamped to month length)."""
    now = _aware(now or datetime.now(UTC))
    day = _aware(anchor).day

    def on(year: int, month: int) -> date:
        return date(year, month, min(day, calendar.monthrange(year, month)[1]))

    this_month = on(now.year, now.month)
    if this_month <= now.date():
        return this_month
    y, m = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    return on(y, m)


def next_reset(anchor: datetime, now: datetime | None = None) -> date:
    start = period_start(anchor, now)
    y, m = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    return date(y, m, min(_aware(anchor).day, calendar.monthrange(y, m)[1]))


@dataclass(frozen=True)
class Balance:
    allowance: int
    used: int
    adjustments: int
    remaining: int
    period_start: date
    resets_on: date


async def get_balance(db: AsyncSession, profile: Profile, now: datetime | None = None) -> Balance:
    key = period_start(profile.credit_anchor, now).isoformat()
    rows = await db.execute(
        select(CreditLedger.kind, func.coalesce(func.sum(CreditLedger.amount), 0))
        .where(CreditLedger.user_id == profile.id, CreditLedger.period_key == key)
        .group_by(CreditLedger.kind)
    )
    sums = {kind: int(total) for kind, total in rows.all()}
    used = -sums.get("usage", 0)
    adjustments = sums.get("adjustment", 0)
    allowance = limits_for(profile.plan).credits
    return Balance(
        allowance=allowance,
        used=used,
        adjustments=adjustments,
        remaining=max(0, allowance + adjustments - used),
        period_start=date.fromisoformat(key),
        resets_on=next_reset(profile.credit_anchor, now),
    )


async def ensure_available(db: AsyncSession, profile: Profile, cost: int = 1) -> Balance:
    bal = await get_balance(db, profile)
    if bal.remaining < cost:
        raise CreditsExhausted(
            "You're out of credits for this month.",
            details={"remaining": bal.remaining, "required": cost, "resets_on": bal.resets_on.isoformat()},
        )
    return bal


async def _existing(db: AsyncSession, user_id: uuid.UUID, key: str) -> CreditLedger | None:
    return await db.scalar(
        select(CreditLedger).where(CreditLedger.user_id == user_id, CreditLedger.idempotency_key == key)
    )


async def consume(
    db: AsyncSession,
    profile: Profile,
    *,
    idempotency_key: str,
    action: str,
    cost: int = 1,
    workspace_id: uuid.UUID | None = None,
) -> tuple[CreditLedger, bool]:
    """Record a successful chargeable action. Returns (entry, created). Commits."""
    if existing := await _existing(db, profile.id, idempotency_key):
        return existing, False

    # Serialize concurrent charges for the same user (row lock on Postgres; no-op on SQLite).
    await db.execute(select(Profile.id).where(Profile.id == profile.id).with_for_update())
    await ensure_available(db, profile, cost)

    entry = CreditLedger(
        user_id=profile.id,
        workspace_id=workspace_id,
        kind="usage",
        amount=-cost,
        action=action,
        idempotency_key=idempotency_key,
        period_key=period_start(profile.credit_anchor).isoformat(),
    )
    db.add(entry)
    try:
        await db.commit()
    except IntegrityError:
        # Lost a race with an identical retry — the other request already charged.
        await db.rollback()
        existing = await _existing(db, profile.id, idempotency_key)
        if existing is None:
            raise
        return existing, False
    return entry, True


async def adjust(
    db: AsyncSession, *, actor: AdminActor, target: Profile, amount: int, reason: str
) -> CreditLedger:
    """Admin credit adjustment for the current period (FR-BILL-10). Audited. Commits."""
    entry = CreditLedger(
        user_id=target.id,
        kind="adjustment",
        amount=amount,
        action=f"admin_adjustment: {reason}"[:64],
        idempotency_key=f"adjust:{uuid.uuid4()}",
        period_key=period_start(target.credit_anchor).isoformat(),
    )
    db.add(entry)
    db.add(
        AuditLog(
            actor_id=actor.id,
            target_user_id=target.id,
            action="credits.adjust",
            details={"amount": amount, "reason": reason},
        )
    )
    await db.commit()
    return entry
