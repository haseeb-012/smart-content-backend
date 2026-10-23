"""Stripe test-mode billing: checkout, portal, signed + idempotent webhooks, plan/credit sync.

Webhook signatures are generated exactly the way Stripe signs them, so the real
`stripe.Webhook.construct_event` verification runs. Outbound API calls use a fake gateway."""

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.config import Settings
from app.modules.plans_credits.billing import StripeTestBillingAdapter, get_billing_adapter

WHSEC = "whsec_test_secret"
PRICES = {"pro_monthly": "price_pro_m", "visionary_monthly": "price_vis_m", "titan_yearly": "price_titan_y"}


class FakeGateway:
    def __init__(self) -> None:
        self.customers: list[dict[str, Any]] = []
        self.checkouts: list[tuple[dict[str, Any], str]] = []

    def create_customer(self, *, email: str | None, user_id: str) -> str:
        self.customers.append({"email": email, "user_id": user_id})
        return f"cus_{len(self.customers)}"

    def create_checkout_session(self, params: dict[str, Any], idempotency_key: str) -> str:
        self.checkouts.append((params, idempotency_key))
        return "https://checkout.stripe.com/c/pay/cs_test_123"

    def create_portal_session(self, *, customer: str, return_url: str) -> str:
        return f"https://billing.stripe.com/p/session/test_{customer}"


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def stripe_app(app, gateway):
    settings = Settings(
        billing_provider="stripe_test",
        stripe_secret_key="rk_test_123",
        stripe_webhook_secret=WHSEC,
        stripe_price_pro_monthly=PRICES["pro_monthly"],
        stripe_price_visionary_monthly=PRICES["visionary_monthly"],
        stripe_price_titan_yearly=PRICES["titan_yearly"],
        dashboard_url="http://localhost:3001",
    )
    app.dependency_overrides[get_billing_adapter] = lambda: StripeTestBillingAdapter(settings, gateway)
    return app


def signed(event: dict[str, Any], secret: str = WHSEC) -> tuple[bytes, dict[str, str]]:
    payload = json.dumps(event).encode()
    t = int(time.time())
    sig = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return payload, {"stripe-signature": f"t={t},v1={sig}", "content-type": "application/json"}


_n = 0


def event(etype: str, obj: dict[str, Any], *, livemode: bool = False) -> dict[str, Any]:
    global _n
    _n += 1
    return {
        "id": f"evt_{_n}",
        "object": "event",
        "type": etype,
        "livemode": livemode,
        "data": {"object": obj},
    }


def subscription(
    user, *, price: str, status: str = "active", sub_id: str = "sub_1", period_start: int | None = None
):
    return {
        "id": sub_id,
        "object": "subscription",
        "customer": "cus_1",
        "status": status,
        "metadata": {"user_id": str(user.id)},
        "items": {
            "data": [{"price": {"id": price}, "current_period_start": period_start or int(time.time())}]
        },
    }


async def post_event(client, ev: dict[str, Any], secret: str = WHSEC):
    payload, headers = signed(ev, secret)
    return await client.post("/api/v1/billing/webhook", content=payload, headers=headers)


# ---------------------------------------------------------------- mock adapter


