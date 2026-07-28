"""Runtime configuration.

All secrets come from the environment or a secrets file; nothing is committed.
See ``.env.example`` for the full list.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]

AuthMode = Literal["cloudflare_access", "disabled"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="USSTOCKS_",
        env_file=(".env",),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------------------------------------------------------------- storage
    # The durable database. Only the collector writes time-series rows here
    # (see docs/spec-review.md A-3 for why symbol tables are an exception).
    db_path: Path = REPO_ROOT / "data" / "market.db"
    # Live state is a separate database so that per-tick snapshot churn never
    # contends with bar writes on the durable file. Point it at /dev/shm in
    # production to avoid SSD wear.
    live_db_path: Path = REPO_ROOT / "data" / "live.db"
    sqlite_busy_timeout_ms: int = 5_000

    # ----------------------------------------------------------- market data
    primary_source: str = "tiingo"
    # Order in which sources win when the same (symbol, minute) exists twice.
    # Spec gap B-1: the spec makes `source` part of the unique key but never
    # says which row to draw.
    source_priority: list[str] = Field(default_factory=lambda: ["tiingo", "alpaca"])

    tiingo_api_key: str | None = None
    tiingo_rest_base: str = "https://api.tiingo.com"
    tiingo_ws_url: str = "wss://api.tiingo.com/iex"
    # Tiingo threshold level. 5 = last-sale/trade updates only. Subscribing to
    # quotes multiplies bandwidth and cannot produce OHLCV (spec gap B-3).
    tiingo_threshold_level: int = 5

    alpaca_api_key: str | None = None
    alpaca_api_secret: str | None = None
    alpaca_rest_base: str = "https://data.alpaca.markets"
    alpaca_ws_url: str = "wss://stream.data.alpaca.markets/v2/iex"

    # --------------------------------------------------------- rate budgets
    # Free-tier budgets. Enforced by a persistent token bucket so that restarts
    # do not reset consumption (spec gap A-2).
    rest_calls_per_hour: int = 50
    rest_calls_per_day: int = 1_000
    # Monthly ingress budget in bytes. Measured, not assumed (spec gap A-1).
    monthly_bandwidth_bytes: int = 1_000_000_000
    bandwidth_warn_ratio: float = 0.8

    # -------------------------------------------------------------- collector
    # Capped at 10 rather than the spec's 30: at 30 symbols the free tier's
    # 1 GB/month ingress is exceeded on any realistic assumption
    # (docs/spec-review.md A-1). 10 keeps the workload inside the budget.
    max_symbols: int = 10
    symbol_refresh_seconds: float = 5.0
    reconnect_initial_seconds: float = 1.0
    reconnect_max_seconds: float = 60.0
    reconnect_jitter_ratio: float = 0.25
    # Do not spend REST budget backfilling a blip.
    backfill_min_gap_seconds: int = 120
    # How long a late trade may still be folded into an already-rolled bar.
    late_trade_grace_seconds: int = 90
    # Debounce for publishing live snapshots to the live database.
    live_publish_interval_seconds: float = 0.25
    # Spec 10.2 offers three options for second-level data with no decision.
    # Default: do not store (option 1). Set >0 to retain that many days.
    tick_retention_days: int = 0

    # -------------------------------------------------------------------- api
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    sse_interval_seconds: float = 1.0
    sse_heartbeat_seconds: float = 15.0
    # Streams are closed and re-established periodically. EventSource
    # reconnects on its own, and a bounded lifetime means a client that
    # vanished without a detectable disconnect cannot pin a database
    # connection indefinitely.
    sse_max_stream_seconds: float = 3600.0
    max_bars_per_request: int = 20_000
    web_dir: Path = REPO_ROOT / "web"

    # ------------------------------------------------------------------- auth
    auth_mode: AuthMode = "cloudflare_access"
    cf_access_team_domain: str | None = None  # e.g. "myteam.cloudflareaccess.com"
    cf_access_aud: str | None = None
    allowed_emails: list[str] = Field(default_factory=list)
    jwks_cache_seconds: int = 3_600

    # ----------------------------------------------------------------- misc
    log_level: str = "INFO"
    display_timezone: str = "America/New_York"

    @field_validator("source_priority", "allowed_emails", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("allowed_emails")
    @classmethod
    def _lowercase_emails(cls, value: list[str]) -> list[str]:
        return [email.strip().lower() for email in value]

    def validate_for_serving(self) -> None:
        """Fail fast on configurations that would expose the app.

        Spec gap A-4: the spec puts the entire access control burden on
        Cloudflare. Refusing to serve unauthenticated on a non-loopback
        address means a mis-set env var cannot quietly publish the app.
        """
        if self.auth_mode == "disabled":
            if self.api_host not in {"127.0.0.1", "::1", "localhost"}:
                raise RuntimeError(
                    "auth_mode=disabled is only allowed on a loopback bind address; "
                    f"api_host={self.api_host!r}"
                )
            return
        missing = [
            name
            for name, value in (
                ("cf_access_team_domain", self.cf_access_team_domain),
                ("cf_access_aud", self.cf_access_aud),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "cloudflare_access auth requires: " + ", ".join(missing)
            )
        if not self.allowed_emails:
            raise RuntimeError("allowed_emails must list at least one address")

    def jwks_url(self) -> str:
        domain = (self.cf_access_team_domain or "").rstrip("/")
        if not domain.startswith("http"):
            domain = f"https://{domain}"
        return f"{domain}/cdn-cgi/access/certs"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
