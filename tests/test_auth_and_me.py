import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from tests.conftest import make_token


async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": "0.1.0", "database": "ok"}


async def test_missing_token_is_rejected(client):
    r = await client.get("/api/v1/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(
    "token_kwargs, code",
    [
        ({"exp_in": -10}, "token_expired"),
        ({"aud": "anon"}, "unauthorized"),
        ({"key": ec.generate_private_key(ec.SECP256R1())}, "unauthorized"),  # forged signature
    ],
)
async def test_bad_tokens_are_rejected(client, token_kwargs, code):
    token = make_token(uuid.uuid4(), **token_kwargs)
    r = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == code


async def test_first_request_creates_profile_with_default_credits(client, alice):
    r = await client.get("/api/v1/me", headers=alice.headers)
    assert r.status_code == 200
    body = r.json()
    assert body["profile"]["id"] == str(alice.id)
    assert body["profile"]["email"] == alice.email
    assert body["profile"]["plan"] == "pro"
    assert body["credits"]["remaining"] == 100  # FR-AUTH-03
    assert body["billing"]["test_mode"] is True


async def test_profile_update_cannot_change_email(client, alice):
    r = await client.patch("/api/v1/me", headers=alice.headers, json={"full_name": "Alice", "email": "x@y.z"})
    assert r.status_code == 422  # FR-AUTH-06: email change not supported
    r = await client.patch("/api/v1/me", headers=alice.headers, json={"full_name": "Alice"})
    assert r.status_code == 200
    assert r.json()["profile"]["full_name"] == "Alice"


async def test_plans_are_public(client):
    r = await client.get("/api/v1/plans")
    assert [p["id"] for p in r.json()] == ["pro", "visionary", "titan"]
    assert r.json()[2]["features"] == ["api", "guests", "mcp"]


def test_live_stripe_keys_are_refused():
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(stripe_secret_key="sk_live_123")
    assert Settings(stripe_secret_key="sk_test_123").stripe_secret_key == "sk_test_123"
