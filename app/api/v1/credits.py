from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1._common import credits_out
from app.core.deps import current_profile
from app.db.session import get_db
from app.models import CreditLedger, Profile
from app.modules.plans_credits.service import get_balance
from app.schemas import CreditHistory, CreditsOut, LedgerEntryOut

router = APIRouter(prefix="/credits", tags=["credits"])


@router.get("", response_model=CreditsOut)
async def get_credits(profile: Profile = Depends(current_profile), db: AsyncSession = Depends(get_db)):
    """Balance for the top-bar badge (FR-BILL-01)."""
    return credits_out(await get_balance(db, profile))


@router.get("/history", response_model=CreditHistory)
async def credit_history(
    limit: int = Query(50, ge=1, le=200),
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.scalars(
        select(CreditLedger)
        .where(CreditLedger.user_id == profile.id)
        .order_by(CreditLedger.created_at.desc())
        .limit(limit)
    )
    return CreditHistory(
        credits=credits_out(await get_balance(db, profile)),
        entries=[LedgerEntryOut.model_validate(r) for r in rows],
    )
