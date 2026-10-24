"""Channels watchlist, ingestion, outlier scoring, Discover feed, Apify mapping."""

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.api.v1.channels import get_session_factory, search_limiter
from app.core.config import Settings
from app.modules.discovery_analysis import channels as svc
from app.modules.discovery_analysis.outlier import compute_outliers, engagement_rate
from app.modules.discovery_analysis.providers import (
    ApifyChannelProvider,
    FakeChannelProvider,
    ProviderError,
    get_channel_provider,
    map_instagram_video,
    map_tiktok_channel,
    map_tiktok_video,
    map_youtube_video,
)
from app.modules.plans_credits.plans import PLANS

# ------------------------------------------------------------------ pure logic


@dataclass
class V:
    posted_at: datetime | None
    views: int


T0 = datetime(2026, 1, 1, tzinfo=UTC)


def test_outlier_needs_ten_prior_videos_and_uses_median_of_last_30():
    vids = [V(T0 + timedelta(days=i), 1000) for i in range(10)] + [V(T0 + timedelta(days=10), 50_000)]
    scores = compute_outliers(vids)
    assert scores[:10] == [None] * 10  # not enough history
    assert scores[10] == 50.0  # 50k ÷ median(1k)


def test_outlier_window_and_order_independent():
    # 40 videos: first 10 at 100 views, next 30 at 1000. The 41st compares only to the last 30 (1000).
    vids = [V(T0 + timedelta(days=i), 100 if i < 10 else 1000) for i in range(40)]
    vids.append(V(T0 + timedelta(days=40), 3000))
    shuffled = list(reversed(vids))
    scores = compute_outliers(shuffled)
    assert scores[0] == 3.0  # newest, aligned with input order
    assert compute_outliers([V(None, 5)]) == [None]


def test_engagement_rate():
    assert engagement_rate(1000, 50, 10, 5) == 6.5
    assert engagement_rate(0, 5, 5, 5) == 0.0


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.tiktok.com/@Some.Creator", ("tiktok", "some.creator")),
        ("https://instagram.com/creator_1/", ("instagram", "creator_1")),
        ("https://m.youtube.com/@Chan-Name/shorts", ("youtube", "chan-name")),
    ],
)
def test_parse_channel_url(url, expected):
    assert svc.parse_channel_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://www.tiktok.com/@x/video/1/../",
        "https://instagram.com/reel/abc",
        "https://example.com/@x",
        "nope",
    ],
)
def test_parse_channel_url_rejects(url):
    if url.startswith("https://www.tiktok.com/@x/video"):
        assert svc.parse_channel_url(url) == ("tiktok", "x")  # a video link still names the profile
        return
    with pytest.raises(svc.InvalidChannel):
        svc.parse_channel_url(url)


# ------------------------------------------------------------------ apify mapping + client


def test_apify_mappers_are_defensive():
    v = map_tiktok_video(
        {
            "id": "73",
            "webVideoUrl": "https://www.tiktok.com/@a/video/73",
            "text": "hi",
            "createTimeISO": "2026-10-01T10:00:00.000Z",
            "playCount": "1200",
            "diggCount": 100,
            "commentCount": None,
            "videoMeta": {"duration": 31, "coverUrl": "https://p16/x.jpg"},
        }
    )
    assert v and v.views == 1200 and v.comments == 0 and v.duration_sec == 31 and v.posted_at.year == 2026
    assert map_tiktok_video({"text": "missing id"}) is None
    ch = map_tiktok_channel({"authorMeta": {"nickName": "A", "fans": 9000, "id": 5}}, "a")
    assert ch.followers == 9000 and ch.external_id == "5"
    ig = map_instagram_video(
        {
            "type": "Video",
            "shortCode": "Cx1",
            "videoPlayCount": 5000,
            "likesCount": 9,
            "timestamp": "2026-09-01T00:00:00Z",
        }
    )
    assert ig and ig.url == "https://www.instagram.com/reel/Cx1/" and ig.views == 5000
    assert map_instagram_video({"type": "Image", "shortCode": "p1"}) is None
    yt = map_youtube_video({"id": "abc", "title": "t", "viewCount": 77, "date": "2026-08-01"})
    assert yt and yt.views == 77 and yt.url.endswith("/shorts/abc")


