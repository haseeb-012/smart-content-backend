"""Pydantic request/response models — this IS the API contract (mirrored in docs/API_CONTRACTS.md)."""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

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


class WaitlistEntryOut(Out):
    email: str
    plan: str
    source: str
    created_at: datetime
