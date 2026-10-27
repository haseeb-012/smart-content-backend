"""Copy thumbnails for videos imported before thumbnail storage existed.

    uv run python scripts/backfill_thumbnails.py

Only works while the platform's link is still valid; expired ones need a channel re-import
(Admin → Channels → Re-import), which copies them automatically. Free: downloads images only."""

import asyncio
import sys

sys.path.insert(0, ".")
from sqlalchemy import func, select  # noqa: E402

from app.db.session import get_sessionmaker  # noqa: E402
from app.models import Video  # noqa: E402
from app.modules.discovery_analysis import thumbnails  # noqa: E402


async def main() -> None:
    async with get_sessionmaker()() as db:
        stored = await thumbnails.store_missing(db)
        missing = await db.scalar(
            select(func.count())
            .select_from(Video)
            .where(Video.thumb_stored_at.is_(None), Video.thumbnail_url.is_not(None))
        )
        rows = (await db.execute(select(Video.thumbnail_url).where(Video.thumb_stored_at.is_(None)))).all()
        expired = sum(1 for (u,) in rows if thumbnails.needs_copy(u))
    print(
        f"stored now: {stored} · still on original links: {missing} (of which expiring-host links not copied: {expired})"
    )


asyncio.run(main())
