"""Deep Analysis: charge-on-success, retries, idempotency, tenant isolation, ideas, Gemini adapter, media safety."""

import base64
import json

import httpx
import pytest

from app.api.v1.analysis import analyze_limiter
from app.api.v1.channels import get_session_factory, search_limiter
from app.core.config import Settings, get_settings
from app.models import CreditLedger
from app.modules.discovery_analysis import llm as llm_mod
from app.modules.discovery_analysis.llm import (
    FakeLLMProvider,
    GeminiLLMProvider,
    VideoContext,
    check_media_url,
    download_media,
    extract_output_text,
    get_llm_provider,
    parse_result,
)
from app.modules.discovery_analysis.providers import ApifyChannelProvider, ProviderError


@pytest.fixture
def an_app(app, sessionmaker):
    app.dependency_overrides[get_session_factory] = lambda: sessionmaker
    search_limiter.reset()
    analyze_limiter.reset()
    return app


class FailingLLM:
    name, model = "failing", "x"

    async def analyze_video(self, ctx):
        raise ProviderError("The AI couldn't process this video. No credit was charged.")


async def _setup(client, user, handle="growthgrace"):
    ws = (await client.post("/api/v1/workspaces", headers=user.headers, json={"name": "WS"})).json()["id"]
    r = await client.post(
        f"/api/v1/workspaces/{ws}/channels",
        headers=user.headers,
        json={"url": f"https://www.tiktok.com/@{handle}"},
    )
    assert r.status_code == 201
    feed = (await client.get(f"/api/v1/workspaces/{ws}/videos", headers=user.headers)).json()
    return ws, feed["items"][0]["id"]


async def _used(client, user) -> int:
    return (await client.get("/api/v1/credits", headers=user.headers)).json()["used"]


async def test_analysis_charges_once_on_success_and_creates_idea(an_app, client, alice):
    ws, vid = await _setup(client, alice)
    detail = (await client.get(f"/api/v1/workspaces/{ws}/videos/{vid}", headers=alice.headers)).json()
    assert detail["analysis"] is None and detail["video"]["id"] == vid

    r = await client.post(f"/api/v1/workspaces/{ws}/videos/{vid}/analysis", headers=alice.headers)
    assert r.status_code == 202 and r.json()["status"] == "queued"

    detail = (await client.get(f"/api/v1/workspaces/{ws}/videos/{vid}", headers=alice.headers)).json()
    a = detail["analysis"]
    assert a["status"] == "complete" and a["provider"] == "fake"
    assert a["summary"] and a["hook"] and a["format"] and a["visual"] and len(a["transcript"]) >= 3
    assert a["transcript"][0]["t"] == "0:00"
    assert detail["idea"]["source"] == "analysis" and detail["idea"]["analysis_id"] == a["id"]
    assert await _used(client, alice) == 1

    # Clicking again is free and returns the stored result.
    r = await client.post(f"/api/v1/workspaces/{ws}/videos/{vid}/analysis", headers=alice.headers)
    assert r.status_code == 200 and r.json()["status"] == "complete"
    assert await _used(client, alice) == 1
    hist = (await client.get("/api/v1/credits/history", headers=alice.headers)).json()["entries"]
    assert [e["action"] for e in hist] == ["deep_analysis"]


async def test_failed_analysis_charges_nothing_and_can_retry(an_app, client, alice):
    ws, vid = await _setup(client, alice)
    an_app.dependency_overrides[get_llm_provider] = lambda: FailingLLM()
    await client.post(f"/api/v1/workspaces/{ws}/videos/{vid}/analysis", headers=alice.headers)
    a = (await client.get(f"/api/v1/workspaces/{ws}/videos/{vid}", headers=alice.headers)).json()["analysis"]
    assert a["status"] == "failed" and "No credit" in a["error"]
    assert await _used(client, alice) == 0

    an_app.dependency_overrides[get_llm_provider] = lambda: FakeLLMProvider()
    r = await client.post(f"/api/v1/workspaces/{ws}/videos/{vid}/analysis", headers=alice.headers)
    assert r.status_code == 202
    d = (await client.get(f"/api/v1/workspaces/{ws}/videos/{vid}", headers=alice.headers)).json()
    assert d["analysis"]["status"] == "complete" and d["analysis"]["error"] is None
    assert await _used(client, alice) == 1


