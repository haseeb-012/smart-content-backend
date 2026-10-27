"""Thumbnail copies: allow-list, shrink to WebP, stored once, served from our URL, safe on bad input."""

import io
import uuid

import httpx
from PIL import Image

from app.models import Channel, Video, VideoThumbnail
from app.modules.discovery_analysis import thumbnails
from app.modules.discovery_analysis.thumbnails import display_url, needs_copy, shrink

TIKTOK = "https://p16-common-sign.tiktokcdn-us.com/obj/cover.jpeg?x-expires=1"


def _jpeg(w: int = 720, h: int = 1280) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 60)).save(buf, "JPEG")
    return buf.getvalue()


def test_only_expiring_https_hosts_are_copied():
    assert needs_copy(TIKTOK)
    assert needs_copy("https://scontent-lhr11-1.cdninstagram.com/v/t51/x.jpg")
    assert needs_copy("https://instagram.fver1-1.fna.fbcdn.net/x.jpg")
    assert not needs_copy("https://i.ytimg.com/vi/abc/hqdefault.jpg")  # YouTube links don't expire
    assert not needs_copy("http://p16.tiktokcdn.com/x.jpg")  # not https
    assert not needs_copy("https://tiktokcdn.com.evil.example/x.jpg")
    assert not needs_copy("https://169.254.169.254/latest")
    assert not needs_copy(None)


def test_shrink_makes_small_webp_and_rejects_junk():
    out = shrink(_jpeg())
    assert out is not None
    data, mime = out
    assert mime == "image/webp" and len(data) < 30_000
    with Image.open(io.BytesIO(data)) as im:
        assert im.width == 360 and im.height == 640
    assert shrink(b"<html>not an image</html>") is None


async def _video(sessionmaker, url: str) -> uuid.UUID:
    async with sessionmaker() as s:
        ch = Channel(platform="tiktok", handle=f"h{uuid.uuid4().hex[:6]}")
        s.add(ch)
        await s.flush()
        v = Video(
            channel_id=ch.id,
            platform="tiktok",
            external_id=uuid.uuid4().hex,
            url="https://www.tiktok.com/@h/video/1",
            thumbnail_url=url,
        )
        s.add(v)
        await s.commit()
        return v.id


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_store_serve_and_display(client, sessionmaker):
    vid = await _video(sessionmaker, TIKTOK)
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=_jpeg())

    async with sessionmaker() as s, _client(handler) as http:
        assert await thumbnails.store_missing(s, [vid], client=http) == 1
    async with sessionmaker() as s, _client(handler) as http:
        assert await thumbnails.store_missing(s, [vid], client=http) == 0  # stored once only
    assert len(calls) == 1

    async with sessionmaker() as s:
        v = await s.get(Video, vid)
        assert display_url(v).endswith(f"/api/v1/thumbnails/{vid}")
    r = await client.get(f"/api/v1/thumbnails/{vid}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert "immutable" in r.headers["cache-control"]
    assert (await client.get(f"/api/v1/thumbnails/{uuid.uuid4()}")).status_code == 404


async def test_bad_responses_keep_the_original_link(sessionmaker):
    cases = [
        httpx.Response(403),
        httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>"),
        httpx.Response(
            302, headers={"location": "https://evil.example/x.jpg"}
        ),  # redirect off the allow-list
        httpx.Response(
            200, headers={"content-type": "image/jpeg"}, content=b"x" * (thumbnails.MAX_BYTES + 10)
        ),
    ]
    for resp in cases:
        vid = await _video(sessionmaker, TIKTOK)
        async with sessionmaker() as s, _client(lambda _req, r=resp: r) as http:
            assert await thumbnails.store_missing(s, [vid], client=http) == 0
        async with sessionmaker() as s:
            v = await s.get(Video, vid)
            assert v.thumb_stored_at is None and display_url(v) == TIKTOK
            assert await s.get(VideoThumbnail, vid) is None


async def test_youtube_links_are_left_alone(sessionmaker):
    url = "https://i.ytimg.com/vi/abc/hqdefault.jpg"
    vid = await _video(sessionmaker, url)

    def handler(req):  # must never be called
        raise AssertionError("no download for YouTube")

    async with sessionmaker() as s, _client(handler) as http:
        assert await thumbnails.store_missing(s, [vid], client=http) == 0
    async with sessionmaker() as s:
        assert display_url(await s.get(Video, vid)) == url
