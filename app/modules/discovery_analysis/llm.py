"""AI analysis providers behind one interface (decision D11: Gemini; every provider swappable).

- FakeLLMProvider: deterministic demo analysis, no network. Default (LLM_PROVIDER=fake) and in tests.
- GeminiLLMProvider: Gemini Interactions API. YouTube videos are passed by URL (Gemini reads them
  directly); TikTok/Instagram videos are downloaded once into memory from a fresh direct link and sent
  inline. The file is never stored.

⚠ The Interactions request/response shape follows ai.google.dev (checked 2026-10-23) but must be
verified on the first real call with a key. Parsing is defensive and any surprise becomes a
ProviderError → analysis marked failed → no credit charged.
"""

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import socket
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal, Protocol
from urllib.parse import urljoin, urlparse

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.core.config import Settings, get_settings
from app.modules.discovery_analysis.providers import ProviderError

log = logging.getLogger("smart_content.llm")

HookCategory = Literal["contrarian", "curiosity_gap", "question", "unexpected_claim", "problem", "other"]


@dataclass
class VideoContext:
    platform: str
    url: str
    caption: str
    creator_handle: str
    duration_sec: int | None
    views: int
    outlier_score: float | None
    # Direct, short-lived media link (TikTok/Instagram). None for YouTube (Gemini reads the URL).
    media_url: str | None = None


class TranscriptLine(BaseModel):
    t: str = Field(max_length=8)  # "MM:SS"
    text: str = Field(max_length=2000)


class IdeaDraft(BaseModel):
    title: str = Field(max_length=200)
    positioning: str = Field(max_length=1000)
    format: str = Field(max_length=500)
    contrast: str = Field(max_length=1000)
    personalization: str = Field(max_length=1000)


class AnalysisResult(BaseModel):
    summary: str = Field(max_length=4000)
    transcript: list[TranscriptLine] = Field(max_length=400)
    hook: str = Field(max_length=2000)
    hook_category: HookCategory = "other"
    format: str = Field(max_length=2000)
    visual: str = Field(max_length=2000)
    idea: str = Field(max_length=2000)
    idea_draft: IdeaDraft


class LLMProvider(Protocol):
    name: str
    model: str

    async def analyze_video(self, ctx: VideoContext) -> AnalysisResult:
        """Watch the video and return the structured breakdown. Raise ProviderError on failure."""
        ...


# ------------------------------------------------------------------------------------------ fake


