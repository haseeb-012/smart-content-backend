"""Channel/video data providers behind one interface (master prompt: every provider swappable).

- FakeChannelProvider: deterministic demo data, no network. Default (CHANNEL_PROVIDER=fake) and in tests.
- ApifyChannelProvider: Apify actors per platform (CHANNEL_PROVIDER=apify + APIFY_TOKEN).
  ⚠ Field mapping follows the actors' documented output but must be verified on the first real run —
  actor outputs change. Mappers are defensive: unknown/missing fields become None/0, never crash.
"""

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any, Literal, Protocol

import httpx

from app.core.config import Settings, get_settings
from app.core.errors import AppError

log = logging.getLogger("smart_content.providers")

Platform = Literal["tiktok", "instagram", "youtube"]


class ProviderError(AppError):
    status_code = 502
    code = "provider_error"


@dataclass
class ChannelInfo:
    platform: Platform
    handle: str
    display_name: str = ""
    external_id: str | None = None
    avatar_url: str | None = None
    followers: int | None = None
    total_views: int | None = None


@dataclass
class VideoInfo:
    external_id: str
    url: str
    caption: str = ""
    thumbnail_url: str | None = None
    posted_at: datetime | None = None
    duration_sec: int | None = None
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0


class ChannelProvider(Protocol):
    name: str

    async def lookup(self, platform: Platform, handle: str) -> ChannelInfo | None:
        """Return the channel if it exists on the platform, else None."""
        ...

    async def recent_videos(self, platform: Platform, handle: str, limit: int) -> list[VideoInfo]:
        """Most recent short-form videos, newest first."""
        ...

    async def bio_has_code(self, platform: Platform, handle: str, code: str) -> bool | None:
        """Social verification: is `code` in the account's public bio? None = bio couldn't be read."""
        ...

    async def media_url(self, platform: Platform, video_url: str) -> str | None:
        """Fresh direct link to ONE video's file, fetched only when a user runs Deep Analysis.
        None for YouTube (the AI reads the URL itself) or when unavailable."""
        ...


# ------------------------------------------------------------------------------------------ fake


def _h(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest()[:12], 16)


class FakeChannelProvider:
    """Deterministic demo data. Handles containing 'notfound' don't exist (to exercise validation)."""

    name = "fake"
    NOW = datetime(2026, 10, 23, 9, tzinfo=UTC)

    async def lookup(self, platform: Platform, handle: str) -> ChannelInfo | None:
        if "notfound" in handle:
            return None
        k = _h(f"{platform}:{handle}")
        return ChannelInfo(
            platform=platform,
            handle=handle,
            display_name=handle.replace(".", " ").replace("_", " ").title(),
            external_id=str(k % 10**10),
            avatar_url=None,
            followers=50_000 + k % 3_000_000,
            total_views=2_000_000 + k % 400_000_000,
        )

    async def recent_videos(self, platform: Platform, handle: str, limit: int) -> list[VideoInfo]:
        k = _h(f"{platform}:{handle}")
        base = 20_000 + k % 200_000
        out: list[VideoInfo] = []
        for i in range(limit):
            r = _h(f"{k}:{i}")
            # Typical videos hover around the baseline; ~1 in 9 breaks out (5x–60x).
            mult = 0.5 + (r % 100) / 100
            if r % 9 == 0:
                mult = 5 + (r % 56)
            views = int(base * mult)
            likes = int(views * (0.03 + (r % 7) / 100))
            vid = f"{platform[:2]}{k % 10**6}{i:04d}"
            url = {
                "tiktok": f"https://www.tiktok.com/@{handle}/video/{vid}",
                "instagram": f"https://www.instagram.com/reel/{vid}/",
                "youtube": f"https://www.youtube.com/shorts/{vid}",
            }[platform]
            out.append(
                VideoInfo(
                    external_id=vid,
                    url=url,
                    caption=f"Demo video {i + 1} from @{handle} — the one thing nobody tells you #shorts",
                    posted_at=self.NOW - timedelta(days=2 * i + (r % 2)),
                    duration_sec=15 + r % 70,
                    views=views,
                    likes=likes,
                    comments=int(likes * 0.04),
                    shares=int(likes * 0.02),
                )
            )
        return out

    async def media_url(self, platform: Platform, video_url: str) -> str | None:
        return None  # the fake AI doesn't need the file

    async def bio_has_code(self, platform: Platform, handle: str, code: str) -> bool | None:
        # Demo: every code is "found", except handles containing 'nocode' (to exercise the failure path).
        if "notfound" in handle:
            return None
        return "nocode" not in handle


