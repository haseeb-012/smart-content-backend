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


BLOCK_KEYS = ("hook", "intro", "main", "story", "cta", "ending")


class ScriptBlocks(BaseModel):
    hook: str = Field(max_length=1000)
    intro: str = Field(max_length=2000)
    main: str = Field(max_length=6000)
    story: str = Field(max_length=3000)
    cta: str = Field(max_length=1000)
    ending: str = Field(max_length=1000)


class HookDraft(BaseModel):
    text: str = Field(max_length=300)
    category: HookCategory = "other"


class HookList(BaseModel):
    hooks: list[HookDraft] = Field(max_length=60)


class IdeaList(BaseModel):
    ideas: list[IdeaDraft] = Field(max_length=20)


class NicheMatches(BaseModel):
    handles: list[str] = Field(max_length=20)


class LLMProvider(Protocol):
    name: str
    model: str

    async def analyze_video(self, ctx: VideoContext) -> AnalysisResult:
        """Watch the video and return the structured breakdown. Raise ProviderError on failure."""
        ...

    async def write_script(self, topic: str, hook: str | None, context: str | None) -> ScriptBlocks:
        """Draft a short-form script in six blocks."""
        ...

    async def expand_script(self, blocks: ScriptBlocks) -> ScriptBlocks:
        """Make the intro/main/story richer while keeping the creator's wording."""
        ...

    async def generate_hooks(self, topic: str, category: str | None, count: int) -> list[HookDraft]: ...

    async def generate_ideas(self, topic: str, count: int, context: str | None) -> list[IdeaDraft]: ...

    async def suggest_creators(self, description: str, platform: str | None) -> list[str]:
        """Creator handles (no @) likely to match a niche description. Verified by the caller."""
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

    async def write_script(self, topic: str, hook: str | None, context: str | None) -> ScriptBlocks:
        t = topic.strip().rstrip(".") or "your topic"
        if "failscript" in t.lower():
            raise ProviderError("The AI couldn't write this script. No credit was charged.")
        return ScriptBlocks(
            hook=hook or f"Nobody tells you this about {t.lower()}…",
            intro="I studied the top outliers in my niche this month, and they all do one thing differently.",
            main=(
                "First, they open on a claim, not a greeting. Second, every beat is under eight seconds. "
                "Third, they pay off the promise before the viewer can scroll."
            ),
            story=(
                "When I tried this on my last video, my average watch time went up and the comments asked "
                "for part two." + (f" (Inspired by: {context[:120]})" if context else "")
            ),
            cta="Follow so you don't miss part two.",
            ending="Try it on your next video and tell me how it goes.",
        )

    async def expand_script(self, blocks: ScriptBlocks) -> ScriptBlocks:
        return blocks.model_copy(
            update={
                "intro": f"{blocks.intro} I'll show you exactly what to change, step by step.",
                "main": (
                    f"{blocks.main} Here's an example: instead of opening with a greeting, start with the "
                    "result. Then show the proof on screen while you explain the first step."
                ),
                "story": f"{blocks.story} The surprising part: it took less effort, not more.",
            }
        )

    async def generate_hooks(self, topic: str, category: str | None, count: int) -> list[HookDraft]:
        t = topic.strip() or "your niche"
        tpl: list[tuple[HookCategory, str]] = [
            ("contrarian", "Stop doing {x} the way everyone tells you to"),
            ("contrarian", "{X} is overrated — here's what works instead"),
            ("curiosity_gap", "The one thing about {x} nobody talks about"),
            ("curiosity_gap", "I tried {x} for 30 days. Day 12 changed everything"),
            ("question", "Why does nobody explain {x} like this?"),
            ("question", "What if {x} is easier than you think?"),
            ("unexpected_claim", "{X} took me 10 minutes, not 10 hours"),
            ("unexpected_claim", "The cheapest way to get better at {x} is free"),
            ("problem", "If {x} isn't working for you, it's probably this"),
            ("problem", "The mistake that's killing your {x} results"),
        ]
        pool = [(c, line) for c, line in tpl if not category or category == c] or tpl
        extras = [
            "",
            " (in 30 seconds)",
            " — the honest version",
            " before it's too late",
            " (from someone who failed first)",
        ]
        out: list[HookDraft] = []
        for i in range(len(pool) * len(extras)):
            c, line = pool[i % len(pool)]
            text = (
                line.replace("{x}", t.lower()).replace("{X}", t[:1].upper() + t[1:]) + extras[i // len(pool)]
            )
            out.append(HookDraft(text=text, category=c))
            if len(out) >= count:
                break
        return out

    async def generate_ideas(self, topic: str, count: int, context: str | None) -> list[IdeaDraft]:
        t = topic.strip() or "your niche"
        angles = [
            ("Myth vs fact", "Bust the most common belief", "Myth first, then the evidence."),
            ("Before / after", "Show the transformation", "Start with the 'after', rewind to the 'before'."),
            ("Listicle countdown", "3 things I'd do differently", "Count down to the most surprising one."),
            ("Case study", "Break down one real example", "Result first, then how it happened."),
            ("Day in the life", "Behind the scenes", "Real moments, captions explain the lesson."),
        ]
        return [
            IdeaDraft(
                title=f"{a[1]}: {t}",
                positioning="Speak as someone a few steps ahead of the viewer.",
                format=a[0],
                contrast=a[2],
                personalization="Use one number from your own results and one mistake you made.",
            )
            for a in (angles * 4)[:count]
        ]

    async def suggest_creators(self, description: str, platform: str | None) -> list[str]:
        words = [w.strip(".,!?").lower() for w in description.split() if len(w) > 3]
        return [f"{w}.daily" for w in words[:3]] + [f"the{w}coach" for w in words[:2]]


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


async def download_media(
    client: httpx.AsyncClient, url: str, max_bytes: int, host_auth: dict[str, str] | None = None
) -> tuple[bytes, str]:
    """Fetch a short video into memory with the safety rules above. Returns (bytes, mime_type).
    `host_auth` maps an exact host to an Authorization header value, sent ONLY to that host
    (re-checked on every redirect) — used for Apify storage, which needs the Apify token."""
    for _ in range(MAX_REDIRECTS + 1):
        host = check_media_url(url)
        if not await _resolves_public(host, 443):
            raise ProviderError("This video's media link isn't safe to open. No credit was charged.")
        headers = (
            {"Authorization": host_auth[host.lower()]} if host_auth and host.lower() in host_auth else {}
        )
        try:
            async with client.stream("GET", url, follow_redirects=False, headers=headers) as r:
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


def parse_json[T: BaseModel](text: str, model: type[T]) -> T:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()
    try:
        return model.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as e:
        log.warning("gemini returned unparseable %s: %s", model.__name__, str(e)[:300])
        raise ProviderError("The AI returned an incomplete answer. No credit was charged.") from e


def parse_result(text: str) -> AnalysisResult:
    return parse_json(text, AnalysisResult)


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props)}


