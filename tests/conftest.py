"""Test harness: in-memory SQLite + a locally generated EC signing key standing in for Supabase JWKS."""

import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

# Tests never touch real services, whatever the local .env says (env vars beat .env in pydantic-settings).
os.environ.update(
    {
        "ENV": "test",
        "DATABASE_URL": "sqlite+aiosqlite://",
        "CHANNEL_PROVIDER": "fake",
        "LLM_PROVIDER": "fake",
        "BILLING_PROVIDER": "mock",
        "SUPABASE_URL": "https://test.supabase.co",
        "STRIPE_SECRET_KEY": "",
        "STRIPE_WEBHOOK_SECRET": "",
        "APIFY_TOKEN": "",
        "GEMINI_API_KEY": "",
        **{f"STRIPE_PRICE_{p}_{c}": "" for p in ("PRO", "VISIONARY", "TITAN") for c in ("MONTHLY", "YEARLY")},
    }
)

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.v1.waitlist import limiter
from app.core.deps import get_verifier
from app.core.security import TokenVerifier
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app

ISSUER = "https://test.supabase.co/auth/v1"
PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())


class StaticResolver:
    def key_for(self, token: str) -> Any:
        return PRIVATE_KEY.public_key()


def make_token(
    sub: uuid.UUID, *, email: str | None = None, aud: str = "authenticated", exp_in: int = 3600, key=None
) -> str:
    now = int(time.time())
    claims = {
        "sub": str(sub),
        "aud": aud,
        "iss": ISSUER,
        "iat": now,
        "exp": now + exp_in,
        "role": "authenticated",
    }
    if email:
        claims["email"] = email
    return jwt.encode(claims, key or PRIVATE_KEY, algorithm="ES256", headers={"kid": "test"})


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def db(sessionmaker) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as s:
        yield s


@pytest.fixture
def app(sessionmaker) -> FastAPI:
    app = create_app()

    async def _db() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as s:
            yield s

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_verifier] = lambda: TokenVerifier(
        StaticResolver(), audience="authenticated", issuer=ISSUER
    )
    limiter.reset()
    return app


@pytest.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


class User:
    def __init__(self, email: str):
        self.id = uuid.uuid4()
        self.email = email

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {make_token(self.id, email=self.email)}"}


@pytest.fixture
def alice() -> User:
    return User("alice@example.com")


@pytest.fixture
def bob() -> User:
    return User("bob@example.com")