async def test_out_of_credits_blocks_before_starting(an_app, client, alice, db):
    ws, vid = await _setup(client, alice)
    me = (await client.get("/api/v1/me", headers=alice.headers)).json()
    db.add(
        CreditLedger(
            user_id=alice.id,
            kind="adjustment",
            amount=-me["credits"]["remaining"],
            action="test",
            idempotency_key="drain",
            period_key=me["credits"]["period_start"],
        )
    )
    await db.commit()
    r = await client.post(f"/api/v1/workspaces/{ws}/videos/{vid}/analysis", headers=alice.headers)
    assert r.status_code == 402 and r.json()["error"]["code"] == "credits_exhausted"
    d = (await client.get(f"/api/v1/workspaces/{ws}/videos/{vid}", headers=alice.headers)).json()
    assert d["analysis"] is None


async def test_other_workspaces_cannot_see_or_analyze(an_app, client, alice, bob):
    ws, vid = await _setup(client, alice)
    bob_ws = (await client.post("/api/v1/workspaces", headers=bob.headers, json={"name": "B"})).json()["id"]
    for path in (f"/api/v1/workspaces/{ws}/videos/{vid}", f"/api/v1/workspaces/{bob_ws}/videos/{vid}"):
        assert (await client.get(path, headers=bob.headers)).status_code == 404
    r = await client.post(f"/api/v1/workspaces/{bob_ws}/videos/{vid}/analysis", headers=bob.headers)
    assert r.status_code == 404


async def test_analysis_survives_untracking_the_channel(an_app, client, alice):
    ws, vid = await _setup(client, alice)
    await client.post(f"/api/v1/workspaces/{ws}/videos/{vid}/analysis", headers=alice.headers)
    wl = (await client.get(f"/api/v1/workspaces/{ws}/channels", headers=alice.headers)).json()
    await client.delete(f"/api/v1/workspaces/{ws}/channels/{wl[0]['id']}", headers=alice.headers)
    d = await client.get(f"/api/v1/workspaces/{ws}/videos/{vid}", headers=alice.headers)
    assert d.status_code == 200 and d.json()["analysis"]["status"] == "complete"


async def test_daily_cap(an_app, client, alice, monkeypatch):
    monkeypatch.setattr(get_settings(), "analysis_daily_limit", 1)
    ws, vid = await _setup(client, alice)
    feed = (await client.get(f"/api/v1/workspaces/{ws}/videos", headers=alice.headers)).json()["items"]
    await client.post(f"/api/v1/workspaces/{ws}/videos/{vid}/analysis", headers=alice.headers)
    r = await client.post(f"/api/v1/workspaces/{ws}/videos/{feed[1]['id']}/analysis", headers=alice.headers)
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"


async def test_ideas_list_shortlist_delete_and_isolation(an_app, client, alice, bob):
    ws, vid = await _setup(client, alice)
    await client.post(f"/api/v1/workspaces/{ws}/videos/{vid}/analysis", headers=alice.headers)
    ideas = (await client.get(f"/api/v1/workspaces/{ws}/ideas", headers=alice.headers)).json()
    assert len(ideas) == 1 and ideas[0]["shortlisted"] is False
    iid = ideas[0]["id"]

    r = await client.patch(
        f"/api/v1/workspaces/{ws}/ideas/{iid}", headers=alice.headers, json={"shortlisted": True}
    )
    assert r.status_code == 200 and r.json()["shortlisted"] is True
    short = (
        await client.get(f"/api/v1/workspaces/{ws}/ideas?shortlisted=true", headers=alice.headers)
    ).json()
    assert [i["id"] for i in short] == [iid]

    bob_ws = (await client.post("/api/v1/workspaces", headers=bob.headers, json={"name": "B"})).json()["id"]
    assert (
        await client.patch(
            f"/api/v1/workspaces/{bob_ws}/ideas/{iid}", headers=bob.headers, json={"shortlisted": False}
        )
    ).status_code == 404
    assert (
        await client.delete(f"/api/v1/workspaces/{ws}/ideas/{iid}", headers=bob.headers)
    ).status_code == 404

    assert (
        await client.delete(f"/api/v1/workspaces/{ws}/ideas/{iid}", headers=alice.headers)
    ).status_code == 204
    assert (await client.get(f"/api/v1/workspaces/{ws}/ideas", headers=alice.headers)).json() == []