_STR: dict[str, Any] = {"type": "string"}
SCRIPT_SCHEMA = _obj({k: _STR for k in BLOCK_KEYS})
HOOKS_SCHEMA = _obj(
    {
        "hooks": {
            "type": "array",
            "items": _obj({"text": _STR, "category": ANALYSIS_SCHEMA["properties"]["hook_category"]}),
        }
    }
)
IDEAS_SCHEMA = _obj({"ideas": {"type": "array", "items": ANALYSIS_SCHEMA["properties"]["idea_draft"]}})
NICHE_SCHEMA = _obj({"handles": {"type": "array", "items": _STR}})
SCRIPT_RULES = (
    "Blocks: hook (first 1-3 seconds, a promise), intro (why keep watching), main (the beats that deliver "
    "the promise), story (a moment, result or example), cta (one clear ask), ending (close the loop). "
    "Spoken, short sentences, no stage directions, no hashtags. Never copy another creator's script."
)


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
        auth = {"api.apify.com": f"Bearer {self.s.apify_token}"} if self.s.apify_token else None
        data, mime = await download_media(
            self.client, ctx.media_url, self.s.gemini_max_video_mb * 1024 * 1024, host_auth=auth
        )
        return {"type": "video", "data": base64.b64encode(data).decode(), "mime_type": mime}

    async def _call(self, parts: list[dict[str, Any]] | str, schema: dict[str, Any]) -> str:
        """One Interactions request with a JSON-schema answer. Returns the model's text."""
        body = {
            "model": self.model,
            "input": parts,
            "response_format": {"type": "text", "mime_type": "application/json", "schema": schema},
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
            raise ProviderError("The AI couldn't finish this request. No credit was charged.")
        return extract_output_text(data)

    async def analyze_video(self, ctx: VideoContext) -> AnalysisResult:
        prompt = PROMPT.format(
            platform=ctx.platform,
            handle=ctx.creator_handle,
            views=f"{ctx.views:,}",
            outlier=f", {ctx.outlier_score}x the creator's median" if ctx.outlier_score else "",
            caption=ctx.caption[:1500],
        )
        text = await self._call(
            [await self._video_part(ctx), {"type": "text", "text": prompt}], ANALYSIS_SCHEMA
        )
        return parse_result(text)

    async def write_script(self, topic: str, hook: str | None, context: str | None) -> ScriptBlocks:
        prompt = (
            f"Write a short-form video script (30-60 seconds) about: {topic[:500]!r}.\n"
            + (f"Use exactly this hook: {hook[:300]!r}.\n" if hook else "")
            + (f"Inspiration (structure only, don't copy): {context[:1500]!r}.\n" if context else "")
            + SCRIPT_RULES
        )
        return parse_json(await self._call(prompt, SCRIPT_SCHEMA), ScriptBlocks)

    async def expand_script(self, blocks: ScriptBlocks) -> ScriptBlocks:
        prompt = (
            "Expand this short-form script: make intro, main and story more detailed and concrete (about "
            "1.5x longer), keep hook, cta and ending unless they are unclear. Keep the creator's voice.\n"
            f"{blocks.model_dump_json()}\n{SCRIPT_RULES}"
        )
        return parse_json(await self._call(prompt, SCRIPT_SCHEMA), ScriptBlocks)

    async def generate_hooks(self, topic: str, category: str | None, count: int) -> list[HookDraft]:
        prompt = (
            f"Write {count} distinct scroll-stopping opening lines for short-form videos about {topic[:300]!r}"
            + (f", all of category {category}" if category else "")
            + ". Max 15 words each, no emojis, no hashtags, no clickbait lies."
        )
        return parse_json(await self._call(prompt, HOOKS_SCHEMA), HookList).hooks[:count]

    async def generate_ideas(self, topic: str, count: int, context: str | None) -> list[IdeaDraft]:
        prompt = (
            f"Give {count} original short-form video ideas for a creator in the niche {topic[:300]!r}."
            + (f" Their recent outliers for context: {context[:1500]!r}." if context else "")
            + " For each: title, positioning, format, contrast (what to do differently), personalization."
        )
        return parse_json(await self._call(prompt, IDEAS_SCHEMA), IdeaList).ideas[:count]

    async def suggest_creators(self, description: str, platform: str | None) -> list[str]:
        where = f" on {platform}" if platform else ""
        prompt = (
            f"List up to 10 real, currently active short-form creators{where} who match this niche: "
            f"{description[:300]!r}. Return only their exact handles without @. "
            "If unsure a handle exists, leave it out."
        )
        handles = parse_json(await self._call(prompt, NICHE_SCHEMA), NicheMatches).handles
        return [h.strip().lstrip("@").lower() for h in handles if h.strip()][:10]


@lru_cache
def _gemini() -> GeminiLLMProvider:
    return GeminiLLMProvider(get_settings())


def get_llm_provider() -> LLMProvider:
    """FastAPI dependency (overridden in tests)."""
    if get_settings().llm_provider == "gemini":
        return _gemini()
    return FakeLLMProvider()