async def test_mock_billing_has_no_checkout(client, alice):
    r = await client.post("/api/v1/billing/checkout-session", headers=alice.headers, json={"plan": "pro"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "billing_unavailable"
    me = (await client.get("/api/v1/me", headers=alice.headers)).json()
    assert me["billing"] == {
        "provider": "mock",
        "checkout_available": False,
        "test_mode": True,
        "subscription_status": "",
        "billing_cycle": "",
        "has_billing_account": False,
    }


# ---------------------------------------------------------------- checkout / portal


async def test_checkout_creates_customer_once_and_hosted_session(stripe_app, client, gateway, alice):
    r = await client.post(
        "/api/v1/billing/checkout-session", headers=alice.headers, json={"plan": "visionary"}
    )
    assert r.status_code == 200
    assert r.json()["url"].startswith("https://checkout.stripe.com/")
    params, key = gateway.checkouts[0]
    assert params["mode"] == "subscription"
    assert "payment_method_types" not in params  # dynamic payment methods
    assert params["line_items"] == [{"price": "price_vis_m", "quantity": 1}]
    assert params["client_reference_id"] == str(alice.id)
    assert params["subscription_data"]["metadata"]["user_id"] == str(alice.id)
    assert params["success_url"].startswith(
        "http://localhost:3001/settings?tab=Subscription&checkout=success"
    )
    assert key.startswith(f"checkout:{alice.id}:visionary:monthly:")

    await client.post("/api/v1/billing/checkout-session", headers=alice.headers, json={"plan": "pro"})
    assert len(gateway.customers) == 1  # customer reused


async def test_checkout_rejects_unconfigured_price(stripe_app, client, alice):
    r = await client.post(
        "/api/v1/billing/checkout-session", headers=alice.headers, json={"plan": "titan", "cycle": "monthly"}
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "billing_unavailable"


async def test_portal_requires_billing_account(stripe_app, client, alice):
    r = await client.post("/api/v1/billing/portal-session", headers=alice.headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_billing_account"
    await client.post("/api/v1/billing/checkout-session", headers=alice.headers, json={"plan": "pro"})
    r = await client.post("/api/v1/billing/portal-session", headers=alice.headers)
    assert r.json()["url"] == "https://billing.stripe.com/p/session/test_cus_1"


# ---------------------------------------------------------------- webhooks


async def test_webhook_rejects_bad_signature_and_live_events(stripe_app, client, alice):
    await client.get("/api/v1/me", headers=alice.headers)
    ev = event("customer.subscription.created", subscription(alice, price="price_vis_m"))
    r = await post_event(client, ev, secret="whsec_wrong")
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_signature"
    r = await client.post("/api/v1/billing/webhook", content=b"{}")
    assert r.status_code == 400
    live = event("customer.subscription.created", subscription(alice, price="price_vis_m"), livemode=True)
    r = await post_event(client, live)
    assert r.status_code == 400 and r.json()["error"]["code"] == "live_event_rejected"
    assert (await client.get("/api/v1/me", headers=alice.headers)).json()["profile"]["plan"] == "pro"


async def test_full_subscription_lifecycle(stripe_app, client, alice, sessionmaker):
    await client.post("/api/v1/billing/checkout-session", headers=alice.headers, json={"plan": "visionary"})

    done = event(
        "checkout.session.completed",
        {
            "object": "checkout.session",
            "mode": "subscription",
            "customer": "cus_1",
            "subscription": "sub_1",
            "client_reference_id": str(alice.id),
        },
    )
    assert (await post_event(client, done)).json() == {"received": True, "duplicate": False}

    # Subscription becomes active → plan + credits switch on the user's next request.
    period = int(datetime(2026, 10, 20, tzinfo=UTC).timestamp())
    created = event(
        "customer.subscription.created", subscription(alice, price="price_vis_m", period_start=period)
    )
    assert (await post_event(client, created)).status_code == 200
    me = (await client.get("/api/v1/me", headers=alice.headers)).json()
    assert me["profile"]["plan"] == "visionary"
    assert me["credits"]["allowance"] == 250 and me["credits"]["period_start"] == "2026-10-20"

    # Stripe retries the same event: acknowledged, not re-applied.
    assert (await post_event(client, created)).json() == {"received": True, "duplicate": True}

    # Upgrade via portal.
    up = event(
        "customer.subscription.updated", subscription(alice, price="price_titan_y", period_start=period)
    )
    await post_event(client, up)
    assert (await client.get("/api/v1/me", headers=alice.headers)).json()["profile"]["plan"] == "titan"

    # Failed renewal keeps the plan but marks the subscription past_due.
    failed = event(
        "invoice.payment_failed", {"object": "invoice", "customer": "cus_1", "subscription": "sub_1"}
    )
    await post_event(client, failed)

    # A stale, different subscription ending doesn't cancel the current one.
    other = event(
        "customer.subscription.deleted",
        subscription(alice, price="price_pro_m", sub_id="sub_old", status="canceled"),
    )
    await post_event(client, other)
    assert (await client.get("/api/v1/me", headers=alice.headers)).json()["profile"]["plan"] == "titan"

    # Cancellation → back to the default plan.
    gone = event(
        "customer.subscription.deleted", subscription(alice, price="price_titan_y", status="canceled")
    )
    await post_event(client, gone)
    me = (await client.get("/api/v1/me", headers=alice.headers)).json()
    assert me["profile"]["plan"] == "pro" and me["credits"]["allowance"] == 100


async def test_incomplete_subscription_does_not_grant_plan(stripe_app, client, alice):
    await client.get("/api/v1/me", headers=alice.headers)
    ev = event("customer.subscription.created", subscription(alice, price="price_vis_m", status="incomplete"))
    await post_event(client, ev)
    assert (await client.get("/api/v1/me", headers=alice.headers)).json()["profile"]["plan"] == "pro"


async def test_cannot_start_second_subscription(stripe_app, client, alice):
    await client.post("/api/v1/billing/checkout-session", headers=alice.headers, json={"plan": "pro"})
    await post_event(client, event("customer.subscription.created", subscription(alice, price="price_pro_m")))
    r = await client.post(
        "/api/v1/billing/checkout-session", headers=alice.headers, json={"plan": "visionary"}
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "already_subscribed"


def test_stripe_mode_requires_test_keys():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(billing_provider="stripe_test", stripe_secret_key="", stripe_webhook_secret=WHSEC)
    with pytest.raises(ValidationError):
        Settings(billing_provider="stripe_test", stripe_secret_key="rk_live_1", stripe_webhook_secret=WHSEC)
    with pytest.raises(ValidationError):
        Settings(billing_provider="stripe_test", stripe_secret_key="rk_test_1", stripe_webhook_secret="")
