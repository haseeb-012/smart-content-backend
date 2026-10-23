"""Outlier score (PRD §9.5, MVP_TECH): how far a video beat its own channel's normal.

    outlier = views ÷ median(views of the channel's previous 30 videos)

Requires at least 10 earlier videos; otherwise the score is None (not enough history to judge).
"""

from collections.abc import Sequence
from datetime import datetime
from statistics import median
from typing import Protocol

WINDOW = 30
MIN_HISTORY = 10


class _V(Protocol):
    posted_at: datetime | None
    views: int


def compute_outliers(videos: Sequence[_V]) -> list[float | None]:
    """Scores aligned with `videos` (any order in, same order out)."""
    order = sorted(
        range(len(videos)), key=lambda i: (videos[i].posted_at is None, videos[i].posted_at or datetime.min)
    )
    scores: list[float | None] = [None] * len(videos)
    history: list[int] = []
    for idx in order:
        v = videos[idx]
        if v.posted_at is None:
            continue
        prev = history[-WINDOW:]
        if len(prev) >= MIN_HISTORY:
            m = median(prev)
            scores[idx] = round(v.views / m, 2) if m > 0 else None
        history.append(v.views)
    return scores


def engagement_rate(views: int, likes: int, comments: int, shares: int) -> float:
    """(likes + comments + shares) ÷ views, as a percent with one decimal."""
    return round((likes + comments + shares) / views * 100, 1) if views > 0 else 0.0
