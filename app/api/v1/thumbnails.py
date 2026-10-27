"""Public thumbnail images (our stored copies). No login: they're the same public images the
platforms show, and video ids are random UUIDs. Cached by browsers/CDNs for a year (never change)."""

import uuid

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.db.session import get_db
from app.models import VideoThumbnail

router = APIRouter(tags=["videos"])


@router.get(
    "/thumbnails/{video_id}", response_class=Response, responses={200: {"content": {"image/webp": {}}}}
)
async def thumbnail(video_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> Response:
    t = await db.get(VideoThumbnail, video_id)
    if t is None:
        raise NotFound("No stored thumbnail.")
    return Response(
        content=t.data,
        media_type=t.mime,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "Cross-Origin-Resource-Policy": "cross-origin",
            "X-Content-Type-Options": "nosniff",
        },
    )
