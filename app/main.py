import logging

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import router as v1
from app.core.config import get_settings
from app.core.errors import install_error_handlers
from app.db.session import get_db
from app.schemas import Health

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("smart_content")


def create_app() -> FastAPI:
    settings = get_settings()  # validates config (e.g. refuses live Stripe keys) at startup
    app = FastAPI(title="Smart Content API", version=settings.version, docs_url="/docs", redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,  # auth is a bearer header, not cookies
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )
    install_error_handlers(app)
    app.include_router(v1)

    @app.get("/health", response_model=Health, tags=["health"])
    async def health(db: AsyncSession = Depends(get_db)) -> Health:
        try:
            await db.execute(text("SELECT 1"))
            db_ok = True
        except Exception:  # noqa: BLE001
            log.exception("health: database check failed")
            db_ok = False
        return Health(
            status="ok" if db_ok else "degraded",
            version=settings.version,
            database="ok" if db_ok else "error",
        )

    return app


app = create_app()
