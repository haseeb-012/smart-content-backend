"""Internal admin endpoints (FR-BILL-09/10, FR-MCP-01). Every change is audited and takes
effect on the user's next request — no redeploy."""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1._common import credits_out
from app.core.deps import admin_profile
from app.core.errors import NotFound
from app.db.session import get_db
from app.models import AuditLog, Profile, WaitlistEntry
from app.modules.plans_credits import service as credits
from app.schemas import (
    AdminCreditsIn,
    AdminFlagsIn,
    AdminPlanIn,
    AdminUserOut,
    AuditOut,
    MeOut,
    WaitlistEntryOut,
)

router = APIRouter(prefix="/admin", tags=["admin"])


async def _target(db: AsyncSession, user_id: uuid.UUID) -> Profile:
    p = await db.get(Profile, user_id)
    if not p:
        raise NotFound("User not found.")
    return p


async def _user_out(db: AsyncSession, p: Profile) -> AdminUserOut:
    return AdminUserOut(
        profile=MeOut.model_validate(p), credits=credits_out(await credits.get_balance(db, p))
    )


@router.get("/users/{user_id}", response_model=AdminUserOut)
async def get_user(
    user_id: uuid.UUID, _: Profile = Depends(admin_profile), db: AsyncSession = Depends(get_db)
):
    return await _user_out(db, await _target(db, user_id))


@router.patch("/users/{user_id}/plan", response_model=AdminUserOut)
async def set_plan(
    user_id: uuid.UUID,
    body: AdminPlanIn,
    actor: Profile = Depends(admin_profile),
    db: AsyncSession = Depends(get_db),
):
    target = await _target(db, user_id)
    old = target.plan
    target.plan = body.plan
    db.add(
        AuditLog(
            actor_id=actor.id,
            target_user_id=target.id,
            action="plan.set",
            details={"from": old, "to": body.plan},
        )
    )
    await db.commit()
    return await _user_out(db, target)


@router.post("/users/{user_id}/credits", response_model=AdminUserOut)
async def adjust_credits(
    user_id: uuid.UUID,
    body: AdminCreditsIn,
    actor: Profile = Depends(admin_profile),
    db: AsyncSession = Depends(get_db),
):
    target = await _target(db, user_id)
    await credits.adjust(db, actor=actor, target=target, amount=body.amount, reason=body.reason)
    return await _user_out(db, target)


@router.patch("/users/{user_id}/flags", response_model=AdminUserOut)
async def set_flags(
    user_id: uuid.UUID,
    body: AdminFlagsIn,
    actor: Profile = Depends(admin_profile),
    db: AsyncSession = Depends(get_db),
):
    target = await _target(db, user_id)
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(target, k, v)
    if changes:
        db.add(AuditLog(actor_id=actor.id, target_user_id=target.id, action="flags.set", details=changes))
    await db.commit()
    return await _user_out(db, target)


@router.get("/audit", response_model=list[AuditOut])
async def audit_log(
    limit: int = Query(100, ge=1, le=500),
    _: Profile = Depends(admin_profile),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit))
    return [AuditOut.model_validate(r) for r in rows]


@router.get("/waitlist", response_model=list[WaitlistEntryOut])
async def waitlist(_: Profile = Depends(admin_profile), db: AsyncSession = Depends(get_db)):
    rows = await db.scalars(select(WaitlistEntry).order_by(WaitlistEntry.created_at.desc()))
    return [WaitlistEntryOut.model_validate(r) for r in rows]
