import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.db.base import Base
from app.models import *  # noqa: F403  (register models on Base.metadata)

target_metadata = Base.metadata


def run_offline() -> None:
    context.configure(url=get_settings().database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _run(connection) -> None:  # noqa: ANN001
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    s = get_settings()
    connect_args = {"statement_cache_size": 0} if s.db_use_pooler and "asyncpg" in s.database_url else {}
    engine = create_async_engine(s.database_url, connect_args=connect_args)
    async with engine.connect() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    asyncio.run(run_online())
