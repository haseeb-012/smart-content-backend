import uuid
from datetime import UTC, date, datetime

import pytest

from app.core.errors import CreditsExhausted
from app.models import Profile
from app.modules.plans_credits.service import consume, ensure_available, get_balance, next_reset, period_start


async def _profile(db, plan="pro", anchor=None) -> Profile:
    p = Profile(id=uuid.uuid4(), email="c@example.com", plan=plan, credit_anchor=anchor or datetime.now(UTC))
    db.add(p)
    await db.commit()
    return p


async def test_consume_is_idempotent(db):
    p = await _profile(db)
    e1, created1 = await consume(db, p, idempotency_key="analysis:ws1:v1", action="Video analysis")
    e2, created2 = await consume(db, p, idempotency_key="analysis:ws1:v1", action="Video analysis")
    assert created1 is True and created2 is False and e1.id == e2.id
    assert (await get_balance(db, p)).remaining == 99  # retried, charged once (FR-BILL-03)


async def test_exhaustion_blocks_and_reports_reset_date(db):
    p = await _profile(db)
    for i in range(100):
        await consume(db, p, idempotency_key=f"k{i}", action="x")
    with pytest.raises(CreditsExhausted) as exc:
        await consume(db, p, idempotency_key="k100", action="x")
    assert exc.value.details["remaining"] == 0
    assert "resets_on" in exc.value.details
    # Browsing-type checks also see zero.
    with pytest.raises(CreditsExhausted):
        await ensure_available(db, p)


async def test_allowance_follows_plan(db):
    p = await _profile(db, plan="visionary")
    assert (await get_balance(db, p)).remaining == 250


async def test_monthly_reset_without_rollover(db):
    anchor = datetime(2026, 1, 15, tzinfo=UTC)
    p = await _profile(db, anchor=anchor)
    await consume(db, p, idempotency_key="a", action="x")
    now_same = datetime.now(UTC)
    bal = await get_balance(db, p, now_same)
    assert bal.used == 1
    # Next period starts fresh at the allowance — unused credits don't carry over.
    later = datetime(bal.resets_on.year, bal.resets_on.month, bal.resets_on.day, 12, tzinfo=UTC)
    assert (await get_balance(db, p, later)).remaining == 100


@pytest.mark.parametrize(
    "anchor, now, start, reset",
    [
        (
            datetime(2026, 1, 31, tzinfo=UTC),
            datetime(2026, 2, 28, 9, tzinfo=UTC),
            date(2026, 2, 28),
            date(2026, 3, 31),
        ),
        (
            datetime(2026, 1, 31, tzinfo=UTC),
            datetime(2026, 2, 27, 9, tzinfo=UTC),
            date(2026, 1, 31),
            date(2026, 2, 28),
        ),
        (
            datetime(2025, 3, 10, tzinfo=UTC),
            datetime(2026, 1, 5, tzinfo=UTC),
            date(2025, 12, 10),
            date(2026, 1, 10),
        ),
    ],
)
def test_anniversary_periods(anchor, now, start, reset):
    assert period_start(anchor, now) == start
    assert next_reset(anchor, now) == reset


async def test_credits_endpoints(client, alice):
    r = await client.get("/api/v1/credits", headers=alice.headers)
    assert r.json()["remaining"] == 100
    r = await client.get("/api/v1/credits/history", headers=alice.headers)
    assert r.json()["entries"] == []