# ------------------------------------------------------------------------------------------ apify


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _opt_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _dt(v: Any) -> datetime | None:
    if v is None:
        return None
    try:
        if isinstance(v, int | float):
            return datetime.fromtimestamp(v, UTC)
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except (ValueError, OSError):
        return None


def _duration(v: Any) -> int | None:
    """Seconds from 31, "31", "0:31" or "1:02:03" (the YouTube actor returns clock strings)."""
    if v is None or v == "":
        return None
    s = str(v).strip()
    if s.isdigit():
        return int(s)
    try:
        parts = [int(p) for p in s.split(":")]
    except ValueError:
        return None
    total = 0
    for p in parts:
        total = total * 60 + p
    return total if len(parts) <= 3 else None


def map_tiktok_video(item: dict[str, Any]) -> VideoInfo | None:
    vid = item.get("id")
    url = item.get("webVideoUrl")
    if not vid or not url:
        return None
    meta = item.get("videoMeta") or {}
    return VideoInfo(
        external_id=str(vid),
        url=url,
        caption=item.get("text") or "",
        thumbnail_url=meta.get("coverUrl"),
        posted_at=_dt(item.get("createTimeISO") or item.get("createTime")),
        duration_sec=_opt_int(meta.get("duration")),
        views=_int(item.get("playCount")),
        likes=_int(item.get("diggCount")),
        comments=_int(item.get("commentCount")),
        shares=_int(item.get("shareCount")),
    )


def map_tiktok_channel(item: dict[str, Any], handle: str) -> ChannelInfo:
    a = item.get("authorMeta") or {}
    return ChannelInfo(
        platform="tiktok",
        handle=handle,
        display_name=a.get("nickName") or a.get("name") or handle,
        external_id=_str(a.get("id")),
        avatar_url=a.get("avatar"),
        followers=_opt_int(a.get("fans")),
        total_views=None,
    )


def map_instagram_video(item: dict[str, Any]) -> VideoInfo | None:
    if (item.get("type") or "").lower() not in ("video", "clips", "reel", ""):
        return None
    sc = item.get("shortCode")
    if not sc:
        return None
    views = item.get("videoPlayCount") or item.get("videoViewCount") or item.get("playCount")
    return VideoInfo(
        external_id=str(sc),
        url=item.get("url") or f"https://www.instagram.com/reel/{sc}/",
        caption=item.get("caption") or "",
        thumbnail_url=item.get("displayUrl"),
        posted_at=_dt(item.get("timestamp")),
        duration_sec=_opt_int(item.get("videoDuration")),
        views=_int(views),
        likes=_int(item.get("likesCount")),
        comments=_int(item.get("commentsCount")),
        shares=0,
    )


def map_instagram_channel(item: dict[str, Any], handle: str) -> ChannelInfo:
    return ChannelInfo(
        platform="instagram",
        handle=handle,
        display_name=item.get("fullName") or item.get("ownerFullName") or handle,
        external_id=_str(item.get("id") or item.get("ownerId")),
        avatar_url=item.get("profilePicUrl") or item.get("profilePicUrlHD"),
        followers=_opt_int(item.get("followersCount")),
    )


def map_youtube_video(item: dict[str, Any]) -> VideoInfo | None:
    vid = item.get("id")
    if not vid:
        return None
    return VideoInfo(
        external_id=str(vid),
        url=item.get("url") or f"https://www.youtube.com/shorts/{vid}",
        caption=item.get("title") or item.get("text") or "",
        thumbnail_url=item.get("thumbnailUrl"),
        posted_at=_dt(item.get("date") or item.get("publishedAt")),
        duration_sec=_duration(item.get("duration")),
        views=_int(item.get("viewCount")),
        likes=_int(item.get("likes")),
        comments=_int(item.get("commentsCount")),
        shares=0,
    )


def map_youtube_channel(item: dict[str, Any], handle: str) -> ChannelInfo:
    return ChannelInfo(
        platform="youtube",
        handle=handle,
        display_name=item.get("channelName") or handle,
        external_id=_str(item.get("channelId")),
        avatar_url=item.get("channelAvatarUrl"),
        followers=_opt_int(item.get("numberOfSubscribers")),
        total_views=_opt_int(item.get("channelTotalViews")),
    )


def _str(v: Any) -> str | None:
    return str(v) if v not in (None, "") else None


