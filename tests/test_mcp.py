"""MCP server: OAuth discovery, gating, JSON-RPC protocol, tools, permissions, credits."""

import asyncio

import pytest

from app.api.mcp import mcp_limiter
from app.api.v1.channels import get_session_factory, niche_limiter, search_limiter
from app.models import Profile
from app.modules.mcp.tools import TOOL_NAMES, _background


@pytest.fixture
def mcp_app(app, sessionmaker, monkeypatch):
    app.dependency_overrides[get_session_factory] = lambda: sessionmaker
    for lim in (mcp_limiter, search_limiter, niche_limiter):
        lim.reset()
    # Tests poll while background imports run; slow CI machines must not trip the per-minute limit.
    monkeypatch.setattr(mcp_limiter, "limit", 10_000)
    return app


async def _enable(client, user, db, on=True):
    await client.get("/api/v1/me", headers=user.headers)
    p = await db.get(Profile, user.id)
    p.mcp_enabled = on
    await db.commit()


async def rpc(client, user, method, params=None, id_=1):
    r = await client.post(
        "/mcp",
        headers=user.headers,
        json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}},
    )
    return r


async def call(client, user, name, args=None):
    r = await rpc(client, user, "tools/call", {"name": name, "arguments": args or {}})
    assert r.status_code == 200, r.text
    if _background:  # let channels-add's background import finish before the next call
        await asyncio.gather(*list(_background))
    return r.json()["result"]


async def test_protected_resource_metadata(mcp_app, client):
    for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
        m = (await client.get(path)).json()
        assert m["resource"].endswith("/mcp") and m["bearer_methods_supported"] == ["header"]


async def test_unauthenticated_gets_oauth_challenge(mcp_app, client):
    r = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r.status_code == 401
    assert 'resource_metadata="' in r.headers["www-authenticate"]
    r = await client.post("/mcp", headers={"Authorization": "Bearer nope"}, json={})
    assert r.status_code == 401 and "invalid_token" in r.headers["www-authenticate"]
    assert (await client.get("/mcp")).status_code == 405


async def test_requires_early_access_flag(mcp_app, client, alice, db):
    await _enable(client, alice, db, on=False)
    r = await rpc(client, alice, "initialize")
    assert r.status_code == 403 and r.json()["error"]["code"] == "mcp_not_enabled"


