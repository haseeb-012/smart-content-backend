"""Billing adapter interface (FR-BILL-05). Swapping providers touches only this module.

- MockBillingAdapter (BILLING_PROVIDER=mock): stored plan only, no checkout.
- StripeTestBillingAdapter (BILLING_PROVIDER=stripe_test): Stripe-hosted Checkout (subscription mode),
  Customer Portal, and signed webhooks that set the plan + credit period. TEST MODE ONLY.

We never build card fields and never see card data — Stripe hosts every payment page.
"""

import logging
import time
import uuid
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, Literal, Protocol

import stripe
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import Settings, get_settings
from app.core.errors import AppError, Conflict
from app.models import AuditLog, Profile, StripeEvent
from app.modules.plans_credits.service import period_start

log = logging.getLogger("smart_content.billing")

Cycle = Literal["monthly", "yearly"]
ACTIVE_STATUSES = {"active", "trialing", "past_due"}
ENDED_STATUSES = {"canceled", "unpaid", "incomplete_expired"}


class BillingUnavailable(AppError):
    status_code = 409
    code = "billing_unavailable"


class InvalidSignature(AppError):
    status_code = 400
    code = "invalid_signature"


class BillingAdapter(Protocol):
    name: str

    def checkout_available(self) -> bool: ...
    async def create_checkout(self, db: AsyncSession, profile: Profile, plan: str, cycle: Cycle) -> str: ...
    async def create_portal(self, db: AsyncSession, profile: Profile) -> str: ...
    async def handle_webhook(self, db: AsyncSession, payload: bytes, signature: str | None) -> bool: ...


class MockBillingAdapter:
    name = "mock"

    def checkout_available(self) -> bool:
        return False

    async def create_checkout(self, db: AsyncSession, profile: Profile, plan: str, cycle: Cycle) -> str:
        raise BillingUnavailable("Billing isn't enabled yet. Join the waitlist and we'll let you know.")

    async def create_portal(self, db: AsyncSession, profile: Profile) -> str:
        raise BillingUnavailable("Billing isn't enabled yet.")

    async def handle_webhook(self, db: AsyncSession, payload: bytes, signature: str | None) -> bool:
        raise BillingUnavailable("Billing isn't enabled.")


# ---------------------------------------------------------------------------- Stripe


class StripeGateway(Protocol):
    """The few Stripe API calls we make. Real implementation below; tests use a fake."""

    def create_customer(self, *, email: str | None, user_id: str) -> str: ...
    def create_checkout_session(self, params: dict[str, Any], idempotency_key: str) -> str: ...
    def create_portal_session(self, *, customer: str, return_url: str) -> str: ...


class StripeSdkGateway:
    def __init__(self, settings: Settings):
        self.client = stripe.StripeClient(
            settings.stripe_secret_key, stripe_version=settings.stripe_api_version
        )

    def create_customer(self, *, email: str | None, user_id: str) -> str:
        params: dict[str, Any] = {"metadata": {"user_id": user_id}}
        if email:
            params["email"] = email
        return self.client.v1.customers.create(params, {"idempotency_key": f"customer:{user_id}"}).id  # type: ignore[arg-type]

    def create_checkout_session(self, params: dict[str, Any], idempotency_key: str) -> str:
        session = self.client.v1.checkout.sessions.create(params, {"idempotency_key": idempotency_key})  # type: ignore[arg-type]
        assert session.url  # noqa: S101 — hosted Checkout always returns a URL
        return session.url

    def create_portal_session(self, *, customer: str, return_url: str) -> str:
        return self.client.v1.billing_portal.sessions.create(
            {"customer": customer, "return_url": return_url}
        ).url


