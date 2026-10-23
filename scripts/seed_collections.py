"""Seed SAMPLE collections for local development / staging demos.

    uv run python -m scripts.seed_collections

Idempotent (skips slugs that exist). The example videos are fictional placeholders, not real creators —
the content team replaces them with real curated outliers via the /admin/collections API.
"""

import asyncio

from sqlalchemy import select

from app.db.session import get_sessionmaker
from app.models import Collection, CollectionVideo

SAMPLES = [
    ("breakdown-explainer", "Breakdown / Explainer", "formats", "Explainers", "from-[#1a5cff] to-[#0a2a8f]",
     "Takes one complex topic and explains it in three quick beats."),
    ("top-3-countdown", "Top 3 Countdown", "formats", "Listicle", "from-[#22c55e] to-[#14532d]",
     "Counts down to the best item to hold attention to the end."),
    ("before-after", "Before / After", "formats", "Story", "from-[#eab308] to-[#713f12]",
     "Shows a transformation, then explains how it happened."),
    ("case-study", "Case Study", "signature_series", "Case study", "from-[#f97316] to-[#7c2d12]",
     "A recurring series breaking down one company or creator."),
    ("fast-push-in", "Fast Push-In", "visual_hooks", "Camera", "from-[#3b82f6] to-[#1e3a8a]",
     "A sudden zoom on the subject in the first frame."),
    ("unexpected-first-frame", "Unexpected First Frame", "visual_hooks", "Opening scene",
     "from-[#14b8a6] to-[#0f4f4a]", "Opens on an image that doesn't seem to belong."),
    ("side-by-side", "Side-by-Side Compare", "editing_styles", "Studio", "from-[#a855f7] to-[#4c1d95]",
     "Split screen comparing two options in real time."),
    ("kinetic-captions", "Kinetic Captions", "editing_styles", "Text", "from-[#ec4899] to-[#831843]",
     "Animated, word-by-word captions that pace the story."),
]  # fmt: skip

BREAKDOWN = [
    "Hook lands in the first 2 seconds with a clear promise.",
    "The pattern repeats every 3–5 seconds to reset attention.",
    "Payoff arrives before the 70% mark; CTA is one short line.",
]
PLATFORMS = [("tiktok", "https://www.tiktok.com/@sample.creator{n}/video/74000000000000{n:05d}"),
             ("instagram", "https://www.instagram.com/reel/SAMPLE{n:05d}/"),
             ("youtube", "https://www.youtube.com/shorts/SAMPLE{n:05d}")]  # fmt: skip


async def main() -> None:
    async with get_sessionmaker()() as db:
        existing = set((await db.scalars(select(Collection.slug))).all())
        n = 0
        for i, (slug, name, category, sub, hue, desc) in enumerate(SAMPLES):
            if slug in existing:
                continue
            c = Collection(
                slug=slug,
                name=name,
                category=category,
                sub=sub,
                description=desc,
                breakdown=BREAKDOWN,
                cover_hue=hue,
                is_published=True,
            )
            for j in range(4 + i % 4):
                n += 1
                platform, url = PLATFORMS[(i + j) % 3]
                c.videos.append(
                    CollectionVideo(
                        position=j,
                        platform=platform,
                        url=url.format(n=n),
                        title=f"SAMPLE — {name} example {j + 1}",
                        creator_handle=f"sample.creator{n}",
                        views=150_000 * (j + 2) * (i + 1),
                        outlier_score=round(4.5 + (i * 7 + j * 3) % 60, 1),
                        notes="Sample entry — replace with a real curated outlier.",
                    )
                )
            db.add(c)
        await db.commit()
    print(f"Seeded sample collections (skipped {len(existing)} existing).")


if __name__ == "__main__":
    asyncio.run(main())