async def test_handshake_and_tool_list(mcp_app, client, alice, db):
    await _enable(client, alice, db)
    r = await rpc(
        client,
        alice,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}},
    )
    res = r.json()["result"]
    assert (
        res["protocolVersion"] == "2025-06-18"
        and res["capabilities"]["tools"]
        and res["serverInfo"]["name"] == "smart-content"
    )
    r = await rpc(client, alice, "initialize", {"protocolVersion": "1999-01-01"})
    assert r.json()["result"]["protocolVersion"] == "2025-11-25"
    n = await client.post(
        "/mcp", headers=alice.headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert n.status_code == 202
    assert (await rpc(client, alice, "ping")).json()["result"] == {}
    tools = (await rpc(client, alice, "tools/list")).json()["result"]["tools"]
    assert [t["name"] for t in tools] == TOOL_NAMES
    assert all(
        t["inputSchema"]["type"] == "object" and "workspace" in t["inputSchema"]["properties"] for t in tools
    )
    assert (await rpc(client, alice, "nope/method")).json()["error"]["code"] == -32601


async def test_tools_end_to_end_with_credit_rules(mcp_app, client, alice, db):
    await _enable(client, alice, db)
    res = await call(client, alice, "videos-watchlist")
    assert res["isError"] is True and "workspace" in res["content"][0]["text"]  # no workspace yet
    await client.post("/api/v1/workspaces", headers=alice.headers, json={"name": "Main"})

    res = await call(client, alice, "channels-add", {"url": "https://www.tiktok.com/@growthgrace"})
    assert res["isError"] is False and res["structuredContent"]["added"] is True
    import asyncio

    for _ in range(100):  # background sync
        recap = (await call(client, alice, "channels-recap"))["structuredContent"]
        if recap["channels"][0]["videos_tracked"]:
            break
        await asyncio.sleep(0.2)
    assert recap["channels"][0]["videos_tracked"] == 60

    vids = (await call(client, alice, "videos-watchlist", {"min_outlier": 5, "limit": 5}))[
        "structuredContent"
    ]
    assert vids["videos"] and all(v["outlier_score"] >= 5 for v in vids["videos"])
    vid = vids["videos"][0]["video_id"]

    a = (await call(client, alice, "analyze", {"video_id": vid}))["structuredContent"]
    assert a["analysis"]["status"] == "complete" and "1 credit" in a["note"]
    again = (await call(client, alice, "analyze", {"video_id": vid}))["structuredContent"]
    assert "no credit" in again["note"]
    assert (await client.get("/api/v1/credits", headers=alice.headers)).json()["used"] == 1

    d = (await call(client, alice, "video-details", {"video_id": vid}))["structuredContent"]
    assert d["analysis"]["hook"]
    f = (await call(client, alice, "formats-watchlist"))["structuredContent"]
    assert len(f["formats"]) == 1
    ideas = (await call(client, alice, "topic", {"topic": "budget travel", "count": 3}))["structuredContent"]
    assert len(ideas["ideas"]) == 3
    assert (
        len(
            (
                await client.get(
                    f"/api/v1/workspaces/{recap and (await client.get('/api/v1/workspaces', headers=alice.headers)).json()[0]['id']}/ideas",
                    headers=alice.headers,
                )
            ).json()
        )
        == 4
    )
    s = (await call(client, alice, "channels-search", {"query": "growthgrace"}))["structuredContent"]
    assert s["results"][0]["tracked"] is True
    s = (await call(client, alice, "channels-search", {"query": "home fitness coach", "platform": "tiktok"}))[
        "structuredContent"
    ]
    assert s["results"]
    assert (await call(client, alice, "hooks-watchlist"))["structuredContent"]["hooks"] == []
    assert (await call(client, alice, "video-suggest", {"topic": "demo"}))["isError"] is False
    bad = await call(client, alice, "video-details", {"video_id": "not-a-uuid"})
    assert bad["isError"] is True


async def test_workspace_selection_and_isolation(mcp_app, client, alice, bob, db):
    await _enable(client, alice, db)
    await _enable(client, bob, db)
    await client.post("/api/v1/workspaces", headers=alice.headers, json={"name": "Main"})
    await client.post("/api/v1/workspaces", headers=alice.headers, json={"name": "Client B"})
    await client.post("/api/v1/workspaces", headers=bob.headers, json={"name": "Bobs"})
    assert (await call(client, alice, "hooks-watchlist", {"workspace": "client b"}))["structuredContent"][
        "workspace"
    ] == "Client B"
    res = await call(client, alice, "hooks-watchlist", {"workspace": "Bobs"})
    assert res["isError"] is True and "Main" in res["content"][0]["text"]

    await call(client, alice, "channels-add", {"platform": "tiktok", "handle": "alicechan"})
    vid = None
    import asyncio

    for _ in range(100):
        v = (await call(client, alice, "videos-watchlist"))["structuredContent"]["videos"]
        if v:
            vid = v[0]["video_id"]
            break
        await asyncio.sleep(0.2)
    assert vid
    leak = await call(client, bob, "video-details", {"video_id": vid})
    assert leak["isError"] is True


async def test_tool_permissions(mcp_app, client, alice, db):
    await _enable(client, alice, db)
    st = (await client.get("/api/v1/me/mcp", headers=alice.headers)).json()
    assert st["enabled"] and st["plan_includes_mcp"] and st["server_url"].endswith("/mcp")
    assert all(t["permission"] == "allow" for t in st["tools"])
    r = await client.patch(
        "/api/v1/me/mcp", headers=alice.headers, json={"permissions": {"analyze": "block"}}
    )
    assert {t["name"]: t["permission"] for t in r.json()["tools"]}["analyze"] == "block"
    names = [t["name"] for t in (await rpc(client, alice, "tools/list")).json()["result"]["tools"]]
    assert "analyze" not in names
    res = await call(client, alice, "analyze", {"video_id": "x"})
    assert res["isError"] is True and "blocked" in res["content"][0]["text"]
    r = await client.patch("/api/v1/me/mcp", headers=alice.headers, json={"permissions": {"rm-rf": "block"}})
    assert r.status_code == 422


async def test_mcp_rate_limit_still_applies(app, client, alice, db):
    mcp_limiter.reset()
    await _enable(client, alice, db)
    statuses = [(await rpc(client, alice, "ping")).status_code for _ in range(mcp_limiter.limit + 1)]
    assert statuses[-1] == 429 and set(statuses[:-1]) == {200}
    mcp_limiter.reset()
