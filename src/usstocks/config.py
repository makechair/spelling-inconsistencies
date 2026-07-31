"""Runtime configuration.

All secrets come from the environment or a secrets file; nothing is committed.
See ``.env.example`` for the full list.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

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
    source_priority: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["tiingo", "alpaca"]
    )

    tiingo_api_key: str | None = None
    tiingo_rest_base: str = "https://api.tiingo.com"
    tiingo_ws_url: str = "wss://api.tiingo.com/iex"
    # Tiingo IEX threshold level, or None to let the plan decide.
    #
    # Level 5 (last-sale only) is what this workload wants -- quotes carry no
    # size, cannot produce OHLCV, and are the largest single contributor to the
    # 1 GB/month budget (spec gaps A-1, B-3). But it is not accepted on every
    # tier: a free key is refused at subscribe time and the socket is closed,
    # leaving the collector reconnecting indefinitely. Defaulting to None keeps
    # a fresh install connecting; set it once you know your tier allows a level.
    tiingo_threshold_level: int | None = None

    # Local ticker catalog. A static file, not an API endpoint, so importing
    # it spends none of the hourly REST allowance -- and being the provider's
    # own universe, it cannot list a symbol the price endpoints will not serve.
    catalog_url: str = (
        "https://apimedia.tiingo.com/docs/tiingo/daily/supported_tickers.zip"
    )
    catalog_timeout_seconds: float = 120.0
    # The file carries every ticker ever covered. Anything whose coverage ended
    # before this many days ago is dropped, so a search for a live symbol is not
    # buried under decades of delisted ones.
    catalog_keep_days: int = 30

    alpaca_api_key: str | None = None
    alpaca_api_secret: str | None = None
    alpaca_rest_base: str = "https://data.alpaca.markets"
    # "iex" on the free plan, "sip" on a paid one. SIP is the consolidated tape:
    # every venue, so a minute in which the stock traded anywhere produces a bar
    # rather than only the ~2-3% of volume IEX sees. Nothing else in the adapter
    # changes -- this is the whole difference between the sparse chart the free
    # feed draws and a continuous one.
    alpaca_feed: str = "iex"
    # Left empty to follow alpaca_feed. Set only to point at a different host.
    alpaca_ws_url: str | None = None

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
    # Persist collector liveness even when the market is closed or a watched
    # symbol produces no trades. This is deliberately much slower than live
    # publishing so the heartbeat does not create avoidable SQLite churn.
    collector_status_interval_seconds: float = Field(default=15.0, gt=0, le=30)
    # REST polling, which is the live path now that neither free tier streams
    # (spec-review A-6). The allowance is 50 calls an hour: spread over ten
    # symbols that is one refresh every twelve minutes, aimed at the symbol on
    # screen it is one every seventy-two seconds. 90 leaves headroom for a
    # reconnect backfill or a symbol search to land without starving the chart.
    foreground_poll_seconds: float = 90.0
    # The others are not starved, only deferred: a REST call returns every
    # minute since the last stored bar, so an unopened symbol fills completely
    # the moment it is opened. This sweep exists because backfill reaches back
    # only max_lookback_days -- without it, a symbol left unopened past that
    # window would lose history for good.
    background_poll_seconds: float = 3600.0
    # How long after a request a symbol still counts as being watched.
    viewer_idle_seconds: float = 300.0
    # Tiingo IEX stops adding bars at about 16:45 ET. Continuing until the
    # exchange's 20:00 extended-hours boundary only burns the free REST budget.
    # Express this relative to the regular close so early-close days stop at
    # 13:45 ET rather than using a hard-coded wall-clock time.
    rest_poll_after_close_minutes: int = Field(default=45, ge=0, le=240)
    # A successful request with no bars is not an adapter failure, but several
    # in a row during the polling window is actionable. Surface it on the
    # symbol instead of leaving a silently frozen chart.
    empty_fetch_warning_threshold: int = Field(default=3, ge=1, le=20)
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
    # Bound deploy downtime when long-lived SSE clients are connected. Uvicorn
    # cancels the remaining streams after this grace period; EventSource
    # reconnects automatically to the restarted process.
    api_graceful_shutdown_seconds: int = Field(default=10, ge=1, le=60)
    max_bars_per_request: int = 20_000
    web_dir: Path = REPO_ROOT / "web"

    # ---------------------------------------------------------- daily corpus
    # The timer overrides these paths to release-independent durable locations.
    corpus_local_dir: Path = REPO_ROOT / "data" / "corpus"
    corpus_universe_path: Path = REPO_ROOT / "data" / "universe.csv"
    # Defaults to <USSTOCKS_BACKUP_S3_URI>/corpus when omitted.
    backup_s3_uri: str | None = None
    corpus_s3_uri: str | None = None
    # Grow the provider's unknown monthly unique-symbol allowance cautiously.
    corpus_max_symbols_per_run: int = Field(default=10, ge=1, le=50)
    corpus_max_new_symbols_per_run: int = Field(default=3, ge=0, le=50)
    corpus_refresh_hours: float = Field(default=20.0, gt=0)
    corpus_overlap_days: int = Field(default=14, ge=1, le=90)

    # ------------------------------------------------------------ news corpus
    # Direct values are useful for local development. Production leaves these
    # empty and resolves the two SecureString parameters from the teiten
    # pipeline's SSM prefix only when the news oneshot runs.
    notion_token: str | None = None
    notion_db_id: str | None = None
    notion_ssm_prefix: str = "/teiten"
    notion_api_base: str = "https://api.notion.com/v1"
    notion_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    # Defaults to <corpus_s3_root>/news.
    news_s3_uri: str | None = None
    news_max_pages: int = Field(default=10_000, ge=1, le=100_000)

    # ------------------------------------------------------------------- auth
    auth_mode: AuthMode = "cloudflare_access"
    cf_access_team_domain: str | None = None  # e.g. "myteam.cloudflareaccess.com"
    cf_access_aud: str | None = None
    allowed_emails: Annotated[list[str], NoDecode] = Field(default_factory=list)
    jwks_cache_seconds: int = 3_600

    # ----------------------------------------------------------------- misc
    log_level: str = "INFO"

    @field_validator("source_priority", "allowed_emails", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Accept ``a@b.com,c@d.com`` as well as a JSON array.

        Both fields carry ``NoDecode`` for this to be reachable. pydantic-settings
        treats a ``list`` field as complex and JSON-decodes it inside the
        environment source, before any model validator runs -- so without
        ``NoDecode`` the obvious value fails several frames deep:

            json.decoder.JSONDecodeError: Expecting value: line 1 column 1
            SettingsError: error parsing value for field "allowed_emails"

        which names neither the variable's content nor the accepted format. This
        validator existed and looked like it handled the plain form; it was only
        ever exercised by constructing Settings directly in tests, where the
        decoding step does not apply.
        """
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                return json.loads(text)
            return [item.strip() for item in text.split(",") if item.strip()]
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
