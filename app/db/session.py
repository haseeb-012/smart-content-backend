from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    s = get_settings()
    connect_args: dict = {}
    if s.database_url.startswith("postgresql+asyncpg") and s.db_use_pooler:
        connect_args["statement_cache_size"] = 0  # required behind Supabase's transaction pooler
    return create_async_engine(s.database_url, pool_pre_ping=True, connect_args=connect_args)


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session