async def test_apify_client_sends_bearer_and_maps_results():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("authorization")
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            json=[
                {
                    "id": "1",
                    "webVideoUrl": "https://www.tiktok.com/@a/video/1",
                    "playCount": 10,
                    "createTimeISO": "2026-10-01T00:00:00Z",
                },
                {
                    "id": "2",
                    "webVideoUrl": "https://www.tiktok.com/@a/video/2",
                    "playCount": 20,
                    "createTimeISO": "2026-10-02T00:00:00Z",
                },
            ],
        )

    s = Settings(channel_provider="apify", apify_token="apify_api_test")
    p = ApifyChannelProvider(s, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    vids = await p.recent_videos("tiktok", "a", 2)
    assert seen["auth"] == "Bearer apify_api_test"
    assert seen["path"].endswith("/clockworks~tiktok-scraper/run-sync-get-dataset-items")
    assert seen["body"]["profiles"] == ["a"] and seen["body"]["shouldDownloadVideos"] is False
    assert [v.external_id for v in vids] == ["2", "1"]  # newest first


async def test_apify_errors_become_provider_errors():
    p = ApifyChannelProvider(
        Settings(channel_provider="apify", apify_token="t"),
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))),
    )
    with pytest.raises(ProviderError):
        await p.recent_videos("youtube", "x", 5)


def test_apify_mode_requires_token():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(channel_provider="apify", apify_token="")


# ------------------------------------------------------------------ API


@pytest.fixture
def chan_app(app, sessionmaker):
    app.dependency_overrides[get_session_factory] = lambda: sessionmaker
    search_limiter.reset()
    return app


async def _ws(client, user, name="WS"):
    return (await client.post("/api/v1/workspaces", headers=user.headers, json={"name": name})).json()["id"]


async def test_add_by_url_syncs_videos_and_scores_outliers(chan_app, client, alice):
    ws = await _ws(client, alice)
    r = await client.post(
        f"/api/v1/workspaces/{ws}/channels",
        headers=alice.headers,
        json={"url": "https://www.tiktok.com/@growthgrace"},
    )
    assert r.status_code == 201 and r.json()["added"] is True
    wl = (await client.get(f"/api/v1/workspaces/{ws}/channels", headers=alice.headers)).json()
    assert len(wl) == 1
    item = wl[0]
    assert item["handle"] == "growthgrace" and item["sync_status"] == "ok" and item["video_count"] == 60
    assert item["outlier_videos"] > 0

    feed = (await client.get(f"/api/v1/workspaces/{ws}/videos?limit=5", headers=alice.headers)).json()
    assert feed["total"] == 60
    scores = [v["outlier_score"] for v in feed["items"]]
    assert scores == sorted(scores, reverse=True) and scores[0] >= 5  # sorted by outlier, desc
    assert feed["items"][0]["channel"]["handle"] == "growthgrace"

    again = await client.post(
        f"/api/v1/workspaces/{ws}/channels",
        headers=alice.headers,
        json={"platform": "tiktok", "handle": "@GrowthGrace"},
    )
    assert again.status_code == 200 and again.json()["added"] is False


async def test_feed_filters_combine_with_and(chan_app, client, alice):
    ws = await _ws(client, alice)
    for url in ("https://www.tiktok.com/@alpha", "https://www.youtube.com/@bravo"):
        await client.post(f"/api/v1/workspaces/{ws}/channels", headers=alice.headers, json={"url": url})
    base = f"/api/v1/workspaces/{ws}/videos"
    all_ = (await client.get(base, headers=alice.headers)).json()["total"]
    yt = (await client.get(f"{base}?platform=youtube", headers=alice.headers)).json()
    assert 0 < yt["total"] < all_ and {v["platform"] for v in yt["items"]} == {"youtube"}
    hot = (
        await client.get(f"{base}?platform=youtube&min_outlier=5&min_views=1000", headers=alice.headers)
    ).json()
    assert hot["total"] <= yt["total"] and all(v["outlier_score"] >= 5 for v in hot["items"])
    newest = (await client.get(f"{base}?sort=newest&limit=3", headers=alice.headers)).json()["items"]
    dates = [v["posted_at"] for v in newest]
    assert dates == sorted(dates, reverse=True)
    q = (await client.get(f"{base}?q=bravo", headers=alice.headers)).json()
    assert q["total"] == yt["total"]
    none = (await client.get(f"{base}?min_outlier=100000", headers=alice.headers)).json()
    assert none == {"items": [], "total": 0, "limit": 48, "offset": 0}


