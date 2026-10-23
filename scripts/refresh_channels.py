"""Daily refresh of tracked channels (PRD: "analyzed daily and continuously refreshed").

    uv run python -m scripts.refresh_channels            # channels older than 24h
    uv run python -m scripts.refresh_channels --hours 6

Run it from a scheduler (Render/Railway cron, GitHub Actions, or Celery beat once workers exist).
Sequential on purpose: keeps Apify spend predictable.
"""

import argparse
import asyncio
from datetime import timedelta

from app.db.session import get_sessionmaker
from app.modules.discovery_analysis.channels import channels_due_for_refresh, sync_channel
from app.modules.discovery_analysis.providers import get_channel_provider


async def main(hours: int) -> None:
    factory = get_sessionmaker()
    provider = get_channel_provider()
    async with factory() as db:
        due = await channels_due_for_refresh(db, timedelta(hours=hours))
    print(f"{len(due)} channel(s) due for refresh (provider: {provider.name})")
    for i, cid in enumerate(due, 1):
        await sync_channel(factory, provider, cid)
        print(f"  [{i}/{len(due)}] {cid} done")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    asyncio.run(main(ap.parse_args().hours))
