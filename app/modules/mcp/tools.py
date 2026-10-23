"""MCP tools: Smart Content data for AI assistants (Claude, Cursor, VS Code, …).

Every tool runs as the signed-in user, inside one of their workspaces, through the same services
(and the same tenant checks, plan limits and credit rules) as the app. Reading is free; `analyze`
costs 1 credit on success, exactly like the Analyze button.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, NotFound
from app.models import Channel, Membership, Profile, SavedHook, Video, VideoAnalysis, Workspace
from app.modules.content_studio import studio
from app.modules.discovery_analysis import analysis as an
from app.modules.discovery_analysis import channels as ch
from app.modules.discovery_analysis.llm import LLMProvider
from app.modules.discovery_analysis.providers import ChannelProvider, Platform


@dataclass
class ToolContext:
    db: AsyncSession
    profile: Profile
    provider: ChannelProvider
    llm: LLMProvider
    session_factory: ch.SessionFactory


Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class Tool:
    name: str
    title: str
    description: str
    properties: dict[str, Any]
    handler: Handler
    required: list[str] = field(default_factory=list)
    read_only: bool = True

    def schema(self) -> dict[str, Any]:
        props = {
            **self.properties,
            "workspace": {
                "type": "string",
                "description": "Workspace name or ID. Defaults to your first workspace.",
            },
        }
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": {"type": "object", "properties": props, "required": self.required},
            "annotations": {
                "title": self.title,
                "readOnlyHint": self.read_only,
                "openWorldHint": not self.read_only,
            },
        }


PLATFORM = {"type": "string", "enum": ["tiktok", "instagram", "youtube"]}
_background: set[asyncio.Task[None]] = set()  # keep fire-and-forget syncs alive


# ------------------------------------------------------------------ helpers


async def _workspace(c: ToolContext, args: dict[str, Any]) -> Workspace:
    rows = (
        await c.db.scalars(
            select(Workspace)
            .join(Membership, Membership.workspace_id == Workspace.id)
            .where(Membership.user_id == c.profile.id, Workspace.is_active.is_(True))
            .order_by(Workspace.created_at)
        )
    ).all()
    if not rows:
        raise NotFound("You don't have a workspace yet — create one in the Smart Content app first.")
    want = str(args.get("workspace") or "").strip()
    if not want:
        return rows[0]
    for w in rows:
        if str(w.id) == want or w.name.lower() == want.lower():
            return w
    raise NotFound(f"No workspace called '{want}'. Yours: {', '.join(w.name for w in rows)}.")


def _video(v: Video, c: Channel) -> dict[str, Any]:
    return {
        "video_id": str(v.id),
        "url": v.url,
        "platform": v.platform,
        "creator": f"@{c.handle}",
        "caption": v.caption[:300],
        "posted_at": v.posted_at.isoformat() if v.posted_at else None,
        "views": v.views,
        "likes": v.likes,
        "comments": v.comments,
        "engagement_rate_pct": v.engagement_rate,
        "outlier_score": v.outlier_score,
        "duration_sec": v.duration_sec,
    }


def _channel(c: Channel, **extra: Any) -> dict[str, Any]:
    return {
        "channel_id": str(c.id),
        "platform": c.platform,
        "handle": f"@{c.handle}",
        "name": c.display_name,
        "followers": c.followers,
        "videos_tracked": c.video_count,
        "sync_status": c.sync_status,
        **extra,
    }


def _int(args: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(args.get(key, default))))
    except (TypeError, ValueError):
        return default


def _uuid(raw: Any, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(raw))
    except ValueError as e:
        raise NotFound(f"That {what} ID isn't valid.") from e


# ------------------------------------------------------------------ tools


async def channels_search(c: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    w = await _workspace(c, args)
    query = str(args.get("query") or "").strip()
    platform = cast(Platform | None, args.get("platform"))
    if len(query) >= 5 and " " in query:
        found = await ch.niche_search(c.db, c.provider, c.llm, query, platform)
    else:
        found = await ch.search_channels(c.db, c.provider, query, platform)
    tracked = {r.channel.id for r in await ch.list_watchlist(c.db, c.profile, w.id)}
    out = []
    for r in found:
        if isinstance(r, Channel):
            out.append(_channel(r, tracked=r.id in tracked))
        else:
            p, h, name, followers = r
            out.append(
                {"platform": p, "handle": f"@{h}", "name": name, "followers": followers, "tracked": False}
            )
    return {"workspace": w.name, "results": out}


async def channels_add(c: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    w = await _workspace(c, args)
    if args.get("url"):
        platform, handle = ch.parse_channel_url(str(args["url"]))
    elif args.get("platform") and args.get("handle"):
        platform, handle = cast(Platform, args["platform"]), str(args["handle"])
    else:
        raise ch.InvalidChannel("Give a profile URL, or a platform and handle.")
    channel, added = await ch.add_to_watchlist(c.db, c.profile, w.id, c.provider, platform, handle)
    if channel.last_synced_at is None or channel.sync_status == "error":
        task = asyncio.create_task(ch.sync_channel(c.session_factory, c.provider, channel.id))
        _background.add(task)
        task.add_done_callback(_background.discard)
    return {
        "workspace": w.name,
        "added": added,
        "channel": _channel(channel),
        "note": "Videos are being fetched — ask again in a minute for outliers."
        if added
        else "Already tracked.",
    }


async def channels_suggest(c: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    w = await _workspace(c, args)
    return {
        "workspace": w.name,
        "suggestions": [_channel(x) for x in await ch.suggested_channels(c.db, c.profile, w.id)],
    }


async def channels_recap(c: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    w = await _workspace(c, args)
    days = _int(args, "days", 30, 1, 365)
    handle = str(args.get("handle") or "").strip().lstrip("@").lower()
    rows = await ch.list_watchlist(c.db, c.profile, w.id)
    if handle:
        rows = [r for r in rows if r.channel.handle == handle]
        if not rows:
            raise NotFound(f"@{handle} isn't on this workspace's watchlist.")
    recap = []
    for r in rows[:25]:
        top, _ = await ch.discover_feed(
            c.db,
            c.profile,
            w.id,
            ch.FeedFilters(channel_ids=[r.channel.id], posted_within_days=days, sort="outlier", limit=3),
        )
        recap.append(
            _channel(r.channel, outlier_videos_5x=r.outliers, top_videos=[_video(v, cc) for v, cc in top])
        )
    return {"workspace": w.name, "days": days, "channels": recap}


async def videos_watchlist(c: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    w = await _workspace(c, args)
    sort = args.get("sort") if args.get("sort") in ("outlier", "views", "engagement", "newest") else "outlier"
    f = ch.FeedFilters(
        platform=cast(Platform | None, args.get("platform")),
        min_outlier=float(args["min_outlier"]) if args.get("min_outlier") is not None else None,
        posted_within_days=_int(args, "days", 30, 1, 3650) if args.get("days") is not None else None,
        q=str(args["keyword"])[:100] if args.get("keyword") else None,
        sort=sort,  # type: ignore[arg-type]
        limit=_int(args, "limit", 20, 1, 50),
    )
    rows, total = await ch.discover_feed(c.db, c.profile, w.id, f)
    return {"workspace": w.name, "total_matching": total, "videos": [_video(v, cc) for v, cc in rows]}


async def video_suggest(c: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    topic = str(args.get("topic") or "").strip()
    args = {
        **args,
        "keyword": topic or None,
        "min_outlier": args.get("min_outlier", 3),
        "limit": args.get("limit", 10),
    }
    res = await videos_watchlist(c, args)
    res["hint"] = (
        "Use `analyze` on a video_id for its transcript, hook, format and visual breakdown (1 credit)."
    )
    return res


def _analysis(a: VideoAnalysis) -> dict[str, Any]:
    return {
        "status": a.status,
        "error": a.error,
        "summary": a.summary,
        "hook": a.hook,
        "hook_category": a.hook_category,
        "format": a.format,
        "visual": a.visual,
        "idea": a.idea,
        "transcript": a.transcript,
    }


async def video_details(c: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    w = await _workspace(c, args)
    vid = _uuid(args.get("video_id"), "video")
    v, cc = await an.get_workspace_video(c.db, c.profile, w.id, vid)
    a = await an.get_analysis(c.db, w.id, vid)
    return {"workspace": w.name, "video": _video(v, cc), "analysis": _analysis(a) if a else None}


async def analyze(c: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """1 credit, only on success — the same rule as the app. Re-reading a finished analysis is free."""
    w = await _workspace(c, args)
    vid = _uuid(args.get("video_id"), "video")
    a, should_run = await an.start_analysis(c.db, c.profile, w.id, vid)
    if should_run:
        await an.run_analysis(c.session_factory, c.llm, c.provider, a.id)
    a2 = await c.db.scalar(
        select(VideoAnalysis).where(VideoAnalysis.id == a.id).execution_options(populate_existing=True)
    )
    assert a2 is not None  # noqa: S101
    v, cc = await an.get_workspace_video(c.db, c.profile, w.id, vid)
    note = {
        "complete": "Done — 1 credit was used." if should_run else "Already analyzed — no credit used.",
        "failed": "The analysis failed — no credit was used.",
    }.get(a2.status, "Still running — call video-details again in a minute.")
    return {"workspace": w.name, "video": _video(v, cc), "analysis": _analysis(a2), "note": note}


async def topic(c: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    w = await _workspace(c, args)
    t = str(args.get("topic") or "").strip()
    if len(t) < 2:
        raise ch.InvalidChannel("Give a topic or niche (2+ characters).")
    ideas = await studio.generate_ideas(c.db, c.profile, w.id, c.llm, t, _int(args, "count", 5, 1, 10))
    return {
        "workspace": w.name,
        "saved_to_ideas": True,
        "ideas": [
            {
                "title": i.title,
                "positioning": i.positioning,
                "format": i.format,
                "contrast": i.contrast,
                "make_it_yours": i.personalization,
            }
            for i in ideas
        ],
    }


async def hooks_watchlist(c: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    w = await _workspace(c, args)
    rows = await c.db.scalars(
        select(SavedHook)
        .where(SavedHook.workspace_id == w.id)
        .order_by(SavedHook.created_at.desc())
        .limit(100)
    )
    return {
        "workspace": w.name,
        "hooks": [{"text": h.text, "category": h.category, "source": h.source} for h in rows],
    }


async def formats_watchlist(c: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    w = await _workspace(c, args)
    rows = (
        await c.db.execute(
            select(VideoAnalysis, Video, Channel)
            .join(Video, Video.id == VideoAnalysis.video_id)
            .join(Channel, Channel.id == Video.channel_id)
            .where(VideoAnalysis.workspace_id == w.id, VideoAnalysis.status == "complete")
            .order_by(Video.outlier_score.desc().nulls_last())
            .limit(50)
        )
    ).all()
    return {
        "workspace": w.name,
        "formats": [
            {
                "format": a.format,
                "hook_category": a.hook_category,
                "visual": a.visual,
                "example": _video(v, cc),
            }
            for a, v, cc in rows
        ],
    }


TOOLS: list[Tool] = [
    Tool("channels-search", "Search creators",
         "Find creators by name or exact @handle, or describe a niche in a few words to get AI-suggested creators "
         "(each one is checked to exist).",
         {"query": {"type": "string", "description": "Name, @handle, or a niche description"}, "platform": PLATFORM},
         channels_search, ["query"]),
    Tool("channels-add", "Track a creator",
         "Add a creator to the workspace watchlist (by profile URL, or platform + handle). Their recent videos are "
         "fetched and scored for outliers.",
         {"url": {"type": "string"}, "platform": PLATFORM, "handle": {"type": "string"}},
         channels_add, read_only=False),
    Tool("channels-suggest", "Suggested creators", "Creators that other Smart Content users track and you don't yet.",
         {}, channels_suggest),
    Tool("channels-recap", "Watchlist recap",
         "For each tracked creator (or one @handle): followers, outlier count and their top videos in the last N days.",
         {"handle": {"type": "string"}, "days": {"type": "integer", "minimum": 1, "maximum": 365}},
         channels_recap),
    Tool("videos-watchlist", "Outlier videos",
         "Videos from your tracked creators with filters. outlier_score = views ÷ the creator's median of their "
         "previous 30 videos (5x+ is a strong outlier).",
         {"min_outlier": {"type": "number"}, "platform": PLATFORM, "days": {"type": "integer"},
          "keyword": {"type": "string"}, "sort": {"type": "string", "enum": ["outlier", "views", "engagement", "newest"]},
          "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
         videos_watchlist),
    Tool("video-suggest", "Videos to study", "The strongest outliers about a topic from your watchlist — good candidates to analyze.",
         {"topic": {"type": "string"}, "min_outlier": {"type": "number"}, "limit": {"type": "integer"}},
         video_suggest),
    Tool("video-details", "Video details", "One video's numbers plus its analysis (transcript, hook, format, visual) if it was analyzed. Free.",
         {"video_id": {"type": "string"}}, video_details, ["video_id"]),
    Tool("analyze", "Analyze a video",
         "Deep-analyze a video: transcript, summary, hook, format, visual breakdown and an idea. Costs 1 credit, "
         "charged only if it succeeds; re-reading an analyzed video is free.",
         {"video_id": {"type": "string"}}, analyze, ["video_id"], read_only=False),
    Tool("topic", "Ideas for a topic", "Generate original video ideas for a topic or niche and save them to Ideas. Free.",
         {"topic": {"type": "string"}, "count": {"type": "integer", "minimum": 1, "maximum": 10}},
         topic, ["topic"], read_only=False),
    Tool("hooks-watchlist", "Saved hooks", "The hooks saved in this workspace.", {}, hooks_watchlist),
    Tool("formats-watchlist", "Winning formats", "Formats, hook types and visual styles from your analyzed outliers.", {}, formats_watchlist),
]  # fmt: skip
TOOL_NAMES = [t.name for t in TOOLS]
BY_NAME = {t.name: t for t in TOOLS}


def allowed_tools(profile: Profile) -> list[Tool]:
    perms = profile.mcp_tool_permissions or {}
    return [t for t in TOOLS if perms.get(t.name, "allow") != "block"]


async def call_tool(c: ToolContext, name: str, args: dict[str, Any]) -> dict[str, Any]:
    tool = next((t for t in allowed_tools(c.profile) if t.name == name), None)
    if tool is None:
        raise AppError(
            f"The tool '{name}' is blocked or doesn't exist. Change tool permissions in Smart Content → Settings → Connectors.",
            code="tool_unavailable",
        )
    return await tool.handler(c, args)
