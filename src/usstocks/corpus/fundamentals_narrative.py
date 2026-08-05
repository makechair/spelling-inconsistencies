"""Add a local-Qwen reading guide to each symbol's fundamentals.

Phase D of docs/earnings-spec.md, on the same contract as narrative.py: the
model selects fact IDs and explains their significance, and every figure the
reader sees is copied out of the metrics by this process. It is not asked to
calculate, and validate_digest rejects a figure that is not in the evidence
it cited.

Runs where Ollama runs -- the Mac -- so it reaches the summary through the S3
exchange rather than reading the corpus directly.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .narrative import validate_digest

# Enough to reason about a cycle without burying the model in near-duplicates.
FACT_SPECS: tuple[tuple[str, str, str], ...] = (
    ("revenue_yoy", "増収率", "percent"),
    ("revenue_yoy_change", "増収率の前期差（プラスは加速、マイナスは減速）", "points"),
    ("gross_margin", "粗利率", "percent"),
    ("gross_margin_yoy_change", "粗利率の前年差", "points"),
    ("operating_margin", "営業利益率", "percent"),
    ("operating_margin_yoy_change", "営業利益率の前年差", "points"),
    ("inventory_days", "在庫日数（短縮が改善）", "days"),
    ("inventory_days_yoy_change", "在庫日数の前年差", "days_delta"),
    ("capex_intensity", "設備投資の売上比", "percent"),
    ("rd_intensity", "研究開発費の売上比", "percent"),
    ("free_cash_flow_margin", "FCFマージン", "percent"),
)


def _format(value: float, kind: str) -> str:
    if kind == "percent":
        return f"{value * 100:.1f}%"
    if kind == "points":
        return f"{value * 100:+.1f}pt"
    if kind == "days_delta":
        # Signed, because the direction is the whole point of a change.
        return f"{value:+.1f}日"
    return f"{value:.0f}日"


def build_facts(row: dict[str, Any]) -> list[dict[str, str]]:
    """One fact per metric the filer actually reported.

    A missing metric produces no fact at all, so the model is never handed a
    blank to interpret as a zero.
    """
    facts: list[dict[str, str]] = []
    for name, label, kind in FACT_SPECS:
        value = row.get(name)
        if value is None:
            continue
        rendered = _format(float(value), kind)
        facts.append({"id": f"F{len(facts) + 1}", "text": f"{label}: {rendered}"})
    basis = "直近12ヶ月の合計" if row.get("basis") == "ttm" else "通期決算"
    facts.append(
        {
            "id": f"F{len(facts) + 1}",
            "text": f"集計基準: {basis}（期末 {row.get('period_end')}）",
        }
    )
    return facts


def _prompt(row: dict[str, Any], facts: list[dict[str, str]]) -> list[dict[str, str]]:
    schema = {
        "overview": "この銘柄の決算の全体像を一文で。数値は書かない",
        "points": [
            {
                "title": "短い見出し",
                "interpretation": "決算をどう読むか。断定や予測はせず、数値も再記述しない",
                "evidence_ids": ["F1"],
            }
        ],
        "caution": "見落としやすい点を一文で。数値は書かない",
    }
    return [
        {
            "role": "system",
            "content": (
                "あなたは決算指標レポートの編集者です。予測や投資助言ではなく、提示済みの"
                "確定値を読みやすく整理します。新しい数値・銘柄・因果関係を作らず、数値を"
                "本文へ転記しません。根拠は必ずfactsのIDだけを指定します。JSON以外は返しません。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "symbol": row.get("symbol"),
                    "subsector": row.get("subsector"),
                    "facts": facts,
                    "required_schema": schema,
                    "requirements": (
                        "pointsは重要度順に2〜3件。簡潔な日本語。"
                        "半導体は在庫日数と設備投資比が景気循環の局面を示す点を踏まえる。"
                    ),
                },
                ensure_ascii=False,
            ),
        },
    ]


def enrich_symbol(
    row: dict[str, Any], *, model: str, ollama_url: str, timeout: float
) -> dict[str, Any] | None:
    facts = build_facts(row)
    # One lone fact is the reporting basis; there is nothing to interpret.
    if len(facts) < 3:
        return None
    response = httpx.post(
        f"{ollama_url.rstrip('/')}/api/chat",
        json={
            "model": model,
            "messages": _prompt(row, facts),
            "format": "json",
            "stream": False,
            "options": {"temperature": 0.1, "num_ctx": 8192},
        },
        timeout=timeout,
    )
    response.raise_for_status()
    content = response.json().get("message", {}).get("content")
    return validate_digest(json.loads(content), facts)


def enrich(
    summary_path: Path,
    output_path: Path,
    *,
    model: str,
    ollama_url: str,
    timeout: float,
    limit: int | None = None,
) -> dict[str, Any]:
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    rows = summary.get("symbols")
    if not isinstance(rows, list) or not rows:
        raise ValueError("summary contains no symbols")
    if limit is not None:
        rows = rows[:limit]

    digests: dict[str, Any] = {}
    failures: dict[str, str] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        try:
            digest = enrich_symbol(
                row, model=model, ollama_url=ollama_url, timeout=timeout
            )
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            # One symbol the model mangled must not cost the other fifty-two.
            failures[symbol] = type(exc).__name__
            continue
        if digest is not None:
            digests[symbol] = digest

    payload = {
        "version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "model": model,
        "source_generated_at": summary.get("generated_at"),
        "symbols": digests,
        "failed": failures,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=output_path.parent, prefix=".fundamentals-digest-", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, output_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--model", default="qwen3:14b")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    payload = enrich(
        args.summary,
        args.output,
        model=args.model,
        ollama_url=args.ollama_url,
        timeout=args.timeout,
        limit=args.limit,
    )
    print(
        json.dumps(
            {
                "symbols": len(payload["symbols"]),
                "failed": len(payload["failed"]),
                "model": payload["model"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":  # pragma: no cover
    main()
