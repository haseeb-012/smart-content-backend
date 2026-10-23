from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ratelimit import RateLimiter, client_key
from app.db.session import get_db
from app.models import WaitlistEntry
from app.schemas import WaitlistIn, WaitlistOut

router = APIRouter(tags=["waitlist"])
limiter = RateLimiter(limit=5, window_seconds=60)


@router.post("/waitlist", response_model=WaitlistOut)
async def join_waitlist(
    body: WaitlistIn, request: Request, db: AsyncSession = Depends(get_db)
) -> WaitlistOut:
    """Public "Notify me" capture (PRD §9.19). Rate-limited per IP. Idempotent per (email, plan)."""
    limiter.check(client_key(request))
    email = body.email.lower()
    exists = await db.scalar(
        select(WaitlistEntry).where(WaitlistEntry.email == email, WaitlistEntry.plan == body.plan)
    )
    if exists:
        return WaitlistOut(ok=True, already_joined=True)
    db.add(WaitlistEntry(email=email, plan=body.plan, source=body.source))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return WaitlistOut(ok=True, already_joined=True)
    return WaitlistOut(ok=True, already_joined=False)
