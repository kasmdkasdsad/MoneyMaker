"""Runtime configuration."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MM_", env_file=".env", extra="ignore"
    )

    environment: str = "development"
    database_url: str = "postgresql+psycopg://moneymaker:moneymaker@localhost:5432/moneymaker"
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_statement_timeout_ms: int = 5_000

    # --- Campaign policy ---------------------------------------------------
    # Card authorizations decay after roughly 7 days. We capture at lock, which
    # happens at window close, so the window itself must finish comfortably
    # inside that. Five days leaves two days of margin for capture retries and
    # PSP incidents. Raising this without moving to a deferred-payment model
    # will produce silent capture failures on real money.
    max_campaign_window_days: int = 5
    reservation_ttl_seconds: int = 180
    rescue_ratio_bps: int = 8_000
    rescue_extension_hours: int = 24
    allow_rescue: bool = True

    # --- Last-mile defaults (per neighbourhood overrides live in the DB) ----
    default_stop_cost_cents: int = 1_200
    default_line_haul_cost_cents: int = 6_000
    default_expected_units_per_run: int = 300
    default_commission_bps: int = 500
    psp_fee_bps: int = 290
    psp_fixed_cents: int = 30

    # --- Matching ----------------------------------------------------------
    pickup_search_radius_m: float = 2_000.0
    consolidation_bonus_m: float = 400.0
    merge_radius_m: float = 3_000.0
    max_merged_stops: int = 3
    vehicle_capacity_units: int = 600
    max_stops_per_run: int = 25

    payments_provider: str = Field(
        default="fake", description="'fake' in dev/test, 'stripe' in production"
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
