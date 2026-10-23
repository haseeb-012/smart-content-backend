"""Users, workspaces and memberships. All workspace access goes through `get_workspace_for`,
which enforces tenant isolation: a user only ever sees workspaces they are a member of."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import Forbidden, NotFound, PlanLimitError
from app.core.security import AuthUser
from app.models import AuditLog, Membership, Profile, Workspace
from app.modules.plans_credits.plans import limits_for

WRITE_ROLES = {"owner", "editor"}


async def get_or_create_profile(db: AsyncSession, user: AuthUser) -> Profile:
    """Profiles are normally created by the DB trigger on auth.users; this is the safety net."""
    profile = await db.get(Profile, user.id)
    if profile:
        if user.email and profile.email != user.email:
            profile.email = user.email
            await db.commit()
        return profile
    profile = Profile(id=user.id, email=user.email, plan=get_settings().default_plan)
    db.add(profile)
    try:
        await db.commit()
    except IntegrityError:  # created concurrently
        await db.rollback()
        profile = await db.get(Profile, user.id)
        assert profile is not None  # noqa: S101
    return profile


async def list_workspaces(db: AsyncSession, profile: Profile) -> list[tuple[Workspace, str]]:
    rows = await db.execute(
        select(Workspace, Membership.role)
        .join(Membership, Membership.workspace_id == Workspace.id)
        .where(Membership.user_id == profile.id)
        .order_by(Workspace.created_at)
    )
    return [(w, role) for w, role in rows.all()]


async def get_workspace_for(
    db: AsyncSession, profile: Profile, workspace_id: uuid.UUID, *, write: bool = False
) -> tuple[Workspace, str]:
    row = (
        await db.execute(
            select(Workspace, Membership.role)
            .join(Membership, Membership.workspace_id == Workspace.id)
            .where(Workspace.id == workspace_id, Membership.user_id == profile.id)
        )
    ).first()
    if not row:
        # Same response whether it doesn't exist or belongs to someone else (no existence leak).
        raise NotFound("Workspace not found.")
    ws, role = row
    if write and role not in WRITE_ROLES:
        raise Forbidden("Guests can view but not change this workspace.")
    return ws, role


async def count_active_owned(db: AsyncSession, profile: Profile) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(Workspace)
            .where(Workspace.owner_id == profile.id, Workspace.is_active)
        )
        or 0
    )


async def create_workspace(db: AsyncSession, profile: Profile, name: str) -> Workspace:
    limits = limits_for(profile.plan)
    if await count_active_owned(db, profile) >= limits.workspaces:
        raise PlanLimitError(
            f"Your {limits.name} plan allows {limits.workspaces} active workspaces.",
            details={"limit": limits.workspaces, "plan": profile.plan, "resource": "workspaces"},
        )
    ws = Workspace(name=name, owner_id=profile.id)
    ws.memberships.append(Membership(user_id=profile.id, role="owner"))
    db.add(ws)
    await db.commit()
    await db.refresh(ws)
    return ws


async def update_workspace(
    db: AsyncSession, profile: Profile, workspace_id: uuid.UUID, *, name: str | None, is_active: bool | None
) -> tuple[Workspace, str]:
    ws, role = await get_workspace_for(db, profile, workspace_id, write=True)
    if is_active is not None and role != "owner":
        raise Forbidden("Only the owner can activate or deactivate a workspace.")
    if is_active is True and not ws.is_active:
        limits = limits_for(profile.plan)
        if await count_active_owned(db, profile) >= limits.workspaces:
            raise PlanLimitError(
                f"Your {limits.name} plan allows {limits.workspaces} active workspaces.",
                details={"limit": limits.workspaces, "plan": profile.plan, "resource": "workspaces"},
            )
    if name is not None:
        ws.name = name
    if is_active is not None and is_active != ws.is_active:
        ws.is_active = is_active
        db.add(
            AuditLog(
                actor_id=profile.id,
                action="workspace.active" if is_active else "workspace.deactivate",
                details={"workspace_id": str(ws.id)},
            )
        )
    await db.commit()
    return ws, role
