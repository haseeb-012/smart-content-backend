"""The Alembic migration must build the same schema as the models (no drift), and be reversible."""

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from app.core.config import get_settings
from app.db.base import Base


def test_migration_matches_models_and_downgrades(tmp_path, monkeypatch):
    db_file = tmp_path / "m.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file.as_posix()}")
    get_settings.cache_clear()
    try:
        cfg = Config("alembic.ini")
        command.upgrade(cfg, "head")

        engine = create_engine(f"sqlite:///{db_file.as_posix()}")
        with engine.connect() as conn:
            assert set(inspect(conn).get_table_names()) >= {
                "profiles",
                "workspaces",
                "memberships",
                "credit_ledger",
                "audit_log",
                "waitlist",
            }
            diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        assert diff == [], f"models and migration differ: {diff}"

        command.downgrade(cfg, "base")
        with engine.connect() as conn:
            assert set(inspect(conn).get_table_names()) <= {"alembic_version"}
        engine.dispose()
    finally:
        get_settings.cache_clear()
