"""Social verification (bio code) + own-account analytics + daily channel snapshots."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.api.v1.channels import get_session_factory
from app.core.config import Settings
from app.models import Channel, ChannelStat, Profile, SocialAccount
from app.modules.discovery_analysis.providers import ApifyChannelProvider
from app.modules.identity import social as svc


@pytest.fixture
def so_app(app, sessionmaker):
    app.dependency_overrides[get_session_factory] = lambda: sessionmaker
    return app


async def _start(client, user, handle="mycreator", platform="tiktok"):
    r = await client.post(
        "/api/v1/me/social", headers=user.headers, json={"platform": platform, "handle": handle}
    )
    assert r.status_code == 201
    return r.json()


async def _allow_recheck(db, account_id):
    acc = await db.get(SocialAccount, account_id)
    acc.last_checked_at = datetime.now(UTC) - timedelta(minutes=5)
    await db.commit()


def test_codes_are_random_and_readable():
    codes = {svc.new_code() for _ in range(200)}
    assert len(codes) == 200
    assert all(c.startswith("SC-") and len(c) == 9 and not set(c[3:]) & set("01IO") for c in codes)


async def test_verify_flow_imports_account_and_shows_analytics(so_app, client, alice, db):
    acc = await _start(client, alice, "@MyCreator")
    assert acc["status"] == "pending" and acc["handle"] == "mycreator" and acc["code"].startswith("SC-")
    again = await _start(client, alice, "mycreator")
    assert again["id"] == acc["id"] and again["code"] == acc["code"]  # idempotent, same code

    r = await client.post(f"/api/v1/me/social/{acc['id']}/verify", headers=alice.headers)
    assert r.status_code == 200
    v = r.json()
    assert v["status"] == "verified" and v["channel"]["handle"] == "mycreator"

    # Background sync imported videos and wrote today's snapshot.
    stats = (await db.scalars(select(ChannelStat))).all()
    assert len(stats) == 1 and stats[0].video_count == 60 and stats[0].followers

    a = (await client.get("/api/v1/me/analytics?days=90", headers=alice.headers)).json()
    assert len(a["accounts"]) == 1 and a["total_followers"] > 0
    assert (
        len(a["views_by_day"]) == 90
        and a["posts"] > 0
        and a["views"] == sum(p["value"] for p in a["views_by_day"])
    )
    assert len(a["recent_videos"]) == 6 and a["recent_videos"][0]["channel"]["handle"] == "mycreator"
    assert a["followers_gained"] is None  # one snapshot so far

    # Verified twice → no-op.
    r = await client.post(f"/api/v1/me/social/{acc['id']}/verify", headers=alice.headers)
    assert r.status_code == 200


async def test_followers_gained_from_daily_snapshots(so_app, client, alice, db):
    acc = await _start(client, alice)
    await client.post(f"/api/v1/me/social/{acc['id']}/verify", headers=alice.headers)
    ch = (await db.scalars(select(Channel))).one()
    today = datetime.now(UTC).date()
    snap = (await db.scalars(select(ChannelStat))).one()
    snap.followers = 1500
    db.add_all(
        [
            ChannelStat(
                channel_id=ch.id, captured_on=today - timedelta(days=2), followers=1000, video_count=60
            ),
            ChannelStat(
                channel_id=ch.id, captured_on=today - timedelta(days=1), followers=1200, video_count=60
            ),
        ]
    )
    await db.commit()
    a = (await client.get("/api/v1/me/analytics?days=7", headers=alice.headers)).json()
    assert a["followers_gained"] == 500
    assert [p["value"] for p in a["followers_by_day"][-3:]] == [0, 200, 300]  # first day has no prior → 0


async def test_code_missing_and_cooldown(so_app, client, alice, db):
    acc = await _start(client, alice, "creator.nocode")
    r = await client.post(f"/api/v1/me/social/{acc['id']}/verify", headers=alice.headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "code_not_found"
    assert acc["code"] in r.json()["error"]["message"]
    r = await client.post(f"/api/v1/me/social/{acc['id']}/verify", headers=alice.headers)
    assert r.status_code == 429  # checked seconds ago
    acc2 = await _start(client, alice, "ghost.notfound")
    r = await client.post(f"/api/v1/me/social/{acc2['id']}/verify", headers=alice.headers)
    assert r.status_code == 404


async def test_plan_limit_on_verified_accounts(so_app, client, alice, db):
    await client.get("/api/v1/me", headers=alice.headers)  # pro = 3 verified
    for i in range(3):
        a = await _start(client, alice, f"acct{i}")
        assert (
            await client.post(f"/api/v1/me/social/{a['id']}/verify", headers=alice.headers)
        ).status_code == 200
    r = await client.post(
        "/api/v1/me/social", headers=alice.headers, json={"platform": "tiktok", "handle": "acct4"}
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "plan_limit"


async def test_handle_verified_by_one_user_only_and_isolation(so_app, client, alice, bob, db):
    a = await _start(client, alice, "shared")
    await client.post(f"/api/v1/me/social/{a['id']}/verify", headers=alice.headers)
    b = await _start(client, bob, "shared")
    r = await client.post(f"/api/v1/me/social/{b['id']}/verify", headers=bob.headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "conflict"

    # Bob can't see, verify or delete Alice's claim.
    assert [x["handle"] for x in (await client.get("/api/v1/me/social", headers=bob.headers)).json()] == [
        "shared"
    ]
    assert (await client.post(f"/api/v1/me/social/{a['id']}/verify", headers=bob.headers)).status_code == 404
    assert (await client.delete(f"/api/v1/me/social/{a['id']}", headers=bob.headers)).status_code == 404
    bob_analytics = (await client.get("/api/v1/me/analytics", headers=bob.headers)).json()
    assert bob_analytics["accounts"] == [] and bob_analytics["recent_videos"] == []

    assert (await client.delete(f"/api/v1/me/social/{a['id']}", headers=alice.headers)).status_code == 204
    assert (await client.get("/api/v1/me/analytics", headers=alice.headers)).json()["accounts"] == []


async def test_analytics_days_validated(so_app, client, alice):
    assert (await client.get("/api/v1/me/analytics?days=12", headers=alice.headers)).status_code == 422


async def test_apify_bio_check_reads_platform_fields():
    def handler(req: httpx.Request) -> httpx.Response:
        body = req.read().decode()
        if "instagram" in str(req.url):
            return httpx.Response(200, json=[{"username": "a", "biography": "Coach | sc-abc234"}])
        if "tiktok" in str(req.url):
            return httpx.Response(200, json=[{"authorMeta": {"signature": "hello"}}])
        assert "channels" in body
        return httpx.Response(200, json=[])

    p = ApifyChannelProvider(
        Settings(channel_provider="apify", apify_token="t"),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert await p.bio_has_code("instagram", "a", "SC-ABC234") is True  # case-insensitive
    assert await p.bio_has_code("tiktok", "a", "SC-ABC234") is False
    assert await p.bio_has_code("youtube", "a", "SC-ABC234") is None  # nothing returned


async def test_verified_channels_are_refreshed_daily(so_app, client, alice, db):
    from app.modules.discovery_analysis.channels import channels_due_for_refresh

    acc = await _start(client, alice)
    await client.post(f"/api/v1/me/social/{acc['id']}/verify", headers=alice.headers)
    ch = (await db.scalars(select(Channel))).one()
    ch.last_synced_at = datetime.now(UTC) - timedelta(days=2)
    await db.commit()
    assert ch.id in await channels_due_for_refresh(db, timedelta(hours=24))
    assert (await db.get(Profile, alice.id)) is not None