# ------------------------------------------------------------------ Gemini adapter

GOOD = {
    "summary": "S",
    "transcript": [{"t": "00:01", "text": "Hi"}],
    "hook": "H",
    "hook_category": "question",
    "format": "F",
    "visual": "V",
    "idea": "I",
    "idea_draft": {"title": "T", "positioning": "P", "format": "F", "contrast": "C", "personalization": "Z"},
}


def _ctx(**kw):
    base = dict(
        platform="youtube",
        url="https://www.youtube.com/shorts/abc",
        caption="c",
        creator_handle="h",
        duration_sec=30,
        views=10,
        outlier_score=4.0,
    )
    base.update(kw)
    return VideoContext(**base)


def test_extract_output_text_shapes():
    steps = {"steps": [{"type": "model_output", "content": [{"type": "text", "text": '{"a":1}'}]}]}
    assert extract_output_text(steps) == '{"a":1}'
    assert extract_output_text({"output_text": "x"}) == "x"
    assert extract_output_text({"candidates": [{"content": {"parts": [{"text": "y"}]}}]}) == "y"
    assert parse_result("```json\n" + json.dumps(GOOD) + "\n```").hook == "H"
    with pytest.raises(ProviderError):
        parse_result('{"summary": "only"}')


async def test_gemini_youtube_request_and_parse():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"], seen["key"], seen["body"] = (
            req.url.path,
            req.headers["x-goog-api-key"],
            json.loads(req.content),
        )
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "steps": [{"type": "model_output", "content": [{"type": "text", "text": json.dumps(GOOD)}]}],
            },
        )

    g = GeminiLLMProvider(
        Settings(llm_provider="gemini", gemini_api_key="g-test"),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    res = await g.analyze_video(_ctx())
    assert res.summary == "S" and res.idea_draft.title == "T"
    assert seen["path"] == "/v1beta/interactions" and seen["key"] == "g-test"
    assert seen["body"]["model"] == "gemini-3.8-flash"
    assert seen["body"]["input"][0] == {"type": "video", "uri": "https://www.youtube.com/shorts/abc"}
    assert seen["body"]["response_format"]["mime_type"] == "application/json"


async def test_gemini_errors_become_provider_errors():
    for resp in (
        httpx.Response(429, json={"error": {}}),
        httpx.Response(200, json={"status": "failed", "errors": [{"message": "x"}]}),
        httpx.Response(200, json={"status": "completed", "steps": []}),
    ):
        g = GeminiLLMProvider(
            Settings(llm_provider="gemini", gemini_api_key="k"),
            httpx.AsyncClient(transport=httpx.MockTransport(lambda r, resp=resp: resp)),
        )
        with pytest.raises(ProviderError):
            await g.analyze_video(_ctx())


async def test_gemini_tiktok_downloads_media_inline(monkeypatch):
    async def public(host, port):
        return True

    monkeypatch.setattr(llm_mod, "_resolves_public", public)
    sent = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host.endswith("tiktokcdn.com"):
            return httpx.Response(200, content=b"MP4DATA", headers={"content-type": "video/mp4"})
        sent["body"] = json.loads(req.content)
        return httpx.Response(200, json={"status": "completed", "output_text": json.dumps(GOOD)})

    g = GeminiLLMProvider(
        Settings(llm_provider="gemini", gemini_api_key="k"),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await g.analyze_video(
        _ctx(
            platform="tiktok",
            url="https://www.tiktok.com/@h/video/1",
            media_url="https://v16.tiktokcdn.com/v.mp4",
        )
    )
    part = sent["body"]["input"][0]
    assert part["mime_type"] == "video/mp4" and base64.b64decode(part["data"]) == b"MP4DATA"
    with pytest.raises(ProviderError):  # no media link → fail (no charge), never guess
        await g.analyze_video(_ctx(platform="tiktok", url="https://www.tiktok.com/@h/video/1"))


@pytest.mark.parametrize(
    "url",
    [
        "http://v16.tiktokcdn.com/v.mp4",  # not https
        "https://evil.example.com/v.mp4",  # unknown host
        "https://tiktokcdn.com.evil.io/v.mp4",  # suffix trick
        "https://user:pw@v16.tiktokcdn.com/v.mp4",  # credentials in URL
        "https://169.254.169.254/latest/meta-data",  # metadata IP
    ],
)
def test_media_url_rejects_unsafe(url):
    with pytest.raises(ProviderError):
        check_media_url(url)


async def test_media_download_safety(monkeypatch):
    async def public(host, port):
        return host != "internal.fbcdn.net"

    monkeypatch.setattr(llm_mod, "_resolves_public", public)

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/redirect-evil":
            return httpx.Response(302, headers={"location": "https://evil.example.com/x.mp4"})
        if p == "/big":
            return httpx.Response(200, content=b"x" * 2000, headers={"content-type": "video/mp4"})
        if p == "/html":
            return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})
        if p == "/gone":
            return httpx.Response(403)
        return httpx.Response(200, content=b"ok", headers={"content-type": "video/mp4"})

    c = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    for path in ("/redirect-evil", "/big", "/html", "/gone"):
        with pytest.raises(ProviderError):
            await download_media(c, f"https://scontent.cdninstagram.com{path}", max_bytes=1000)
    with pytest.raises(ProviderError):  # resolves to a private address
        await download_media(c, "https://internal.fbcdn.net/ok", max_bytes=1000)
    assert (await download_media(c, "https://scontent.cdninstagram.com/ok", max_bytes=1000))[0] == b"ok"


