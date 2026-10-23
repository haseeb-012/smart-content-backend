"""Small in-process sliding-window rate limiter for public endpoints.

Good enough for a single instance; swap for Redis-backed limits when workers/instances scale out."""

import time
from collections import defaultdict, deque

from fastapi import Request

from app.core.errors import RateLimited


class RateLimiter:
    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window = window_seconds
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            raise RateLimited(
                "Too many requests. Please try again shortly.", details={"retry_after": self.window}
            )
        q.append(now)

    def reset(self) -> None:
        self.hits.clear()


def client_key(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")
