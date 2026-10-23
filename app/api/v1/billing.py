from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import current_profile
from app.db.session import get_db
from app.models import Profile
from app.modules.plans_credits.billing import BillingAdapter, get_billing_adapter

router = APIRouter(prefix="/billing", tags=["billing"])


class CheckoutIn(BaseModel):
    plan: Literal["pro", "visionary", "titan"]
    cycle: Literal["monthly", "yearly"] = "monthly"


class RedirectOut(BaseModel):
    url: str


class WebhookOut(BaseModel):
    received: bool
    duplicate: bool


@router.post("/checkout-session", response_model=RedirectOut)
async def checkout_session(
    body: CheckoutIn,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
    billing: BillingAdapter = Depends(get_billing_adapter),
) -> RedirectOut:
    """Returns a Stripe-hosted Checkout URL (test mode). The frontend redirects the browser to it."""
    return RedirectOut(url=await billing.create_checkout(db, profile, body.plan, body.cycle))


@router.post("/portal-session", response_model=RedirectOut)
async def portal_session(
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
    billing: BillingAdapter = Depends(get_billing_adapter),
) -> RedirectOut:
    """Returns a Stripe Customer Portal URL for upgrades, downgrades, cancellation and card changes."""
    return RedirectOut(url=await billing.create_portal(db, profile))


@router.post("/webhook", response_model=WebhookOut)
async def webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    billing: BillingAdapter = Depends(get_billing_adapter),
) -> WebhookOut:
    """Stripe → us. Signature-verified; duplicates are acknowledged but not re-applied."""
    payload = await request.body()
    applied = await billing.handle_webhook(db, payload, request.headers.get("stripe-signature"))
    return WebhookOut(received=True, duplicate=not applied)
