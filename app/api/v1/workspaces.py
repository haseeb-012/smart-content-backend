import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import current_profile
from app.db.session import get_db
from app.models import Profile
from app.modules.identity import service as identity
from app.schemas import WorkspaceCreate, WorkspaceOut, WorkspaceUpdate

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


def _out(ws, role: str) -> WorkspaceOut:  # noqa: ANN001
    return WorkspaceOut(id=ws.id, name=ws.name, is_active=ws.is_active, role=role, created_at=ws.created_at)  # type: ignore[arg-type]


@router.get("", response_model=list[WorkspaceOut])
async def list_workspaces(profile: Profile = Depends(current_profile), db: AsyncSession = Depends(get_db)):
    return [_out(ws, role) for ws, role in await identity.list_workspaces(db, profile)]


@router.post("", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    body: WorkspaceCreate, profile: Profile = Depends(current_profile), db: AsyncSession = Depends(get_db)
):
    ws = await identity.create_workspace(db, profile, body.name.strip())
    return _out(ws, "owner")


@router.get("/{workspace_id}", response_model=WorkspaceOut)
async def get_workspace(
    workspace_id: uuid.UUID, profile: Profile = Depends(current_profile), db: AsyncSession = Depends(get_db)
):
    ws, role = await identity.get_workspace_for(db, profile, workspace_id)
    return _out(ws, role)


@router.patch("/{workspace_id}", response_model=WorkspaceOut)
async def update_workspace(
    workspace_id: uuid.UUID,
    body: WorkspaceUpdate,
    profile: Profile = Depends(current_profile),
    db: AsyncSession = Depends(get_db),
):
    ws, role = await identity.update_workspace(
        db, profile, workspace_id, name=body.name.strip() if body.name else None, is_active=body.is_active
    )
    return _out(ws, role)
