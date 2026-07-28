"""Health and operational status (spec 3.6, 4.4).

Reports process liveness, collector connectivity and last data receipt, and
adds the two numbers the spec's own free-tier table implies but never tracks:
month-to-date ingress against the 1 GB budget, and REST calls against the
hourly and daily allowances (docs/spec-review.md A-1, A-2).
"""

from __future__ import annotations

import os
import shutil
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Response

from ...collector.ratelimit import RestBudget
from ...config import Settings
from ...db.live_store import LiveStore
from ...db.repository import Repository
from ..deps import get_live_store, get_repository, get_settings_dep

router = APIRouter(tags=["ops"])

_STARTED_AT = time.time()

# How long without a collector status write before we call it stale. The
# collector rewrites status at least once per publish cycle.
STATUS_STALE_SECONDS = 60.0


@router.get("/api/health")
def health(
    response: Response,
    repository: Repository = Depends(get_repository),
    live_store: LiveStore = Depends(get_live_store),
    settings: Settings = Depends(get_settings_dep),
) -> dict:
    now = datetime.now(tz=UTC)
    status, status_at = live_store.read_status()

    status_age = (now - status_at).total_seconds() if status_at else None
    collector_alive = status_age is not None and status_age < STATUS_STALE_SECONDS
    connected = bool(status and status.get("connected"))

    last_trade_at = (status or {}).get("last_trade_at")
    data_age = None
    if last_trade_at:
        data_age = (now - datetime.fromisoformat(last_trade_at)).total_seconds()

    budget = RestBudget(
        repository,
        settings.primary_source,
        per_hour=settings.rest_calls_per_hour,
        per_day=settings.rest_calls_per_day,
        monthly_bandwidth_bytes=settings.monthly_bandwidth_bytes,
    ).snapshot(now)

    usage = shutil.disk_usage(settings.db_path.parent if settings.db_path.parent.exists() else ".")
    db_bytes = repository.database_size_bytes()

    problems: list[str] = []
    if not collector_alive:
        problems.append("collector_status_stale")
    elif not connected:
        problems.append("collector_disconnected")
    if budget.bandwidth_ratio >= settings.bandwidth_warn_ratio:
        problems.append("bandwidth_budget_high")
    if usage.used / usage.total >= 0.7:
        # Spec 10.3 asks to act before 70% disk. Backups need headroom equal to
        # the database itself (VACUUM INTO writes a full copy).
        problems.append("disk_usage_high")
    if usage.free < db_bytes * 2:
        problems.append("insufficient_headroom_for_backup")

    overall = "ok" if not problems else ("degraded" if collector_alive else "down")
    if overall != "ok":
        response.status_code = 503 if overall == "down" else 200

    return {
        "status": overall,
        "problems": problems,
        "server_time": now.isoformat(),
        "api": {
            "pid": os.getpid(),
            "uptime_seconds": round(time.time() - _STARTED_AT, 1),
            "auth_mode": settings.auth_mode,
        },
        "collector": {
            "alive": collector_alive,
            "connected": connected,
            "status_age_seconds": status_age,
            "source": (status or {}).get("source"),
            "connected_since": (status or {}).get("connected_since"),
            "last_message_at": (status or {}).get("last_message_at"),
            "last_trade_at": last_trade_at,
            "data_age_seconds": data_age,
            "subscribed_symbols": (status or {}).get("subscribed_symbols", []),
            "reconnect_count": (status or {}).get("reconnect_count"),
            "last_error": (status or {}).get("last_error"),
        },
        "database": {
            "path": str(settings.db_path),
            "size_bytes": db_bytes,
            "bars": repository.count_bars(),
            "disk_total_bytes": usage.total,
            "disk_free_bytes": usage.free,
            "disk_used_ratio": round(usage.used / usage.total, 4),
        },
        "bandwidth": {
            "month_to_date_bytes": budget.bytes_month,
            "monthly_budget_bytes": budget.limit_month_bytes,
            "ratio": round(budget.bandwidth_ratio, 4),
            "warn_ratio": settings.bandwidth_warn_ratio,
        },
        "rest_budget": {
            "calls_this_hour": budget.calls_hour,
            "hourly_limit": budget.limit_hour,
            "calls_today": budget.calls_day,
            "daily_limit": budget.limit_day,
        },
    }


@router.get("/api/ping")
def ping() -> dict:
    """Cheapest possible authenticated probe.

    The browser calls this when the SSE stream drops: a 401 or a redirect here
    means the Access session expired rather than the server dying, which is the
    case the UI must handle by reloading (docs/spec-review.md B-6).
    """
    return {"ok": True, "server_time": datetime.now(tz=UTC).isoformat()}
