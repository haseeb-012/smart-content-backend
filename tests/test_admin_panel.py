"""Admin panel: env-defined login, admin guard, overview/list endpoints, hooks, audit attribution."""

import pytest
from sqlalchemy import update

from app.api.v1.admin import login_fail_limiter, login_ip_limiter
from app.api.v1.channels import get_session_factory
from app.core.admin_auth import hash_password, verify_password
from app.core.config import get_settings
from app.models import HookTemplate, Profile

ADMIN = "panel-admin@example.com"
PASSWORD = "correct horse battery"


@pytest.fixture
def panel(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAIL", ADMIN)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password(PASSWORD, iterations=1000))
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "x" * 48)
    get_settings.cache_clear()
    login_ip_limiter.reset()
    login_fail_limiter.reset()
    yield
    get_settings.cache_clear()


async def _login(client, email=ADMIN, password=PASSWORD):
    return await client.post("/api/v1/admin/login", json={"email": email, "password": password})


async def _panel_headers(client) -> dict[str, str]:
    r = await _login(client)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_password_hash_roundtrip():
    h = hash_password("sample-pass-1", iterations=1000)
    assert h.startswith("pbkdf2_sha256$") and "sample-pass-1" not in h
    assert verify_password("sample-pass-1", h) and not verify_password("sample-pass-2", h)
    assert not verify_password("sample-pass-1", "garbage")


async def test_login_disabled_without_env(client):
    r = await _login(client)
    assert r.status_code == 404 and r.json()["error"]["code"] == "admin_login_disabled"


async def test_login_wrong_and_right(client, panel):
    r = await _login(client, password="nope")
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"
    r = await _login(client, email="PANEL-ADMIN@example.com ")  # email is case/space-insensitive
    assert r.status_code == 200 and r.json()["token"] and r.json()["source"] == "panel"
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    s = (await client.get("/api/v1/admin/session", headers=h)).json()
    assert s == {"token": None, "expires_at": None, "email": ADMIN, "source": "panel"}


async def test_login_is_rate_limited(client, panel):
    for _ in range(5):
        await _login(client, password="nope")
    r = await _login(client)  # even the right password is blocked for this IP now
    assert r.status_code == 429


async def test_panel_token_is_admin_only(client, panel):
    h = await _panel_headers(client)
    # Not a user session: normal app routes reject it.
    assert (await client.get("/api/v1/me", headers=h)).status_code == 401
    assert (await client.get("/api/v1/admin/stats")).status_code == 401
    # A tampered token is rejected.
    bad = {"Authorization": h["Authorization"][:-4] + "abcd"}
    assert (await client.get("/api/v1/admin/stats", headers=bad)).status_code == 401


async def test_old_sessions_end_when_admin_email_changes(client, panel, monkeypatch):
    h = await _panel_headers(client)
    monkeypatch.setenv("ADMIN_EMAIL", "other-admin@example.com")
    get_settings.cache_clear()
    assert (await client.get("/api/v1/admin/session", headers=h)).status_code == 401


async def test_supabase_admins_still_work_and_others_are_forbidden(client, sessionmaker, alice, bob):
    await client.get("/api/v1/me", headers=alice.headers)
    async with sessionmaker() as s:
        await s.execute(update(Profile).where(Profile.id == alice.id).values(is_admin=True))
        await s.commit()
    assert (await client.get("/api/v1/admin/stats", headers=alice.headers)).status_code == 200
    assert (await client.get("/api/v1/admin/session", headers=alice.headers)).json()["source"] == "supabase"
    assert (await client.get("/api/v1/admin/stats", headers=bob.headers)).status_code == 403


async def test_panel_manages_users_and_audit_shows_panel_admin(client, panel, bob):
    await client.get("/api/v1/me", headers=bob.headers)  # creates Bob's profile
    h = await _panel_headers(client)

    users = (await client.get("/api/v1/admin/users", headers=h, params={"q": "bob"})).json()
    assert users["total"] == 1 and users["items"][0]["profile"]["email"] == "bob@example.com"
    assert (await client.get("/api/v1/admin/users", headers=h, params={"plan": "titan"})).json()["total"] == 0

    uid = str(bob.id)
    r = await client.patch(f"/api/v1/admin/users/{uid}/flags", headers=h, json={"mcp_enabled": True})
    assert r.status_code == 200 and r.json()["profile"]["mcp_enabled"] is True
    r = await client.patch(f"/api/v1/admin/users/{uid}/plan", headers=h, json={"plan": "titan"})
    assert r.json()["profile"]["plan"] == "titan"
    r = await client.post(f"/api/v1/admin/users/{uid}/credits", headers=h, json={"amount": 25, "reason": "support"})
    assert r.json()["credits"]["adjustments"] == 25

    usage = (await client.get(f"/api/v1/admin/users/{uid}/usage", headers=h)).json()
    assert usage["ledger"][0]["amount"] == 25 and usage["workspaces"] == [] and usage["analyses"] == 0

    audit = (await client.get("/api/v1/admin/audit", headers=h)).json()
    assert {a["action"] for a in audit} == {"flags.set", "plan.set", "credits.adjust"}
    assert all(a["actor"] == "Admin (panel login)" and a["target"] == "bob@example.com" for a in audit)


