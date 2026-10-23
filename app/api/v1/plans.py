from fastapi import APIRouter

from app.api.v1._common import plan_out
from app.modules.plans_credits.plans import PLANS
from app.schemas import PlanOut

router = APIRouter(tags=["plans"])


@router.get("/plans", response_model=list[PlanOut])
async def list_plans() -> list[PlanOut]:
    """Public. Used by the landing pricing section and Settings → Subscription."""
    return [plan_out(p) for p in PLANS.values()]
