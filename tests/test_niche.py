"""'Describe your niche' search: AI suggestions are only returned if the account really exists."""

from app.api.v1.channels import get_session_factory, niche_limiter
from app.modules.discovery_analysis.llm import FakeLLMProvider, get_llm_provider


class MixedLLM(FakeLLMProvider):
    async def suggest_creators(self, description, platform):
        return ["@Real.Creator", "ghost.notfound", "bad handle!", "real.creator"]


async def test_niche_search_verifies_suggestions(app, client, alice, sessionmaker):
    app.dependency_overrides[get_session_factory] = lambda: sessionmaker
    app.dependency_overrides[get_llm_provider] = lambda: MixedLLM()
    niche_limiter.reset()
    ws = (await client.post("/api/v1/workspaces", headers=alice.headers, json={"name": "W"})).json()["id"]
    r = await client.post(
        f"/api/v1/workspaces/{ws}/channels/niche-search",
        headers=alice.headers,
        json={"description": "budget travel for students", "platform": "tiktok"},
    )
    assert r.status_code == 200
    assert [(x["platform"], x["handle"]) for x in r.json()] == [
        ("tiktok", "real.creator")
    ]  # invented one dropped

    # Track it, then the same search marks it as tracked.
    await client.post(
        f"/api/v1/workspaces/{ws}/channels",
        headers=alice.headers,
        json={"platform": "tiktok", "handle": "real.creator"},
    )
    r = await client.post(
        f"/api/v1/workspaces/{ws}/channels/niche-search",
        headers=alice.headers,
        json={"description": "budget travel for students", "platform": "tiktok"},
    )
    assert r.json()[0]["tracked"] is True and r.json()[0]["channel_id"]


async def test_niche_search_all_platforms_and_validation(app, client, alice, sessionmaker):
    app.dependency_overrides[get_session_factory] = lambda: sessionmaker
    niche_limiter.reset()
    ws = (await client.post("/api/v1/workspaces", headers=alice.headers, json={"name": "W"})).json()["id"]
    r = await client.post(
        f"/api/v1/workspaces/{ws}/channels/niche-search",
        headers=alice.headers,
        json={"description": "home fitness coach"},
    )
    assert r.status_code == 200 and {x["platform"] for x in r.json()} == {"tiktok", "instagram", "youtube"}
    r = await client.post(
        f"/api/v1/workspaces/{ws}/channels/niche-search", headers=alice.headers, json={"description": "x"}
    )
    assert r.status_code == 422
