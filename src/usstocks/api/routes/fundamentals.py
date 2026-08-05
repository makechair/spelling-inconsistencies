"""Read-only access to the fundamentals summary the metrics job writes.

The job prepares the JSON so this only has to serve a file. Reading the
Parquet here would mean DuckDB inside the API process, and the instance has
320MB for it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ...config import Settings
from ..deps import get_settings_dep

router = APIRouter(prefix="/api/fundamentals", tags=["fundamentals"])


def _summary_path(settings: Settings) -> Path:
    return settings.corpus_local_dir / "fundamentals" / "summary.json"


def _read_summary(settings: Settings) -> dict[str, Any]:
    path = _summary_path(settings)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail="fundamentals summary has not been generated yet"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=503, detail="fundamentals summary is unavailable"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
        raise HTTPException(status_code=503, detail="fundamentals summary is invalid")
    return payload


@router.get("")
def summary(settings: Settings = Depends(get_settings_dep)) -> dict[str, Any]:
    """Every symbol's latest annual figures, for the cross-sectional table."""
    payload = _read_summary(settings)
    return {
        key: value for key, value in payload.items() if key != "symbols"
    } | {
        # History is only needed on a detail view; sending 53 symbols' worth
        # of it makes the table's first paint several times heavier.
        "symbols": [
            {
                key: value
                for key, value in row.items()
                if key not in ("history", "quarterly_history")
            }
            for row in payload["symbols"]
        ]
    }


def _read_digest(settings: Settings) -> dict[str, Any]:
    """The Qwen reading guide, if the Mac-side pass has produced one.

    Its absence is ordinary -- the bridge may not have run, or may have failed
    on this symbol -- so it never turns a working detail view into an error.
    """
    path = settings.corpus_local_dir / "fundamentals" / "digest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


@router.get("/{symbol}")
def detail(symbol: str, settings: Settings = Depends(get_settings_dep)) -> dict[str, Any]:
    payload = _read_summary(settings)
    wanted = symbol.strip().upper()
    for row in payload["symbols"]:
        if str(row.get("symbol", "")).upper() == wanted:
            digest = _read_digest(settings)
            guide = (digest.get("symbols") or {}).get(row["symbol"])
            if isinstance(guide, dict):
                return row | {"narrative": guide | {"model": digest.get("model")}}
            return row
    raise HTTPException(status_code=404, detail="symbol has no fundamentals yet")
