"""Collections (PRD §9.10): team-curated, free on every plan, no credits (FR-COL-04).
Public reads only see published collections; all writes are admin-only and audited."""

import json
import uuid
from typing import Literal

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import Conflict, NotFound
from app.models import AuditLog, Collection, CollectionVideo, Profile

CATEGORY_LABELS = {
    "formats": "Formats",
    "editing_styles": "Editing Styles",
    "visual_hooks": "Visual Hooks",
    "signature_series": "Signature Series",
}
Sort = Literal["most_videos", "name", "recent"]


def _video_count():  # noqa: ANN202
    return (
        select(func.count(CollectionVideo.id))
        .where(CollectionVideo.collection_id == Collection.id)
        .correlate(Collection)
        .scalar_subquery()
    )


async def list_collections(
    db: AsyncSession,
    *,
    category: str | None = None,
    q: str | None = None,
    sort: Sort = "most_videos",
    include_unpublished: bool = False,
) -> list[tuple[Collection, int]]:
    count = _video_count().label("video_count")
    stmt = select(Collection, count)
    if not include_unpublished:
        stmt = stmt.where(Collection.is_published)
    if category:
        stmt = stmt.where(Collection.category == category)
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Collection.name).like(like),
                func.lower(Collection.sub).like(like),
                func.lower(Collection.description).like(like),
            )
        )
    order = {
        "most_videos": (count.desc(), Collection.name),
        "name": (Collection.name,),
        "recent": (Collection.updated_at.desc(),),
    }[sort]
    rows = await db.execute(stmt.order_by(*order))
    return [(c, int(n or 0)) for c, n in rows.all()]


async def get_collection(db: AsyncSession, slug: str, *, include_unpublished: bool = False) -> Collection:
    stmt = (
        select(Collection)
        .options(selectinload(Collection.videos))
        .where(Collection.slug == slug)
        # Always reload rows + videos (after admin writes the identity map may hold stale lists).
        .execution_options(populate_existing=True)
    )
    c = await db.scalar(stmt)
    if c is None or (not c.is_published and not include_unpublished):
        raise NotFound("Collection not found.")
    return c


def export_collection(c: Collection, fmt: Literal["markdown", "json"]) -> str:
    """Shareable reference sheet (FR-COL-05)."""
    if fmt == "json":
        return json.dumps(
            {
                "name": c.name,
                "category": CATEGORY_LABELS.get(c.category, c.category),
                "sub": c.sub,
                "description": c.description,
                "breakdown": c.breakdown,
                "examples": [
                    {
                        "title": v.title,
                        "creator": v.creator_handle,
                        "platform": v.platform,
                        "url": v.url,
                        "views": v.views,
                        "outlier_score": v.outlier_score,
                        "notes": v.notes,
                    }
                    for v in c.videos
                ],
            },
            indent=2,
        )
    lines = [
        f"# {c.name}",
        f"_{CATEGORY_LABELS.get(c.category, c.category)} · {c.sub}_",
        "",
        c.description,
        "",
    ]
    if c.breakdown:
        lines += ["## Pattern breakdown", *[f"{i}. {b}" for i, b in enumerate(c.breakdown, 1)], ""]
    lines.append("## Examples")
    for v in c.videos:
        stats = " · ".join(
            s
            for s in [
                f"@{v.creator_handle}" if v.creator_handle else "",
                v.platform,
                f"{v.views:,} views" if v.views else "",
                f"{v.outlier_score:g}x outlier" if v.outlier_score else "",
            ]
            if s
        )
        lines.append(f"- [{v.title}]({v.url}) — {stats}")
        if v.notes:
            lines.append(f"  - {v.notes}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- admin (curation)


def _audit(db: AsyncSession, actor: Profile, action: str, c: Collection, **extra: object) -> None:
    db.add(
        AuditLog(
            actor_id=actor.id,
            action=action,
            details={"collection_id": str(c.id), "slug": c.slug, **{k: str(v) for k, v in extra.items()}},
        )
    )


async def create_collection(db: AsyncSession, actor: Profile, data: dict) -> Collection:
    c = Collection(**data)
    db.add(c)
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise Conflict(f"A collection with slug '{data['slug']}' already exists.", code="slug_taken") from e
    _audit(db, actor, "collection.create", c)
    await db.commit()
    return await get_collection(db, c.slug, include_unpublished=True)


async def _by_id(db: AsyncSession, collection_id: uuid.UUID) -> Collection:
    c = await db.scalar(
        select(Collection).options(selectinload(Collection.videos)).where(Collection.id == collection_id)
    )
    if c is None:
        raise NotFound("Collection not found.")
    return c


async def update_collection(
    db: AsyncSession, actor: Profile, collection_id: uuid.UUID, data: dict
) -> Collection:
    c = await _by_id(db, collection_id)
    for k, v in data.items():
        setattr(c, k, v)
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise Conflict("That slug is already used by another collection.", code="slug_taken") from e
    _audit(db, actor, "collection.update", c, fields=",".join(sorted(data)))
    await db.commit()
    return await get_collection(db, c.slug, include_unpublished=True)


async def delete_collection(db: AsyncSession, actor: Profile, collection_id: uuid.UUID) -> None:
    c = await _by_id(db, collection_id)
    _audit(db, actor, "collection.delete", c)
    await db.delete(c)
    await db.commit()


async def add_video(db: AsyncSession, actor: Profile, collection_id: uuid.UUID, data: dict) -> Collection:
    c = await _by_id(db, collection_id)
    if data.get("position") is None:
        data["position"] = max((v.position for v in c.videos), default=-1) + 1
    v = CollectionVideo(collection_id=c.id, **data)
    db.add(v)
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise Conflict("That video is already in this collection.", code="duplicate_video") from e
    _audit(db, actor, "collection.video_add", c, url=data["url"])
    slug = c.slug
    await db.commit()
    return await get_collection(db, slug, include_unpublished=True)


async def remove_video(
    db: AsyncSession, actor: Profile, collection_id: uuid.UUID, video_id: uuid.UUID
) -> Collection:
    c = await _by_id(db, collection_id)
    v = next((x for x in c.videos if x.id == video_id), None)
    if v is None:
        raise NotFound("Video not found in this collection.")
    _audit(db, actor, "collection.video_remove", c, url=v.url)
    slug = c.slug
    await db.delete(v)
    await db.commit()
    return await get_collection(db, slug, include_unpublished=True)
