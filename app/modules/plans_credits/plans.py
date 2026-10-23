"""Plan limits (PRD §4.5). Gating code reads these via the user's `plan` attribute only,
so activating real billing later changes the attribute, not this code."""

from dataclasses import dataclass
from typing import Literal

PlanId = Literal["pro", "visionary", "titan"]
Feature = Literal["mcp", "api", "guests"]


@dataclass(frozen=True)
class PlanLimits:
    id: PlanId
    name: str
    credits: int
    workspaces: int
    competitors_per_workspace: int
    verified_channels: int
    refresh_hours: int
    features: frozenset[str]
    monthly_usd: int
    yearly_usd: int


PLANS: dict[str, PlanLimits] = {
    "pro": PlanLimits("pro", "Pro", 100, 3, 150, 3, 12, frozenset({"mcp"}), 49, 39),
    "visionary": PlanLimits("visionary", "Visionary", 250, 5, 250, 5, 6, frozenset({"mcp"}), 99, 79),
    "titan": PlanLimits(
        "titan", "Titan", 1500, 20, 1000, 20, 1, frozenset({"mcp", "api", "guests"}), 499, 399
    ),
}


def limits_for(plan: str) -> PlanLimits:
    return PLANS.get(plan, PLANS["pro"])
