"""Collections — public, free, no credits (FR-COL-04). Admin curation lives under /admin/collections."""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import AdminActor, admin_actor
from app.core.ratelimit import RateLimiter, client_key
from app.db.session import get_db
from app.models import Collection
from app.modules.content_studio import collections as svc
from app.schemas import (
    CollectionCategory,
    CollectionCreate,
    CollectionDetail,
    CollectionSummary,
    CollectionUpdate,
    CollectionVideoIn,
    CollectionVideoOut,
)

router = APIRouter(prefix="/collections", tags=["collections"])
admin_router = APIRouter(prefix="/admin/collections", tags=["admin", "collections"])
export_limiter = RateLimiter(limit=30, window_seconds=60)


def _summary(c: Collection, count: int) -> CollectionSummary:
    return CollectionSummary(
        id=c.id,
        slug=c.slug,
        name=c.name,
        category=c.category,  # type: ignore[arg-type]
        sub=c.sub,
        description=c.description,
        cover_hue=c.cover_hue,
        video_count=count,
        is_published=c.is_published,
        updated_at=c.updated_at,
    )


def _detail(c: Collection) -> CollectionDetail:
    return CollectionDetail(
        **_summary(c, len(c.videos)).model_dump(),
        breakdown=list(c.breakdown or []),
        videos=[CollectionVideoOut.model_validate(v) for v in c.videos],
    )


# ---------------------------------------------------------------- public


@router.get("", response_model=list[CollectionSummary])
async def list_collections(
    category: CollectionCategory | None = None,
    q: str | None = Query(default=None, max_length=100),
    sort: svc.Sort = "most_videos",
    db: AsyncSession = Depends(get_db),
):
    return [_summary(c, n) for c, n in await svc.list_collections(db, category=category, q=q, sort=sort)]


@router.get("/{slug}", response_model=CollectionDetail)
async def get_collection(slug: str, db: AsyncSession = Depends(get_db)):
    return _detail(await svc.get_collection(db, slug))


@router.get("/{slug}/export")
async def export_collection(
    slug: str,
    request: Request,
    format: Literal["markdown", "json"] = "markdown",
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Reference sheet download (FR-COL-05). Free; lightly rate-limited per IP."""
    export_limiter.check(client_key(request))
    c = await svc.get_collection(db, slug)
    body = svc.export_collection(c, format)
    ext, media = ("json", "application/json") if format == "json" else ("md", "text/markdown; charset=utf-8")
    return Response(
        body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{c.slug}-reference.{ext}"'},
    )


# ---------------------------------------------------------------- admin curation


@admin_router.get("", response_model=list[CollectionSummary])
async def admin_list(_: AdminActor = Depends(admin_actor), db: AsyncSession = Depends(get_db)):
    return [
        _summary(c, n) for c, n in await svc.list_collections(db, sort="recent", include_unpublished=True)
    ]


@admin_router.get("/{slug}", response_model=CollectionDetail)
async def admin_get(slug: str, _: AdminActor = Depends(admin_actor), db: AsyncSession = Depends(get_db)):
    return _detail(await svc.get_collection(db, slug, include_unpublished=True))


@admin_router.post("", response_model=CollectionDetail, status_code=status.HTTP_201_CREATED)
async def admin_create(
    body: CollectionCreate, actor: AdminActor = Depends(admin_actor), db: AsyncSession = Depends(get_db)
):
    return _detail(await svc.create_collection(db, actor, body.model_dump()))


@admin_router.patch("/{collection_id}", response_model=CollectionDetail)
async def admin_update(
    collection_id: uuid.UUID,
    body: CollectionUpdate,
    actor: AdminActor = Depends(admin_actor),
    db: AsyncSession = Depends(get_db),
):
    return _detail(await svc.update_collection(db, actor, collection_id, body.model_dump(exclude_none=True)))


@admin_router.delete("/{collection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete(
    collection_id: uuid.UUID, actor: AdminActor = Depends(admin_actor), db: AsyncSession = Depends(get_db)
) -> Response:
    await svc.delete_collection(db, actor, collection_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@admin_router.post(
    "/{collection_id}/videos", response_model=CollectionDetail, status_code=status.HTTP_201_CREATED
)
async def admin_add_video(
    collection_id: uuid.UUID,
    body: CollectionVideoIn,
    actor: AdminActor = Depends(admin_actor),
    db: AsyncSession = Depends(get_db),
):
    return _detail(await svc.add_video(db, actor, collection_id, body.model_dump()))


@admin_router.delete("/{collection_id}/videos/{video_id}", response_model=CollectionDetail)
async def admin_remove_video(
    collection_id: uuid.UUID,
    video_id: uuid.UUID,
    actor: AdminActor = Depends(admin_actor),
    db: AsyncSession = Depends(get_db),
):
    return _detail(await svc.remove_video(db, actor, collection_id, video_id))
