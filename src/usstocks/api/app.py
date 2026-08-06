"""FastAPI application.

Serves the static frontend, the JSON API and the SSE stream from one origin
(spec 7.1), behind a single authentication middleware that covers all three
(spec 3.5).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..adapters.registry import build_adapter
from ..collector.ratelimit import RestBudget
from ..config import Settings, get_settings
from ..db.live_store import LiveStore
from ..db.migrate import migrate, migrate_live
from ..db.repository import Repository
from ..logging_setup import configure_logging
from .auth import AccessVerifier, AuthError, extract_token
from .deps import AppState
from .routes import (
    analysis,
    bars,
    coverage,
    export,
    fundamentals,
    health,
    live,
    symbols,
    watchlists,
)

log = logging.getLogger(__name__)

# Nothing else is reachable unauthenticated. /api/ping is deliberately NOT
# here: the browser uses its 401 to detect an expired Access session.
PUBLIC_PATHS = frozenset({"/api/livez"})


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    settings.validate_for_serving()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        migrate(settings.db_path, busy_timeout_ms=settings.sqlite_busy_timeout_ms)
        migrate_live(settings.live_db_path, busy_timeout_ms=settings.sqlite_busy_timeout_ms)

        adapter = None
        try:
            adapter = build_adapter(settings)
        except RuntimeError as exc:
            # Search degrades to local-only rather than blocking startup; the
            # collector is the process that truly needs credentials.
            log.warning("provider adapter unavailable for search: %s", exc)

        state = AppState(
            settings=settings,
            repository=Repository(
                settings.db_path,
                busy_timeout_ms=settings.sqlite_busy_timeout_ms,
                source_priority=settings.source_priority,
            ),
            live_store=LiveStore(
                settings.live_db_path,
                busy_timeout_ms=settings.sqlite_busy_timeout_ms,
            ),
            verifier=(
                AccessVerifier(settings) if settings.auth_mode == "cloudflare_access" else None
            ),
            adapter=adapter,
        )
        if adapter is not None:
            state.rest_budget = RestBudget(
                state.repository,
                adapter.name,
                per_hour=settings.rest_calls_per_hour,
                per_day=settings.rest_calls_per_day,
                monthly_bandwidth_bytes=settings.monthly_bandwidth_bytes,
            )
        app.state.app_state = state
        log.info(
            "api ready (auth=%s, db=%s)", settings.auth_mode, settings.db_path
        )
        try:
            yield
        finally:
            if adapter is not None:
                await adapter.close()
            state.close()

    app = FastAPI(
        title="Personal US Stock Chart",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    @app.middleware("http")
    async def require_access(request: Request, call_next):
        """Authenticate everything, including static files and SSE (spec 3.5).

        Verifying the Cloudflare Access assertion in the app means a mistake in
        the Cloudflare dashboard alone cannot expose the site
        (docs/spec-review.md A-4).
        """
        state: AppState | None = getattr(request.app.state, "app_state", None)
        if state is None or state.verifier is None or request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        token = extract_token(dict(request.headers), request.cookies)
        try:
            identity = state.verifier.verify(token)
        except AuthError as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": str(exc)},
                headers={"Cache-Control": "no-store"},
            )
        request.state.identity = identity
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/analysis/"):
            # Daily reports are regenerated in-place.  A browser or reverse
            # proxy must therefore revalidate the same dated URL instead of
            # keeping the pre-regeneration JSON response.
            response.headers["Cache-Control"] = (
                "no-store, no-cache, must-revalidate, max-age=0"
            )
            response.headers["CDN-Cache-Control"] = "no-store"
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        # Everything the page needs is served from this origin, so the policy
        # can forbid outbound loads entirely.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'",
        )
        return response

    app.include_router(health.router)
    app.include_router(symbols.router)
    app.include_router(bars.router)
    app.include_router(coverage.router)
    app.include_router(live.router)
    app.include_router(export.router)
    app.include_router(analysis.router)
    app.include_router(fundamentals.router)
    app.include_router(watchlists.router)

    @app.get("/api/livez", include_in_schema=False)
    def livez() -> dict:
        """Unauthenticated process-liveness probe.

        Bound to loopback in practice; it reveals nothing but that the process
        answers, which is what a local supervisor needs.
        """
        return {"ok": True}

    web_dir = settings.web_dir
    if web_dir.is_dir():
        app.mount(
            "/static", StaticFiles(directory=web_dir, html=False), name="static"
        )

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(web_dir / "index.html")

        @app.get("/reports", include_in_schema=False)
        def analysis_reports() -> FileResponse:
            return FileResponse(web_dir / "reports.html")

        @app.get("/coverage", include_in_schema=False)
        def data_coverage() -> FileResponse:
            return FileResponse(web_dir / "coverage.html")

        @app.get("/fundamentals", include_in_schema=False)
        def fundamentals_page() -> FileResponse:
            return FileResponse(web_dir / "fundamentals.html")

        @app.get("/favicon.ico", include_in_schema=False)
        def favicon() -> FileResponse:
            # Browsers request this regardless of the <link> tag.
            return FileResponse(web_dir / "favicon.svg", media_type="image/svg+xml")
    else:  # pragma: no cover - misconfiguration
        log.warning("web directory not found: %s", web_dir)

    return app


app = None  # populated by __main__ / uvicorn factory


def get_app() -> FastAPI:
    return create_app()