async def test_validation_not_found_and_tenant_isolation(chan_app, client, alice, bob):
    ws = await _ws(client, alice)
    url = f"/api/v1/workspaces/{ws}/channels"
    r = await client.post(url, headers=alice.headers, json={"url": "https://www.tiktok.com/@notfound.person"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "channel_not_found"
    r = await client.post(url, headers=alice.headers, json={"url": "https://example.com/@x"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_channel"
    assert (await client.post(url, headers=alice.headers, json={})).status_code == 422

    await client.post(url, headers=alice.headers, json={"url": "https://www.tiktok.com/@private.one"})
    assert (await client.get(url, headers=bob.headers)).status_code == 404
    assert (await client.get(f"/api/v1/workspaces/{ws}/videos", headers=bob.headers)).status_code == 404
    assert (
        await client.post(url, headers=bob.headers, json={"url": "https://www.tiktok.com/@x"})
    ).status_code == 404


async def test_plan_limit_on_channels(chan_app, client, alice, monkeypatch):
    monkeypatch.setitem(PLANS, "pro", replace(PLANS["pro"], competitors_per_workspace=2))
    ws = await _ws(client, alice)
    url = f"/api/v1/workspaces/{ws}/channels"
    for h in ("one", "two"):
        assert (
            await client.post(url, headers=alice.headers, json={"platform": "tiktok", "handle": h})
        ).status_code == 201
    r = await client.post(url, headers=alice.headers, json={"platform": "tiktok", "handle": "three"})
    assert r.status_code == 403 and r.json()["error"]["details"]["resource"] == "channels"


async def test_remove_and_refresh_cooldown(chan_app, client, alice):
    ws = await _ws(client, alice)
    ch = (
        await client.post(
            f"/api/v1/workspaces/{ws}/channels",
            headers=alice.headers,
            json={"platform": "instagram", "handle": "cook"},
        )
    ).json()["channel"]
    r = await client.post(f"/api/v1/workspaces/{ws}/channels/{ch['id']}/refresh", headers=alice.headers)
    assert r.status_code == 429  # just synced
    assert (
        await client.delete(f"/api/v1/workspaces/{ws}/channels/{ch['id']}", headers=alice.headers)
    ).status_code == 204
    assert (await client.get(f"/api/v1/workspaces/{ws}/videos", headers=alice.headers)).json()["total"] == 0
    assert (
        await client.delete(f"/api/v1/workspaces/{ws}/channels/{ch['id']}", headers=alice.headers)
    ).status_code == 404


async def test_search_and_suggestions(chan_app, client, alice, bob):
    a_ws = await _ws(client, alice)
    await client.post(
        f"/api/v1/workspaces/{a_ws}/channels",
        headers=alice.headers,
        json={"platform": "tiktok", "handle": "fitwithkai"},
    )
    b_ws = await _ws(client, bob)
    sug = (await client.get(f"/api/v1/workspaces/{b_ws}/channels/suggested", headers=bob.headers)).json()
    assert [c["handle"] for c in sug] == ["fitwithkai"]
    # Known channel by partial name + exact-handle lookups on platforms not stored yet.
    res = (
        await client.get(f"/api/v1/workspaces/{b_ws}/channels/search?q=fitwithkai", headers=bob.headers)
    ).json()
    known = [r for r in res if r["channel_id"]]
    assert known[0]["handle"] == "fitwithkai" and known[0]["tracked"] is False
    assert {r["platform"] for r in res if not r["channel_id"]} == {"instagram", "youtube"}
    mine = (
        await client.get(
            f"/api/v1/workspaces/{a_ws}/channels/search?q=fitw&platform=tiktok", headers=alice.headers
        )
    ).json()
    assert mine[0]["tracked"] is True


async def test_sync_failure_is_recorded_not_raised(chan_app, client, alice):
    class Broken(FakeChannelProvider):
        async def recent_videos(self, platform, handle, limit):
            raise ProviderError("Provider down")

    chan_app.dependency_overrides[get_channel_provider] = lambda: Broken()
    ws = await _ws(client, alice)
    r = await client.post(
        f"/api/v1/workspaces/{ws}/channels",
        headers=alice.headers,
        json={"platform": "youtube", "handle": "down"},
    )
    assert r.status_code == 201
    item = (await client.get(f"/api/v1/workspaces/{ws}/channels", headers=alice.headers)).json()[0]
    assert item["sync_status"] == "error" and item["sync_error"] == "Provider down"


def test_youtube_duration_and_input_param():
    from app.modules.discovery_analysis.providers import _duration

    assert [_duration(x) for x in (31, "31", "0:58", "1:02:03", None, "", "abc")] == [
        31,
        31,
        58,
        3723,
        None,
        None,
        None,
    ]
    yt = map_youtube_video({"id": "x", "duration": "00:00:45", "viewCount": 5})
    assert yt and yt.duration_sec == 45
    p = ApifyChannelProvider(Settings(channel_provider="apify", apify_token="t"))
    _, payload = p._actor_and_input("youtube", "mrbeast", 60)
    assert payload == {"channels": ["mrbeast"], "maxResultsShorts": 60}
