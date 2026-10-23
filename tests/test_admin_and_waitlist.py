from sqlalchemy import update

from app.models import Profile


async def _make_admin(sessionmaker, user) -> None:
    async with sessionmaker() as s:
        await s.execute(update(Profile).where(Profile.id == user.id).values(is_admin=True))
        await s.commit()


async def test_non_admin_is_forbidden(client, alice, bob):
    await client.get("/api/v1/me", headers=bob.headers)
    r = await client.patch(
        f"/api/v1/admin/users/{bob.id}/plan", headers=alice.headers, json={"plan": "titan"}
    )
    assert r.status_code == 403


async def test_admin_plan_credit_and_flag_changes_apply_immediately(client, sessionmaker, alice, bob):
    await client.get("/api/v1/me", headers=alice.headers)
    await client.get("/api/v1/me", headers=bob.headers)
    await _make_admin(sessionmaker, alice)

    r = await client.patch(
        f"/api/v1/admin/users/{bob.id}/plan", headers=alice.headers, json={"plan": "visionary"}
    )
    assert r.status_code == 200 and r.json()["credits"]["remaining"] == 250
    r = await client.post(
        f"/api/v1/admin/users/{bob.id}/credits",
        headers=alice.headers,
        json={"amount": 25, "reason": "beta tester"},
    )
    assert r.json()["credits"]["remaining"] == 275
    r = await client.patch(
        f"/api/v1/admin/users/{bob.id}/flags", headers=alice.headers, json={"mcp_enabled": True}
    )
    assert r.json()["profile"]["mcp_enabled"] is True

    # Bob sees the change on his next request (FR-BILL-09: no redeploy).
    me = (await client.get("/api/v1/me", headers=bob.headers)).json()
    assert me["profile"]["plan"] == "visionary" and me["credits"]["remaining"] == 275

    audit = (await client.get("/api/v1/admin/audit", headers=alice.headers)).json()
    assert {a["action"] for a in audit} == {"plan.set", "credits.adjust", "flags.set"}


async def test_waitlist_is_idempotent_and_visible_to_admin(client, sessionmaker, alice):
    r = await client.post("/api/v1/waitlist", json={"email": "Fan@Example.com", "plan": "visionary"})
    assert r.json() == {"ok": True, "already_joined": False}
    r = await client.post("/api/v1/waitlist", json={"email": "fan@example.com", "plan": "visionary"})
    assert r.json() == {"ok": True, "already_joined": True}
    assert (await client.post("/api/v1/waitlist", json={"email": "nope", "plan": "pro"})).status_code == 422

    await client.get("/api/v1/me", headers=alice.headers)
    await _make_admin(sessionmaker, alice)
    rows = (await client.get("/api/v1/admin/waitlist", headers=alice.headers)).json()
    assert [(w["email"], w["plan"]) for w in rows] == [("fan@example.com", "visionary")]


async def test_waitlist_rate_limit(client):
    codes = [
        (await client.post("/api/v1/waitlist", json={"email": f"u{i}@x.io", "plan": "pro"})).status_code
        for i in range(7)
    ]
    assert codes[:5] == [200] * 5 and codes[5] == 429
