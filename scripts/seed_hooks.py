"""Seed a SAMPLE hook library for local development / staging demos.

    uv run python -m scripts.seed_hooks

Idempotent (skips hooks that exist). The lines are original templates written for Smart Content with
illustrative view counts — the content team replaces them with real curated hooks via /admin/hooks.
"""

import asyncio

from sqlalchemy import select

from app.db.session import get_sessionmaker
from app.models import HookTemplate

SAMPLES: list[tuple[str, str, str, int]] = [
    ("Stop posting every day — do this instead", "contrarian", "Creators", 2_400_000),
    ("Cardio isn't why you're not losing weight", "contrarian", "Fitness", 1_900_000),
    ("Your budget isn't the problem. Your bank app is", "contrarian", "Finance", 880_000),
    ("I deleted every productivity app. Here's what happened", "curiosity_gap", "Productivity", 1_300_000),
    ("The ingredient chefs never tell you about", "curiosity_gap", "Food", 3_100_000),
    ("Nobody explains this part of starting a business", "curiosity_gap", "Business", 640_000),
    ("Why do some videos get 10x more views than the rest?", "question", "Creators", 1_100_000),
    ("Would you pay $5 to save 5 hours a week?", "question", "Productivity", 410_000),
    ("What happens to your body after 30 days of walking?", "question", "Fitness", 2_700_000),
    ("This 9-second edit doubled my watch time", "unexpected_claim", "Creators", 960_000),
    ("I made dinner for four for under $6", "unexpected_claim", "Food", 1_500_000),
    ("My first client paid me in pizza", "unexpected_claim", "Business", 330_000),
    ("If your videos die at 200 views, watch this", "problem", "Creators", 2_000_000),
    ("You're stretching wrong (and it's why your back hurts)", "problem", "Fitness", 1_700_000),
    ("The mistake that keeps you broke at 25", "problem", "Finance", 1_200_000),
    ("Your landing page loses customers in 3 seconds", "problem", "Business", 520_000),
]


async def main() -> None:
    async with get_sessionmaker()() as db:
        existing = set((await db.scalars(select(HookTemplate.text))).all())
        new = [
            HookTemplate(text=t, category=c, niche=n, views=v) for t, c, n, v in SAMPLES if t not in existing
        ]
        db.add_all(new)
        await db.commit()
        print(f"Seeded {len(new)} sample hooks ({len(existing)} already present).")


if __name__ == "__main__":
    asyncio.run(main())
