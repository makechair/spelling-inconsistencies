"""Turn deterministic event-study facts into a short local-Qwen reading guide.

The model is deliberately not asked to calculate or repeat figures.  It may
only select fact IDs and explain their significance; the exact evidence shown
to the reader is copied from report.json by this process.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx


def _facts(report: dict[str, Any]) -> list[dict[str, str]]:
    facts: list[dict[str, str]] = []
    for index, finding in enumerate(report.get("findings", []), 1):
        if not isinstance(finding, dict):
            continue
        title, body = str(finding.get("title", "")).strip(), str(finding.get("body", "")).strip()
        if title and body:
            facts.append({"id": f"F{index}", "text": f"{title}: {body}"})

    focus = str(report.get("focus_symbol") or "")
    simulations = report.get("walk_forward_simulations", {}).get(focus, [])
    if isinstance(simulations, list):
        ranked = sorted(
            (row for row in simulations if isinstance(row, dict)),
            key=lambda row: int(row.get("test_trades") or 0),
            reverse=True,
        )[:3]
        for index, row in enumerate(ranked, 1):
            facts.append({
                "id": f"V{index}",
                "text": (
                    f"{focus}のウォークフォワード検証: 変動帯{row.get('bucket_label', '—')}、"
                    f"買い{row.get('buy_day', '—')}日後、売り{row.get('sell_day', '—')}日後、"
                    f"検証件数{row.get('test_trades', '—')}、勝率{row.get('test_win_rate', '—')}、"
                    f"平均リターン{row.get('test_mean_return', '—')}"
                ),
            })
    return facts[:12]


def _prompt(report: dict[str, Any], facts: list[dict[str, str]]) -> list[dict[str, str]]:
    schema = {
        "overview": "全体像を一文で。数値は書かない",
        "points": [{
            "title": "短い見出し",
            "interpretation": "売買判断でどう読むか。断定や数値の再記述はしない",
            "evidence_ids": ["F1"],
        }],
        "caution": "最重要の注意点を一文で。数値は書かない",
    }
    return [
        {
            "role": "system",
            "content": (
                "あなたは株式統計レポートの編集者です。予測や投資助言ではなく、提示済みの"
                "確定値を読みやすく整理します。新しい数値・銘柄・因果関係を作らず、数値を"
                "本文へ転記しません。根拠は必ずfactsのIDだけを指定します。JSON以外は返しません。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "report_date": report.get("report_date"),
                    "focus_symbol": report.get("focus_symbol"),
                    "facts": facts,
                    "required_schema": schema,
                    "requirements": "pointsは重要度順に2〜3件。簡潔な日本語。",
                },
                ensure_ascii=False,
            ),
        },
    ]


def _validate(raw: Any, facts: list[dict[str, str]]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("Qwen response must be a JSON object")
    allowed = {fact["id"]: fact["text"] for fact in facts}
    overview, caution = raw.get("overview"), raw.get("caution")
    if not isinstance(overview, str) or not overview.strip():
        raise ValueError("overview is missing")
    if not isinstance(caution, str) or not caution.strip():
        raise ValueError("caution is missing")
    if re.search(r"[0-9０-９]", overview + caution):
        raise ValueError("overview and caution must not contain figures")
    points = raw.get("points")
    if not isinstance(points, list) or not 1 <= len(points) <= 3:
        raise ValueError("points must contain one to three items")
    validated = []
    for point in points:
        if not isinstance(point, dict):
            raise ValueError("point must be an object")
        ids = point.get("evidence_ids")
        if not isinstance(ids, list) or not ids or any(item not in allowed for item in ids):
            raise ValueError("point contains an unknown evidence ID")
        title, interpretation = point.get("title"), point.get("interpretation")
        if not isinstance(title, str) or not isinstance(interpretation, str):
            raise ValueError("point text is invalid")
        evidence_text = " ".join(allowed[item] for item in ids)
        for figure in re.findall(r"[+-]?[0-9０-９]+(?:[.,][0-9０-９]+)?%?", interpretation):
            if figure not in evidence_text:
                raise ValueError(f"interpretation contains an unsupported figure: {figure}")
        validated.append({
            "title": title.strip(),
            "interpretation": interpretation.strip(),
            "evidence": [{"id": item, "text": allowed[item]} for item in ids[:3]],
        })
    return {
        "overview": overview.strip(),
        "points": validated,
        "caution": caution.strip(),
    }


def enrich(
    report_path: Path,
    output_path: Path,
    *,
    model: str,
    ollama_url: str,
    timeout: float,
) -> dict[str, Any]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    facts = _facts(report)
    if not facts:
        raise ValueError("report contains no narrative facts")
    response = httpx.post(
        f"{ollama_url.rstrip('/')}/api/chat",
        json={
            "model": model,
            "messages": _prompt(report, facts),
            "format": "json",
            "stream": False,
            "options": {"temperature": 0.1, "num_ctx": 8192},
        },
        timeout=timeout,
    )
    response.raise_for_status()
    content = response.json().get("message", {}).get("content")
    digest = _validate(json.loads(content), facts)
    payload = {
        "version": 1,
        "report_date": report.get("report_date"),
        "generated_at": datetime.now(UTC).isoformat(),
        "model": model,
        **digest,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=output_path.parent, prefix=".ai-digest-", suffix=".json")
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
    parser = argparse.ArgumentParser(description="Add a local-Qwen reading guide to a report")
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model", default="qwen3:14b")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args()
    output = args.output or args.report.with_name("ai_digest.json")
    enrich(args.report, output, model=args.model, ollama_url=args.ollama_url, timeout=args.timeout)
    print(output)


if __name__ == "__main__":
    main()