async def test_overview_lists_and_system(client, app, sessionmaker, panel, alice):
    app.dependency_overrides[get_session_factory] = lambda: sessionmaker
    h = await _panel_headers(client)
    ws = (await client.post("/api/v1/workspaces", headers=alice.headers, json={"name": "W"})).json()["id"]
    r = await client.post(
        f"/api/v1/workspaces/{ws}/channels", headers=alice.headers, json={"platform": "tiktok", "handle": "demo"}
    )
    assert r.status_code == 201

    stats = (await client.get("/api/v1/admin/stats", headers=h)).json()
    assert stats["users"]["total"] == 1 and stats["content"]["workspaces"] == 1
    assert stats["content"]["channels"] == 1
    assert len(stats["signups_by_day"]) == 30 and stats["signups_by_day"][-1]["value"] == 1

    chans = (await client.get("/api/v1/admin/channels", headers=h)).json()
    assert chans["total"] == 1 and chans["items"][0]["workspaces"] == 1
    cid = chans["items"][0]["id"]
    assert (await client.post(f"/api/v1/admin/channels/{cid}/resync", headers=h)).status_code == 202

    vids = (await client.get("/api/v1/admin/videos", headers=h, params={"platform": "tiktok"})).json()
    assert vids and all(v["platform"] == "tiktok" for v in vids)
    scores = [v["outlier_score"] or 0 for v in vids]
    assert scores == sorted(scores, reverse=True)

    assert (await client.get("/api/v1/admin/analyses", headers=h)).json() == {"items": [], "total": 0}
    system = (await client.get("/api/v1/admin/system", headers=h)).json()
    assert system["database"] == "ok" and system["providers"]["channels"] == "fake"
    assert system["apify_usage"] is None
    assert all(isinstance(v, bool) for v in system["keys"].values())  # never key values
    bill = (await client.get("/api/v1/admin/billing", headers=h)).json()
    assert bill["provider"] == "mock" and bill["subscriptions"] == [] and bill["events"] == []


async def test_hooks_admin_list_and_unpublish(client, sessionmaker, panel):
    async with sessionmaker() as s:
        s.add(HookTemplate(text="Stop doing this now", category="contrarian"))
        await s.commit()
    h = await _panel_headers(client)
    hooks = (await client.get("/api/v1/admin/hooks", headers=h)).json()
    assert len(hooks) == 1 and hooks[0]["is_published"] is True
    r = await client.patch(f"/api/v1/admin/hooks/{hooks[0]['id']}", headers=h, json={"is_published": False})
    assert r.status_code == 200 and r.json()["is_published"] is False
    assert (await client.get("/api/v1/hooks")).json() == []  # hidden from the public library
    assert len((await client.get("/api/v1/admin/hooks", headers=h)).json()) == 1


async def test_panel_can_curate_collections(client, panel):
    h = await _panel_headers(client)
    r = await client.post(
        "/api/v1/admin/collections",
        headers=h,
        json={"slug": "hooks-101", "name": "Hooks 101", "category": "formats", "is_published": True},
    )
    assert r.status_code == 201
    r = await client.post(
        f"/api/v1/admin/collections/{r.json()['id']}/videos",
        headers=h,
        json={"platform": "youtube", "url": "https://www.youtube.com/shorts/abc123", "title": "Example"},
    )
    assert r.status_code == 201 and len(r.json()["videos"]) == 1


async def test_duplicate_hook_is_a_clean_conflict(client, panel):
    h = await _panel_headers(client)
    body = {"text": "Nobody tells you this", "category": "curiosity_gap"}
    assert (await client.post("/api/v1/admin/hooks", headers=h, json=body)).status_code == 201
    r = await client.post("/api/v1/admin/hooks", headers=h, json=body)
    assert r.status_code == 409 and r.json()["error"]["code"] == "duplicate_hook"
