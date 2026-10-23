from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1._common import credits_out, profile_plan
from app.core.deps import current_profile
from app.db.session import get_db
from app.models import Profile
from app.modules.plans_credits.billing import get_billing_adapter
from app.modules.plans_credits.service import get_balance
from app.schemas import MeOut, MeResponse, MeUpdate

router = APIRouter(tags=["me"])


async def _me(db: AsyncSession, profile: Profile) -> MeResponse:
    billing = get_billing_adapter()
    return MeResponse(
        profile=MeOut.model_validate(profile),
        plan=profile_plan(profile),
        credits=credits_out(await get_balance(db, profile)),
        billing={
            "provider": billing.name,
            "checkout_available": billing.checkout_available(),
            "test_mode": True,
            "subscription_status": profile.subscription_status or "",
            "billing_cycle": profile.billing_cycle or "",
            "has_billing_account": bool(profile.stripe_customer_id),
        },
    )


@router.get("/me", response_model=MeResponse)
async def get_me(
    profile: Profile = Depends(current_profile), db: AsyncSession = Depends(get_db)
) -> MeResponse:
    return await _me(db, profile)


@router.patch("/me", response_model=MeResponse)
async def update_me(
    body: MeUpdate, profile: Profile = Depends(current_profile), db: AsyncSession = Depends(get_db)
) -> MeResponse:
    profile.full_name = body.full_name.strip()
    await db.commit()
    return await _me(db, profile)