class FakeLLMProvider:
    """Deterministic demo output. Captions containing 'failanalysis' fail (to exercise no-charge)."""

    name = "fake"
    model = "fake-analyst-1"

    async def analyze_video(self, ctx: VideoContext) -> AnalysisResult:
        if "failanalysis" in ctx.caption.lower():
            raise ProviderError("The AI couldn't process this video. No credit was charged.")
        k = int(hashlib.sha256(ctx.url.encode()).hexdigest()[:8], 16)
        topic = (ctx.caption.split("—")[0].split("#")[0].strip() or "this video")[:80]
        cats: list[HookCategory] = ["contrarian", "curiosity_gap", "question", "unexpected_claim", "problem"]
        cat = cats[k % len(cats)]
        dur = ctx.duration_sec or 30
        lines = [
            "Stop scrolling — most people get this completely wrong.",
            f"Here's what @{ctx.creator_handle} figured out about {topic.lower()}.",
            "First, the setup: one clear promise in the opening two seconds.",
            "Then a quick proof point so the viewer believes it.",
            "Now the twist nobody expects — and that's why it gets shared.",
            "Follow for part two.",
        ]
        step = max(2, dur // len(lines))
        return AnalysisResult(
            summary=(
                f"A {dur}-second {ctx.platform} short from @{ctx.creator_handle} built around one bold "
                f"promise about {topic.lower()}, a fast proof point and a twist ending. It earned "
                f"{ctx.views:,} views"
                + (f" ({ctx.outlier_score}x the creator's median)." if ctx.outlier_score else ".")
            ),
            transcript=[
                TranscriptLine(t=f"{(i * step) // 60}:{(i * step) % 60:02d}", text=line)
                for i, line in enumerate(lines)
            ],
            hook=f'"{lines[0]}" — a pattern-interrupt that challenges what the viewer already believes.',
            hook_category=cat,
            format="Talking head → proof cut-in → twist reveal → CTA. One idea, no filler, under a minute.",
            visual="Tight face framing, bold burned-in captions, a jump cut every 1–2 seconds, b-roll on the proof.",
            idea=f"Make your own take on '{topic}' for your audience, keeping the same promise → proof → twist arc.",
            idea_draft=IdeaDraft(
                title=f"The truth about {topic.lower()} (that nobody tells you)",
                positioning="Contrarian expert: say the quiet part out loud.",
                format="Talking head with one proof cut-in and a twist at the end.",
                contrast="Show the common mistake first, then the better way.",
                personalization="Use a result from your own work as the proof point.",
            ),
        )


# ------------------------------------------------------------------------------------------ media safety

# Direct media links come from a third party (the scraper), so they are treated as untrusted:
# HTTPS only, known media hosts only, every redirect re-checked, no private/internal addresses,
# video content type, hard size + time limits. Never stored.
ALLOWED_MEDIA_HOST_SUFFIXES = (
    "api.apify.com",  # Apify key-value store (downloaded videos)
    "apifyusercontent.com",
    "tiktokcdn.com",
    "tiktokcdn-us.com",
    "tiktokv.com",
    "tiktok.com",
    "byteoversea.com",
    "ibyteimg.com",
    "cdninstagram.com",
    "fbcdn.net",
)
MAX_REDIRECTS = 3


def _host_allowed(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(host == s or host.endswith("." + s) for s in ALLOWED_MEDIA_HOST_SUFFIXES)


async def _resolves_public(host: str, port: int) -> bool:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return False
    return bool(infos)


def check_media_url(url: str) -> str:
    """Static checks (scheme + host allow-list). Returns the host. Raises ProviderError."""
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        raise ProviderError("This video's media link isn't safe to open. No credit was charged.")
    if not _host_allowed(p.hostname):
        log.warning("media host not allowed: %s", p.hostname)
        raise ProviderError("This video's media link isn't from a trusted host. No credit was charged.")
    return p.hostname


async def download_media(client: httpx.AsyncClient, url: str, max_bytes: int) -> tuple[bytes, str]:
    """Fetch a short video into memory with the safety rules above. Returns (bytes, mime_type)."""
    for _ in range(MAX_REDIRECTS + 1):
        host = check_media_url(url)
        if not await _resolves_public(host, 443):
            raise ProviderError("This video's media link isn't safe to open. No credit was charged.")
        try:
            async with client.stream("GET", url, follow_redirects=False) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code >= 400:
                    raise ProviderError(
                        "The video link has expired or is blocked. Refresh the channel and try again — "
                        "no credit was charged."
                    )
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                if not (
                    ctype.startswith("video/") or ctype in ("application/octet-stream", "binary/octet-stream")
                ):
                    raise ProviderError("That link didn't return a video. No credit was charged.")
                declared = int(r.headers.get("content-length") or 0)
                if declared > max_bytes:
                    raise ProviderError("This video is too large to analyze. No credit was charged.")
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        raise ProviderError("This video is too large to analyze. No credit was charged.")
                if not buf:
                    raise ProviderError("The video file was empty. No credit was charged.")
                return bytes(buf), ctype if ctype.startswith("video/") else "video/mp4"
        except httpx.HTTPError as e:
            raise ProviderError(
                "Couldn't download the video. Try again shortly — no credit was charged."
            ) from e
    raise ProviderError("The video link redirected too many times. No credit was charged.")


# ------------------------------------------------------------------------------------------ gemini

PROMPT = """You are a short-form video strategist. Watch this {platform} video by @{handle}
({views} views{outlier}). Caption: {caption!r}

Return JSON only, matching the schema:
- summary: 2-4 sentences on what happens and why it performed.
- transcript: every spoken line with its start time as "MM:SS" (use on-screen text if there is no speech;
  empty list if neither).
- hook: the opening line/visual and why it stops the scroll. hook_category: one of the allowed values.
- format: the structure, beat by beat.
- visual: framing, editing pace, captions, b-roll, text overlays.
- idea: one sentence on how another creator could make their own version (do not copy it).
- idea_draft: title, positioning, format, contrast (what to do differently), personalization.
Describe; never invent facts that aren't in the video."""

ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "transcript": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"t": {"type": "string"}, "text": {"type": "string"}},
                "required": ["t", "text"],
            },
        },
        "hook": {"type": "string"},
        "hook_category": {
            "type": "string",
            "enum": ["contrarian", "curiosity_gap", "question", "unexpected_claim", "problem", "other"],
        },
        "format": {"type": "string"},
        "visual": {"type": "string"},
        "idea": {"type": "string"},
        "idea_draft": {
            "type": "object",
            "properties": {
                k: {"type": "string"}
                for k in ("title", "positioning", "format", "contrast", "personalization")
            },
            "required": ["title", "positioning", "format", "contrast", "personalization"],
        },
    },
    "required": ["summary", "transcript", "hook", "hook_category", "format", "visual", "idea", "idea_draft"],
}


