"""App settings, read from environment variables / .env. Secrets never have defaults."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: Literal["local", "test", "staging", "production"] = "local"
    version: str = "0.1.0"

    # Database (Supabase Postgres in staging/prod). asyncpg driver.
    database_url: str = "sqlite+aiosqlite:///./local.db"
    # Supabase's transaction pooler (port 6543) needs asyncpg's statement cache off.
    db_use_pooler: bool = False

    # Supabase Auth — backend verifies access tokens against the project's JWKS.
    supabase_url: str = ""
    supabase_jwt_audience: str = "authenticated"

    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://localhost:3001"]
    )

    # Providers (all behind interfaces)
    billing_provider: Literal["mock", "stripe_test"] = "mock"
    # Prefer a restricted test key (rk_test_…) with only Customers, Checkout Sessions,
    # Customer Portal and Subscriptions (read) permissions.
    stripe_secret_key: str = ""
    stripe_publishable_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_api_version: str = "2026-04-22.dahlia"
    # Stripe Price IDs (test mode) for each plan × cycle.
    stripe_price_pro_monthly: str = ""
    stripe_price_pro_yearly: str = ""
    stripe_price_visionary_monthly: str = ""
    stripe_price_visionary_yearly: str = ""
    stripe_price_titan_monthly: str = ""
    stripe_price_titan_yearly: str = ""
    # Where Stripe sends the user back to.
    dashboard_url: str = "http://localhost:3001"
    # Public URL of this API — used in MCP OAuth metadata (the MCP endpoint is {public_api_url}/mcp).
    public_api_url: str = "http://localhost:8000"

    # Channel/video data (behind ChannelProvider). "fake" = deterministic demo data, no network.
    channel_provider: Literal["fake", "apify"] = "fake"
    apify_token: str = ""
    # Apify actor IDs per platform — configurable because public actors change over time.
    apify_actor_tiktok: str = "clockworks~tiktok-scraper"
    apify_actor_instagram: str = "apify~instagram-scraper"
    apify_actor_youtube: str = "streamers~youtube-shorts-scraper"
    apify_timeout_seconds: int = 180
    # How many recent videos to pull per channel on each sync (outlier baseline needs ≥ 31).
    ingest_videos_per_channel: int = 60

    # AI (behind LLMProvider, decision D11). "fake" = deterministic demo analysis, no network.
    llm_provider: Literal["fake", "gemini"] = "fake"
    gemini_api_key: str = ""
    # Verified on ai.google.dev/gemini-api/docs/models (2026-10-23). Configurable because models rotate.
    gemini_model: str = "gemini-3.8-flash"
    gemini_timeout_seconds: int = 240
    # Inline video upload cap (Gemini: inline requests should stay under ~20 MB).
    gemini_max_video_mb: int = 18
    # Per-user cost cap (PRD §18 open item → safe default): Deep Analyses started per user per day.
    analysis_daily_limit: int = 50

    # Credits
    default_plan: Literal["pro", "visionary", "titan"] = "pro"

    @field_validator("stripe_secret_key", "stripe_publishable_key")
    @classmethod
    def refuse_live_keys(cls, v: str) -> str:
        # Owner override: Stripe is test mode only. Never accept live keys.
        if v.startswith(("sk_live_", "pk_live_", "rk_live_")):
            raise ValueError("Live Stripe keys are not allowed — use test-mode keys (sk_test_/pk_test_).")
        return v

    @model_validator(mode="after")
    def require_auth_config(self) -> "Settings":
        if self.env in ("staging", "production") and not self.supabase_url:
            raise ValueError("SUPABASE_URL is required outside local/test.")
        if self.channel_provider == "apify" and not self.apify_token:
            raise ValueError("CHANNEL_PROVIDER=apify needs APIFY_TOKEN.")
        if self.llm_provider == "gemini" and not self.gemini_api_key:
            raise ValueError("LLM_PROVIDER=gemini needs GEMINI_API_KEY.")
        if self.billing_provider == "stripe_test":
            if not self.stripe_secret_key.startswith(("sk_test_", "rk_test_")):
                raise ValueError("BILLING_PROVIDER=stripe_test needs a test key (rk_test_… or sk_test_…).")
            if not self.stripe_webhook_secret.startswith("whsec_"):
                raise ValueError("BILLING_PROVIDER=stripe_test needs STRIPE_WEBHOOK_SECRET (whsec_…).")
        return self

    @property
    def stripe_prices(self) -> dict[tuple[str, str], str]:
        """(plan, cycle) → price id, only for configured prices."""
        raw = {
            ("pro", "monthly"): self.stripe_price_pro_monthly,
            ("pro", "yearly"): self.stripe_price_pro_yearly,
            ("visionary", "monthly"): self.stripe_price_visionary_monthly,
            ("visionary", "yearly"): self.stripe_price_visionary_yearly,
            ("titan", "monthly"): self.stripe_price_titan_monthly,
            ("titan", "yearly"): self.stripe_price_titan_yearly,
        }
        return {k: v for k, v in raw.items() if v}

    @property
    def jwks_url(self) -> str:
        return f"{self.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"

    @property
    def jwt_issuer(self) -> str:
        return f"{self.supabase_url.rstrip('/')}/auth/v1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
