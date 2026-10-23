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

    llm_provider: Literal["gemini", "mock"] = "gemini"
    gemini_api_key: str = ""

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
