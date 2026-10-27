"""Our own copies of video thumbnails.

TikTok and Instagram give signed image links that stop working after ~2–5 days, so the app would
show empty cards. After each import we download each new thumbnail once, shrink it to a small WebP
(~10–20 KB) and keep it in `video_thumbnails`; the API then hands out our stable URL instead.
YouTube links (i.ytimg.com) don't expire, so they're used as-is.

Safety: HTTPS only, image hosts on an allow-list (also after redirects), images only, max 3 MB.
"""

import asyncio
import io
import logging
import uuid
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Video, VideoThumbnail

log = logging.getLogger("smart_content.thumbnails")

# Hosts whose links expire. Matched as the host itself or any sub-domain.
EXPIRING_HOSTS = (
    "tiktokcdn.com",
    "tiktokcdn-us.com",
    "tiktokcdn-eu.com",
    "ibyteimg.com",
    "cdninstagram.com",
    "fbcdn.net",
)
MAX_BYTES = 3 * 1024 * 1024
MAX_WIDTH = 360
CONCURRENCY = 6


def needs_copy(url: str | None) -> bool:
    if not url:
        return False
    p = urlparse(url)
    host = (p.hostname or "").lower()
    return p.scheme == "https" and any(host == h or host.endswith("." + h) for h in EXPIRING_HOSTS)


def display_url(v: Video) -> str | None:
    """The thumbnail link to show for a video: our stored copy when we have one."""
    if v.thumb_stored_at is not None:
        return f"{get_settings().public_api_url.rstrip('/')}/api/v1/thumbnails/{v.id}"
    return v.thumbnail_url


async def fetch_image(client: httpx.AsyncClient, url: str) -> bytes | None:
    """Download one image following at most 3 redirects, each re-checked against the allow-list."""
    for _ in range(4):
        if not needs_copy(url):
            return None
        async with client.stream("GET", url) as r:
            if r.is_redirect and "location" in r.headers:
                url = str(r.url.join(r.headers["location"]))
                continue
            if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
                return None
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_BYTES:
                    return None
            return bytes(buf)
    return None


def shrink(raw: bytes) -> tuple[bytes, str] | None:
    """Resize to ≤ 360 px wide and re-encode as WebP. None if it isn't a readable image."""
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im.load()
            if im.width < 8 or im.height < 8:
                return None
            img = im.convert("RGB")
            if img.width > MAX_WIDTH:
                img = img.resize(
                    (MAX_WIDTH, round(img.height * MAX_WIDTH / img.width)), Image.Resampling.LANCZOS
                )
            out = io.BytesIO()
            img.save(out, "WEBP", quality=72, method=4)
            return out.getvalue(), "image/webp"
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None


async def store_missing(
    db: AsyncSession, video_ids: list[uuid.UUID] | None = None, *, client: httpx.AsyncClient | None = None
) -> int:
    """Copy thumbnails we don't have yet (all videos, or just `video_ids`). Returns how many were stored.
    Never raises: a failed image just keeps its original link."""
    stmt = select(Video).where(Video.thumb_stored_at.is_(None), Video.thumbnail_url.is_not(None))
    if video_ids is not None:
        if not video_ids:
            return 0
        stmt = stmt.where(Video.id.in_(video_ids))
    todo = [v for v in (await db.scalars(stmt)).all() if needs_copy(v.thumbnail_url)]
    if not todo:
        return 0

    own = client is None
    http = client or httpx.AsyncClient(
        timeout=20, follow_redirects=False, headers={"User-Agent": "Mozilla/5.0 SmartContent/1.0"}
    )
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(v: Video) -> tuple[Video, tuple[bytes, str] | None]:
        async with sem:
            try:
                raw = await fetch_image(http, v.thumbnail_url or "")
            except httpx.HTTPError:
                raw = None
        return v, (await asyncio.to_thread(shrink, raw) if raw else None)

    try:
        results = await asyncio.gather(*(one(v) for v in todo))
    finally:
        if own:
            await http.aclose()

    stored = 0
    now = datetime.now(UTC)
    for v, img in results:
        if img is None:
            continue
        data, mime = img
        db.add(VideoThumbnail(video_id=v.id, mime=mime, data=data, created_at=now))
        v.thumb_stored_at = now
        stored += 1
    if stored:
        await db.commit()
    log.info("thumbnails: stored %s of %s", stored, len(todo))
    return stored