class ApifyChannelProvider:
    name = "apify"
    BASE = "https://api.apify.com/v2/acts"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.s = settings
        self.client = client or httpx.AsyncClient(timeout=settings.apify_timeout_seconds)

    def _actor_and_input(self, platform: Platform, handle: str, limit: int) -> tuple[str, dict[str, Any]]:
        if platform == "tiktok":
            return self.s.apify_actor_tiktok, {
                "profiles": [handle],
                "resultsPerPage": limit,
                "shouldDownloadVideos": False,
                "shouldDownloadCovers": False,
            }
        if platform == "instagram":
            return self.s.apify_actor_instagram, {
                "directUrls": [f"https://www.instagram.com/{handle}/reels/"],
                "resultsType": "posts",
                "resultsLimit": limit,
            }
        return self.s.apify_actor_youtube, {"channels": [handle], "maxResultsShorts": limit}

    async def _run(self, actor: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        url = f"{self.BASE}/{actor}/run-sync-get-dataset-items"
        try:
            r = await self.client.post(
                url, json=payload, headers={"Authorization": f"Bearer {self.s.apify_token}"}
            )
        except httpx.HTTPError as e:
            raise ProviderError("Couldn't reach the video data provider. Try again shortly.") from e
        if r.status_code >= 400:
            log.warning("apify %s failed: %s %s", actor, r.status_code, r.text[:300])
            raise ProviderError("The video data provider returned an error. Try again shortly.")
        data = r.json()
        return data if isinstance(data, list) else []

    async def _items(self, platform: Platform, handle: str, limit: int) -> list[dict[str, Any]]:
        actor, payload = self._actor_and_input(platform, handle, limit)
        return await self._run(actor, payload)

    async def lookup(self, platform: Platform, handle: str) -> ChannelInfo | None:
        if platform == "instagram":
            items = await self._run(
                self.s.apify_actor_instagram,
                {
                    "directUrls": [f"https://www.instagram.com/{handle}/"],
                    "resultsType": "details",
                    "resultsLimit": 1,
                },
            )
            return map_instagram_channel(items[0], handle) if items and items[0].get("username") else None
        items = await self._items(platform, handle, 1)
        if not items:
            return None
        return (
            map_tiktok_channel(items[0], handle)
            if platform == "tiktok"
            else map_youtube_channel(items[0], handle)
        )

    async def recent_videos(self, platform: Platform, handle: str, limit: int) -> list[VideoInfo]:
        mapper = {"tiktok": map_tiktok_video, "instagram": map_instagram_video, "youtube": map_youtube_video}[
            platform
        ]
        videos = [v for v in (mapper(i) for i in await self._items(platform, handle, limit)) if v]
        return sorted(videos, key=lambda v: v.posted_at or datetime.min.replace(tzinfo=UTC), reverse=True)

    async def bio_has_code(self, platform: Platform, handle: str, code: str) -> bool | None:
        """Reads the public bio. ⚠ Field names follow the actors' docs — verify on first real run."""
        if platform == "instagram":
            items = await self._run(
                self.s.apify_actor_instagram,
                {
                    "directUrls": [f"https://www.instagram.com/{handle}/"],
                    "resultsType": "details",
                    "resultsLimit": 1,
                },
            )
            bios = [str(i.get("biography") or "") for i in items[:1]]
        else:
            items = await self._items(platform, handle, 1)
            if platform == "tiktok":
                bios = [str((i.get("authorMeta") or {}).get("signature") or "") for i in items[:1]]
            else:
                bios = [
                    str(
                        i.get("channelDescription")
                        or (i.get("aboutChannelInfo") or {}).get("channelDescription")
                        or ""
                    )
                    for i in items[:1]
                ]
        if not bios:
            return None
        return code.upper() in bios[0].upper()

    async def media_url(self, platform: Platform, video_url: str) -> str | None:
        """One-video lookup. TikTok: the actor downloads the file to Apify storage (mediaUrls).
        Instagram: the post's CDN link (videoUrl). Links expire, so they are never stored."""
        if platform == "youtube":
            return None
        if platform == "tiktok":
            items = await self._run(
                self.s.apify_actor_tiktok,
                {"postURLs": [video_url], "shouldDownloadVideos": True, "shouldDownloadCovers": False},
            )
            for it in items:
                urls = it.get("mediaUrls") or []
                if urls and isinstance(urls[0], str):
                    return urls[0]
            return None
        items = await self._run(
            self.s.apify_actor_instagram,
            {"directUrls": [video_url], "resultsType": "posts", "resultsLimit": 1},
        )
        for it in items:
            if isinstance(it.get("videoUrl"), str):
                return str(it["videoUrl"])
        return None


@lru_cache
def _apify() -> ApifyChannelProvider:
    return ApifyChannelProvider(get_settings())


def get_channel_provider() -> ChannelProvider:
    """FastAPI dependency (overridden in tests)."""
    if get_settings().channel_provider == "apify":
        return _apify()
    return FakeChannelProvider()
