from dataclasses import asdict

from app.models import Profile
from app.modules.plans_credits.plans import PlanLimits, limits_for
from app.modules.plans_credits.service import Balance
from app.schemas import CreditsOut, PlanOut


def plan_out(p: PlanLimits) -> PlanOut:
    d = asdict(p)
    d["features"] = sorted(p.features)
    return PlanOut(**d)


def credits_out(b: Balance) -> CreditsOut:
    return CreditsOut(**asdict(b))


def profile_plan(profile: Profile) -> PlanOut:
    return plan_out(limits_for(profile.plan))
