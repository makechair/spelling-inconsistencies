"""Read-only access to the small JSON event-study report archive."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ...config import Settings
from ..deps import get_settings_dep

router = APIRouter(prefix="/api/analysis", tags=["analysis"])


def _analysis_root(settings: Settings) -> Path:
    if settings.analysis_output_dir is not None:
        return settings.analysis_output_dir.parent
    return settings.corpus_local_dir / "analysis"


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="analysis report not found") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=503, detail="analysis report is unavailable") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=503, detail="analysis report is invalid")
    return payload


@router.get("/reports")
def reports(settings: Settings = Depends(get_settings_dep)) -> dict[str, Any]:
    index_path = _analysis_root(settings) / "index.json"
    if not index_path.exists():
        return {"version": 1, "latest_report_date": None, "reports": []}
    payload = _read_object(index_path)
    if not isinstance(payload.get("reports"), list):
        raise HTTPException(status_code=503, detail="analysis report index is invalid")
    return payload


@router.get("/reports/latest")
def latest_report(settings: Settings = Depends(get_settings_dep)) -> dict[str, Any]:
    index = reports(settings)
    latest = index.get("latest_report_date")
    if not latest:
        raise HTTPException(status_code=404, detail="analysis report not found")
    try:
        report_date = date.fromisoformat(str(latest))
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="analysis report index is invalid") from exc
    return _report_for_date(report_date, settings)


def _report_for_date(report_date: date, settings: Settings) -> dict[str, Any]:
    path = (
        _analysis_root(settings)
        / "daily"
        / f"date={report_date.isoformat()}"
        / "report.json"
    )
    return _read_object(path)


@router.get("/reports/{report_date}")
def report_for_date(
    report_date: date,
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, Any]:
    return _report_for_date(report_date, settings)