def extract_output_text(data: dict[str, Any]) -> str:
    """Pull the model's text out of an Interaction (steps[].content[].text), tolerating older shapes."""
    texts: list[str] = []
    for step in data.get("steps") or []:
        if isinstance(step, dict) and step.get("type", "model_output") == "model_output":
            for c in step.get("content") or []:
                if isinstance(c, dict) and c.get("type", "text") == "text" and c.get("text"):
                    texts.append(c["text"])
    if not texts and isinstance(data.get("output_text"), str):
        texts.append(data["output_text"])
    if not texts:  # generateContent-style fallback
        for cand in data.get("candidates") or []:
            for part in (cand.get("content") or {}).get("parts") or []:
                if part.get("text"):
                    texts.append(part["text"])
    return "".join(texts).strip()


def parse_result(text: str) -> AnalysisResult:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()
    try:
        return AnalysisResult.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as e:
        log.warning("gemini returned unparseable analysis: %s", str(e)[:300])
        raise ProviderError("The AI returned an incomplete analysis. No credit was charged.") from e


class GeminiLLMProvider:
    name = "gemini"
    BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.s = settings
        self.model = settings.gemini_model
        self.client = client or httpx.AsyncClient(timeout=settings.gemini_timeout_seconds)

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self.s.gemini_api_key, "Content-Type": "application/json"}

    async def _video_part(self, ctx: VideoContext) -> dict[str, Any]:
        if ctx.platform == "youtube":
            host = (urlparse(ctx.url).hostname or "").lower()
            if not (host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")):
                raise ProviderError("Not a YouTube link. No credit was charged.")
            return {"type": "video", "uri": ctx.url}
        if not ctx.media_url:
            raise ProviderError(
                "We couldn't get this video's file. Try again shortly — no credit was charged."
            )
        data, mime = await download_media(
            self.client, ctx.media_url, self.s.gemini_max_video_mb * 1024 * 1024
        )
        return {"type": "video", "data": base64.b64encode(data).decode(), "mime_type": mime}

    async def analyze_video(self, ctx: VideoContext) -> AnalysisResult:
        prompt = PROMPT.format(
            platform=ctx.platform,
            handle=ctx.creator_handle,
            views=f"{ctx.views:,}",
            outlier=f", {ctx.outlier_score}x the creator's median" if ctx.outlier_score else "",
            caption=ctx.caption[:1500],
        )
        body = {
            "model": self.model,
            "input": [await self._video_part(ctx), {"type": "text", "text": prompt}],
            "response_format": {"type": "text", "mime_type": "application/json", "schema": ANALYSIS_SCHEMA},
        }
        try:
            r = await self.client.post(f"{self.BASE}/interactions", json=body, headers=self._headers())
            data = r.json() if r.content else {}
            # Long jobs may come back in progress — poll until done or timeout.
            waited = 0
            while r.status_code < 400 and data.get("status") == "in_progress" and data.get("id"):
                if waited >= self.s.gemini_timeout_seconds:
                    raise ProviderError("The AI took too long. No credit was charged.")
                await asyncio.sleep(3)
                waited += 3
                r = await self.client.get(f"{self.BASE}/interactions/{data['id']}", headers=self._headers())
                data = r.json() if r.content else {}
        except httpx.HTTPError as e:
            raise ProviderError(
                "Couldn't reach the AI service. Try again shortly — no credit was charged."
            ) from e
        except ValueError as e:  # non-JSON body
            raise ProviderError(
                "The AI service returned an unexpected response. No credit was charged."
            ) from e
        if r.status_code >= 400:
            log.warning("gemini error %s: %s", r.status_code, str(data)[:300])
            msg = (
                "The AI service is busy. Try again in a minute — no credit was charged."
                if r.status_code == 429
                else "The AI service returned an error. No credit was charged."
            )
            raise ProviderError(msg)
        if data.get("status") in ("failed", "cancelled", "incomplete"):
            log.warning("gemini interaction %s: %s", data.get("status"), str(data.get("errors"))[:300])
            raise ProviderError("The AI couldn't finish this analysis. No credit was charged.")
        return parse_result(extract_output_text(data))


@lru_cache
def _gemini() -> GeminiLLMProvider:
    return GeminiLLMProvider(get_settings())


def get_llm_provider() -> LLMProvider:
    """FastAPI dependency (overridden in tests)."""
    if get_settings().llm_provider == "gemini":
        return _gemini()
    return FakeLLMProvider()