class StripeTestBillingAdapter:
    name = "stripe_test"

    def __init__(self, settings: Settings, gateway: StripeGateway):
        self.settings = settings
        self.gateway = gateway
        self.price_to_plan = {price: key for key, price in settings.stripe_prices.items()}

    def checkout_available(self) -> bool:
        return bool(self.settings.stripe_prices)

    # ---- Checkout / portal

    async def _ensure_customer(self, db: AsyncSession, profile: Profile) -> str:
        if profile.stripe_customer_id:
            return profile.stripe_customer_id
        cid = await run_in_threadpool(
            self.gateway.create_customer, email=profile.email, user_id=str(profile.id)
        )
        profile.stripe_customer_id = cid
        await db.commit()
        return cid

    async def create_checkout(self, db: AsyncSession, profile: Profile, plan: str, cycle: Cycle) -> str:
        price = self.settings.stripe_prices.get((plan, cycle))
        if not price:
            raise BillingUnavailable(f"The {plan} {cycle} plan isn't available for checkout yet.")
        if profile.stripe_subscription_id and profile.subscription_status in ACTIVE_STATUSES:
            raise Conflict(
                "You already have a subscription. Use “Manage billing” to change plans.",
                code="already_subscribed",
            )
        customer = await self._ensure_customer(db, profile)
        base = f"{self.settings.dashboard_url.rstrip('/')}/settings?tab=Subscription"
        params: dict[str, Any] = {
            "mode": "subscription",
            "customer": customer,
            "client_reference_id": str(profile.id),
            # No payment_method_types: Stripe picks eligible methods dynamically.
            "line_items": [{"price": price, "quantity": 1}],
            "subscription_data": {"metadata": {"user_id": str(profile.id), "plan": plan, "cycle": cycle}},
            "metadata": {"user_id": str(profile.id)},
            "success_url": f"{base}&checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": f"{base}&checkout=cancelled",
        }
        # Same user + plan within a minute → same session (protects against double clicks).
        key = f"checkout:{profile.id}:{plan}:{cycle}:{int(time.time() // 60)}"
        return await run_in_threadpool(self.gateway.create_checkout_session, params, key)

    async def create_portal(self, db: AsyncSession, profile: Profile) -> str:
        if not profile.stripe_customer_id:
            raise BillingUnavailable("You don't have a billing account yet.", code="no_billing_account")
        return_url = f"{self.settings.dashboard_url.rstrip('/')}/settings?tab=Subscription"
        return await run_in_threadpool(
            self.gateway.create_portal_session, customer=profile.stripe_customer_id, return_url=return_url
        )

    # ---- Webhooks

    def _verify(self, payload: bytes, signature: str | None) -> dict[str, Any]:
        if not signature:
            raise InvalidSignature("Missing Stripe-Signature header.")
        try:
            event = stripe.Webhook.construct_event(payload, signature, self.settings.stripe_webhook_secret)
        except (stripe.SignatureVerificationError, ValueError) as e:
            raise InvalidSignature("Webhook signature verification failed.") from e
        data: dict[str, Any] = event.to_dict()
        if data.get("livemode"):
            raise InvalidSignature(
                "Live-mode events are rejected (test mode only).", code="live_event_rejected"
            )
        return data

    async def handle_webhook(self, db: AsyncSession, payload: bytes, signature: str | None) -> bool:
        """Returns False if the event was already processed (duplicate delivery)."""
        event = self._verify(payload, signature)
        return await apply_event(db, event, self.price_to_plan, self.settings.default_plan)


# ---------------------------------------------------------------------------- event logic


def _profile_lookup_keys(obj: dict[str, Any]) -> tuple[uuid.UUID | None, str | None]:
    meta = obj.get("metadata") or {}
    raw = meta.get("user_id") or obj.get("client_reference_id")
    try:
        uid = uuid.UUID(raw) if raw else None
    except ValueError:
        uid = None
    customer = obj.get("customer")
    return uid, customer if isinstance(customer, str) else None


async def _find_profile(db: AsyncSession, obj: dict[str, Any]) -> Profile | None:
    uid, customer = _profile_lookup_keys(obj)
    if uid and (p := await db.get(Profile, uid)):
        return p
    if customer:
        return await db.scalar(select(Profile).where(Profile.stripe_customer_id == customer))
    return None


def _subscription_period_start(sub: dict[str, Any]) -> int | None:
    items = (sub.get("items") or {}).get("data") or []
    # Newer API versions put the period on the item; older ones on the subscription.
    return (items[0].get("current_period_start") if items else None) or sub.get("current_period_start")


