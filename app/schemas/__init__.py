"""Pydantic request/response models — this IS the API contract (mirrored in docs/API_CONTRACTS.md)."""

import uuid
from datetime import date, datetime
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, EmailStr, Field, ValidationInfo, field_validator

PlanId = Literal["pro", "visionary", "titan"]


class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    database: Literal["ok", "error"]


class PlanOut(BaseModel):
    id: PlanId
    name: str
    credits: int
    workspaces: int
    competitors_per_workspace: int
    verified_channels: int
    refresh_hours: int
    features: list[str]
    monthly_usd: int
    yearly_usd: int


class CreditsOut(BaseModel):
    allowance: int
    used: int
    adjustments: int
    remaining: int
    period_start: date
    resets_on: date


class MeOut(Out):
    id: uuid.UUID
    email: str | None
    full_name: str | None
    plan: PlanId
    is_admin: bool
    mcp_enabled: bool
    created_at: datetime


class MeResponse(BaseModel):
    profile: MeOut
    plan: PlanOut
    credits: CreditsOut
    billing: dict[str, str | bool]


class MeUpdate(BaseModel):
    # Email changes are intentionally not supported in v1 (FR-AUTH-06).
    model_config = ConfigDict(extra="forbid")
    full_name: str = Field(min_length=1, max_length=120)


class WorkspaceOut(Out):
    id: uuid.UUID
    name: str
    is_active: bool
    role: Literal["owner", "editor", "guest"]
    created_at: datetime


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class WorkspaceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=80)
    is_active: bool | None = None


class LedgerEntryOut(Out):
    id: uuid.UUID
    kind: Literal["usage", "adjustment"]
    amount: int
    action: str
    workspace_id: uuid.UUID | None
    created_at: datetime


class CreditHistory(BaseModel):
    credits: CreditsOut
    entries: list[LedgerEntryOut]


class WaitlistIn(BaseModel):
    email: EmailStr
    plan: PlanId
    source: Literal["landing", "app"] = "landing"


class WaitlistOut(BaseModel):
    ok: bool
    already_joined: bool


class AdminPlanIn(BaseModel):
    plan: PlanId


class AdminCreditsIn(BaseModel):
    amount: int = Field(ge=-10_000, le=10_000)
    reason: str = Field(min_length=3, max_length=200)


class AdminFlagsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mcp_enabled: bool | None = None
    is_admin: bool | None = None


class AdminUserOut(BaseModel):
    profile: MeOut
    credits: CreditsOut


class AuditOut(Out):
    id: uuid.UUID
    actor_id: uuid.UUID | None
    target_user_id: uuid.UUID | None
    action: str
    details: dict
    created_at: datetime


CollectionCategory = Literal["formats", "editing_styles", "visual_hooks", "signature_series"]
Platform = Literal["tiktok", "instagram", "youtube"]
PLATFORM_HOSTS = {
    "tiktok": ("tiktok.com",),
    "instagram": ("instagram.com",),
    "youtube": ("youtube.com", "youtu.be"),
}


class CollectionSummary(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    category: CollectionCategory
    sub: str
    description: str
    cover_hue: str
    video_count: int
    is_published: bool
    updated_at: datetime


class CollectionVideoOut(Out):
    id: uuid.UUID
    position: int
    platform: Platform
    url: str
    title: str
    creator_handle: str
    views: int | None
    outlier_score: float | None
    transcript: str
    notes: str


class CollectionDetail(CollectionSummary):
    breakdown: list[str]
    videos: list[CollectionVideoOut]


class CollectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", min_length=2, max_length=80)
    name: str = Field(min_length=2, max_length=120)
    category: CollectionCategory
    sub: str = Field(default="", max_length=80)
    description: str = Field(default="", max_length=2000)
    breakdown: list[str] = Field(default_factory=list, max_length=20)
    cover_hue: str = Field(default="", max_length=160)
    is_published: bool = False


class CollectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", min_length=2, max_length=80)
    name: str | None = Field(default=None, min_length=2, max_length=120)
    category: CollectionCategory | None = None
    sub: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=2000)
    breakdown: list[str] | None = Field(default=None, max_length=20)
    cover_hue: str | None = Field(default=None, max_length=160)
    is_published: bool | None = None


class CollectionVideoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: Platform
    url: str = Field(max_length=500)
    title: str = Field(min_length=1, max_length=300)
    creator_handle: str = Field(default="", max_length=100)
    views: int | None = Field(default=None, ge=0)
    outlier_score: float | None = Field(default=None, ge=0)
    transcript: str = Field(default="", max_length=20_000)
    notes: str = Field(default="", max_length=2000)
    position: int | None = Field(default=None, ge=0)

    @field_validator("url")
    @classmethod
    def https_platform_url(cls, v: str, info: ValidationInfo) -> str:
        parsed = urlparse(v)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host:
            raise ValueError("URL must be an https link.")
        platform = info.data.get("platform")
        allowed = PLATFORM_HOSTS.get(platform or "", ())
        if allowed and not any(host == h or host.endswith("." + h) for h in allowed):
            raise ValueError(f"URL doesn't match platform '{platform}'.")
        return v

    @field_validator("creator_handle")
    @classmethod
    def strip_at(cls, v: str) -> str:
        return v.strip().lstrip("@")


class ChannelOut(Out):
    id: uuid.UUID
    platform: Platform
    handle: str
    display_name: str
    avatar_url: str | None
    followers: int | None
    total_views: int | None
    video_count: int
    sync_status: Literal["pending", "ok", "error"]
    sync_error: str | None
    last_synced_at: datetime | None


class WatchlistItem(ChannelOut):
    outlier_videos: int
    added_at: datetime


class ChannelAdd(BaseModel):
    """Add by URL, or by platform + handle (FR-CH-01)."""

    model_config = ConfigDict(extra="forbid")
    url: str | None = Field(default=None, max_length=500)
    platform: Platform | None = None
    handle: str | None = Field(default=None, max_length=100)


class ChannelAddResult(BaseModel):
    channel: ChannelOut
    added: bool


class ChannelSearchResult(BaseModel):
    platform: Platform
    handle: str
    display_name: str
    followers: int | None
    channel_id: uuid.UUID | None  # None = found on the platform but not stored yet
    tracked: bool


class FeedVideo(BaseModel):
    id: uuid.UUID
    platform: Platform
    url: str
    caption: str
    thumbnail_url: str | None
    posted_at: datetime | None
    duration_sec: int | None
    views: int
    likes: int
    comments: int
    shares: int
    engagement_rate: float
    outlier_score: float | None
    channel: ChannelOut


class FeedPage(BaseModel):
    items: list[FeedVideo]
    total: int
    limit: int
    offset: int


class WaitlistEntryOut(Out):
    email: str
    plan: str
    source: str
    created_at: datetime