def test_gemini_mode_requires_key():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(llm_provider="gemini", gemini_api_key="")


async def test_apify_media_url_per_platform():
    bodies = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        bodies.append(body)
        if "postURLs" in body:
            return httpx.Response(
                200, json=[{"mediaUrls": ["https://api.apify.com/v2/key-value-stores/s/records/v"]}]
            )
        return httpx.Response(200, json=[{"videoUrl": "https://scontent.cdninstagram.com/v.mp4"}])

    p = ApifyChannelProvider(
        Settings(channel_provider="apify", apify_token="t"),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert (await p.media_url("tiktok", "https://www.tiktok.com/@a/video/1")).startswith(
        "https://api.apify.com/"
    )
    assert bodies[0]["shouldDownloadVideos"] is True and bodies[0]["postURLs"] == [
        "https://www.tiktok.com/@a/video/1"
    ]
    assert (await p.media_url("instagram", "https://www.instagram.com/reel/x/")).endswith("v.mp4")
    assert await p.media_url("youtube", "https://youtube.com/shorts/x") is None


async def test_apify_token_sent_only_to_apify_storage(monkeypatch):
    async def public(host, port):
        return True

    monkeypatch.setattr(llm_mod, "_resolves_public", public)
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen[req.url.host] = req.headers.get("authorization")
        if req.url.host == "api.apify.com":
            return httpx.Response(302, headers={"location": "https://v16.tiktokcdn.com/v.mp4"})
        return httpx.Response(200, content=b"vid", headers={"content-type": "video/mp4"})

    c = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    data, _ = await download_media(
        c,
        "https://api.apify.com/v2/key-value-stores/s/records/v",
        1000,
        host_auth={"api.apify.com": "Bearer apify_api_x"},
    )
    assert data == b"vid"
    assert seen["api.apify.com"] == "Bearer apify_api_x"
    assert seen["v16.tiktokcdn.com"] is None  # token never leaves Apify


async def test_gemini_downloads_apify_media_with_token(monkeypatch):
    async def public(host, port):
        return True

    monkeypatch.setattr(llm_mod, "_resolves_public", public)
    auth = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.apify.com":
            auth["h"] = req.headers.get("authorization")
            return httpx.Response(200, content=b"MP4", headers={"content-type": "video/mp4"})
        return httpx.Response(200, json={"status": "completed", "output_text": json.dumps(GOOD)})

    g = GeminiLLMProvider(
        Settings(llm_provider="gemini", gemini_api_key="k", apify_token="apify_api_tok"),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await g.analyze_video(
        _ctx(
            platform="tiktok",
            url="https://www.tiktok.com/@a/video/1",
            media_url="https://api.apify.com/v2/key-value-stores/s/records/v",
        )
    )
    assert auth["h"] == "Bearer apify_api_tok"