async def apply_event(
    db: AsyncSession, event: dict[str, Any], price_to_plan: dict[str, tuple[str, str]], default_plan: str
) -> bool:
    """Apply one verified Stripe event. Idempotent by event id. Commits."""
    try:
        db.add(StripeEvent(id=event["id"], type=event["type"]))
        await db.flush()
    except IntegrityError:
        await db.rollback()
        return False

    etype: str = event["type"]
    obj: dict[str, Any] = event["data"]["object"]
    profile = await _find_profile(db, obj)
    if profile is None:
        log.warning("stripe event %s (%s): no matching profile", event["id"], etype)
        await db.commit()
        return True

    if etype == "checkout.session.completed" and obj.get("mode") == "subscription":
        if isinstance(obj.get("customer"), str):
            profile.stripe_customer_id = obj["customer"]
        if isinstance(obj.get("subscription"), str):
            profile.stripe_subscription_id = obj["subscription"]
        db.add(
            AuditLog(
                target_user_id=profile.id, action="billing.checkout_completed", details={"event": event["id"]}
            )
        )

    elif etype.startswith("customer.subscription."):
        _apply_subscription(db, profile, event, obj, price_to_plan, default_plan)

    elif etype in ("invoice.paid", "invoice.payment_failed"):
        sub_id = (obj.get("parent") or {}).get("subscription_details", {}).get("subscription") or obj.get(
            "subscription"
        )
        if sub_id and sub_id == profile.stripe_subscription_id:
            profile.subscription_status = "active" if etype == "invoice.paid" else "past_due"
        db.add(AuditLog(target_user_id=profile.id, action=f"billing.{etype}", details={"event": event["id"]}))

    await db.commit()
    return True


def _apply_subscription(
    db: AsyncSession,
    profile: Profile,
    event: dict[str, Any],
    sub: dict[str, Any],
    price_to_plan: dict[str, tuple[str, str]],
    default_plan: str,
) -> None:
    status = sub.get("status")
    ended = event["type"] == "customer.subscription.deleted" or status in ENDED_STATUSES
    # A different, older subscription ending must not cancel the current one.
    if ended and profile.stripe_subscription_id and sub["id"] != profile.stripe_subscription_id:
        return

    old_plan = profile.plan
    if ended:
        profile.plan = default_plan
        profile.subscription_status = status or "canceled"
        profile.stripe_subscription_id = None
        profile.billing_cycle = None
    elif status in ACTIVE_STATUSES:
        items = (sub.get("items") or {}).get("data") or []
        price_id = items[0]["price"]["id"] if items else None
        mapped = price_to_plan.get(price_id or "")
        if not mapped:
            log.warning("subscription %s uses unknown price %s — plan unchanged", sub["id"], price_id)
            return
        plan, cycle = mapped
        profile.plan = plan
        profile.billing_cycle = cycle
        profile.subscription_status = status
        profile.stripe_subscription_id = sub["id"]
        if isinstance(sub.get("customer"), str):
            profile.stripe_customer_id = sub["customer"]
        # Credits follow the subscription period once a subscription exists (master prompt §Phase 2).
        if (ps := _subscription_period_start(sub)) is not None:
            start = datetime.fromtimestamp(ps, UTC)
            if period_start(profile.credit_anchor) != start.date():
                profile.credit_anchor = start
    else:  # incomplete — payment not confirmed yet; don't grant the plan
        profile.subscription_status = status
        return

    if profile.plan != old_plan or ended:
        db.add(
            AuditLog(
                target_user_id=profile.id,
                action="billing.plan",
                details={
                    "from": old_plan,
                    "to": profile.plan,
                    "status": profile.subscription_status,
                    "event": event["id"],
                },
            )
        )


@lru_cache
def _stripe_adapter() -> StripeTestBillingAdapter:
    s = get_settings()
    return StripeTestBillingAdapter(s, StripeSdkGateway(s))


def get_billing_adapter() -> BillingAdapter:
    if get_settings().billing_provider == "stripe_test":
        return _stripe_adapter()
    return MockBillingAdapter()
