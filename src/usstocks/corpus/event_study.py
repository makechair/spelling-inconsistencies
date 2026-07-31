"""Build the Phase 3 Notion-event × adjusted-daily-return study.

The job is deliberately a one-shot batch. It reads the durable local Parquet
mirror produced by the Phase 1/2 timers, lets DuckDB align events to trading
sessions and calculate returns, then writes Parquet plus human-readable
Markdown/HTML. It makes no Tiingo or Notion requests.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import shutil
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..config import Settings, get_settings
from ..logging_setup import configure_logging
from .daily import CorpusError, Uploader, aws_upload, corpus_s3_root, save_state

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
JST = ZoneInfo("Asia/Tokyo")
HORIZONS = (0, 1, 2, 5, 20)
OUTPUT_NAMES = (
    "event_returns.parquet",
    "event_summary.parquet",
    "event_unmatched.parquet",
    "report.json",
    "report.md",
    "report.html",
)


def analysis_s3_root(settings: Settings) -> str:
    explicit = (settings.analysis_s3_uri or "").strip().rstrip("/")
    # Before daily archives existed this setting was documented as the latest
    # directory itself. Accept that old form during the transition.
    if explicit.endswith("/latest"):
        explicit = explicit.removesuffix("/latest")
    return explicit or f"{corpus_s3_root(settings)}/analysis"


def classify_event_time(
    published_at: str | None,
    event_date: date,
) -> tuple[date, str, str]:
    """Return candidate date, timing quality and market-time bucket.

    The candidate is not assumed to be a session. DuckDB advances it to the
    first date that actually exists in that symbol's daily series.
    """

    value = (published_at or "").strip()
    if not value or len(value) <= 10:
        return event_date, "date_only", "date_only"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CorpusError(f"event has invalid published_at: {value!r}") from exc
    if parsed.tzinfo is None:
        try:
            candidate = date.fromisoformat(value[:10])
        except ValueError as exc:
            raise CorpusError(f"event has invalid published_at: {value!r}") from exc
        return candidate, "date_only", "date_only"

    local = parsed.astimezone(ET)
    wall_time = local.timetz().replace(tzinfo=None)
    if wall_time < time(9, 30):
        return local.date(), "timestamped", "pre_market"
    if wall_time < time(16, 0):
        return local.date(), "timestamped", "regular"
    return local.date() + timedelta(days=1), "timestamped", "after_hours"


def _analysis_modules() -> tuple[Any, Any, Any]:
    try:
        import duckdb
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise CorpusError("event study requires the 'parquet' package extra") from exc
    return duckdb, pa, pq


def _discover_inputs(local_root: Path) -> tuple[list[Path], list[Path], Path]:
    daily = sorted(local_root.glob("daily/symbol=*/part.parquet"))
    news = sorted(local_root.glob("news/date=*/part.parquet"))
    sectors = local_root / "universe" / "sectors.parquet"
    missing: list[str] = []
    if not daily:
        missing.append("daily/symbol=*/part.parquet")
    if not news:
        missing.append("news/date=*/part.parquet")
    if not sectors.is_file():
        missing.append("universe/sectors.parquet")
    if missing:
        raise CorpusError("event study input is missing: " + ", ".join(missing))
    return daily, news, sectors


def _timed_event_schema(pa: Any) -> Any:
    return pa.schema(
        [
            ("event_key", pa.string()),
            ("page_id", pa.string()),
            ("symbol", pa.string()),
            ("event_date", pa.date32()),
            ("published_at", pa.string()),
            ("headline", pa.string()),
            ("event_type", pa.string()),
            ("sentiment", pa.string()),
            ("confidence", pa.float64()),
            ("importance", pa.int64()),
            ("category", pa.string()),
            ("source", pa.string()),
            ("url", pa.string()),
            ("notion_url", pa.string()),
            ("candidate_date", pa.date32()),
            ("timing_quality", pa.string()),
            ("timing_bucket", pa.string()),
        ]
    )


def _build_timed_events(connection: Any, pa: Any) -> Any:
    exploded = connection.execute(
        """
        SELECT
            page_id || ':' || ticker AS event_key,
            page_id,
            ticker AS symbol,
            event_date,
            published_at,
            headline,
            event_type,
            sentiment,
            confidence,
            importance,
            category,
            source,
            url,
            notion_url
        FROM news_input
        CROSS JOIN unnest(tickers) AS ticker_rows(ticker)
        WHERE ticker IS NOT NULL AND ticker <> ''
        ORDER BY page_id, ticker
        """
    ).to_arrow_table()
    rows: list[dict[str, object]] = []
    for row in exploded.to_pylist():
        candidate, quality, bucket = classify_event_time(
            row["published_at"],
            row["event_date"],
        )
        row["candidate_date"] = candidate
        row["timing_quality"] = quality
        row["timing_bucket"] = bucket
        rows.append(row)
    return pa.Table.from_pylist(rows, schema=_timed_event_schema(pa))


def _write_parquet(path: Path, table: Any, pq: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    pq.write_table(table, temporary, compression="zstd")
    os.replace(temporary, path)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    shutil.copyfile(source, temporary)
    os.replace(temporary, destination)


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _load_analysis_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 2, "daily": {}, "latest": {}, "index_digest": None}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorpusError(f"cannot read analysis state: {type(exc).__name__}") from exc
    if state.get("version") == 1 and isinstance(state.get("files"), dict):
        return {
            "version": 2,
            "daily": {},
            "latest": state["files"],
            "index_digest": None,
        }
    if (
        state.get("version") != 2
        or not isinstance(state.get("daily"), dict)
        or not isinstance(state.get("latest"), dict)
    ):
        raise CorpusError("unsupported analysis state format")
    return state


def _jsonable(value: object) -> object:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _previous_report(analysis_root: Path, report_date: date) -> dict[str, Any] | None:
    candidates: list[tuple[date, Path]] = []
    for path in (analysis_root / "daily").glob("date=*/report.json"):
        try:
            candidate_date = date.fromisoformat(path.parent.name.removeprefix("date="))
        except ValueError:
            continue
        if candidate_date < report_date:
            candidates.append((candidate_date, path))
    if not candidates:
        return None
    _, path = max(candidates)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorpusError(f"cannot read previous analysis report: {type(exc).__name__}") from exc
    return payload if isinstance(payload, dict) else None


def _overall_comparison(
    summary_rows: list[dict[str, object]],
    previous: dict[str, Any] | None,
) -> list[dict[str, object]]:
    if previous is None:
        return []
    previous_rows = {
        (row.get("metric"), row.get("horizon")): row
        for row in previous.get("summary", [])
        if row.get("sample") == "all"
        and row.get("dimension") == "all"
        and row.get("group_value") == "all"
    }
    comparison: list[dict[str, object]] = []
    for row in summary_rows:
        if not (
            row["sample"] == "all"
            and row["dimension"] == "all"
            and row["group_value"] == "all"
        ):
            continue
        previous_row = previous_rows.get((row["metric"], row["horizon"]))
        current_value = row["weighted_mean_return"]
        previous_value = previous_row.get("weighted_mean_return") if previous_row else None
        comparison.append(
            {
                "metric": row["metric"],
                "horizon": row["horizon"],
                "current": current_value,
                "previous": previous_value,
                "delta": (
                    float(current_value) - float(previous_value)
                    if current_value is not None and previous_value is not None
                    else None
                ),
            }
        )
    return comparison


def _report_payload(
    report_date: date,
    metadata: dict[str, object],
    summary_rows: list[dict[str, object]],
    previous: dict[str, Any] | None,
) -> dict[str, object]:
    counts = {
        "news_pages": metadata["news_pages"],
        "ticker_events": metadata["ticker_events"],
        "matched_events": metadata["matched_events"],
        "unmatched_events": metadata["unmatched_events"],
        "date_only_events": metadata["date_only_events"],
        "overlapping_events": metadata["overlapping_events"],
    }
    previous_counts = previous.get("counts", {}) if previous else {}
    return _jsonable(
        {
            "version": 1,
            "report_date": report_date,
            "daily_through": metadata["latest_daily_date"],
            "notion_through": metadata["latest_news_edit"],
            "min_peers": metadata["min_peers"],
            "counts": counts,
            "unmatched_symbols": metadata["unmatched_symbols"],
            "previous_report_date": previous.get("report_date") if previous else None,
            "comparison": {
                "count_deltas": {
                    key: (
                        int(value) - int(previous_counts[key])
                        if key in previous_counts
                        else None
                    )
                    for key, value in counts.items()
                },
                "overall": _overall_comparison(summary_rows, previous),
            },
            # JSON is deliberately complete enough for the API and future
            # historical comparisons, so the web process never imports
            # DuckDB/pyarrow or holds the large event-level Parquet in memory.
            "summary": summary_rows,
        }
    )  # type: ignore[return-value]


def _history_index(analysis_root: Path) -> dict[str, object]:
    reports: list[dict[str, object]] = []
    for path in (analysis_root / "daily").glob("date=*/report.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            report_date = date.fromisoformat(str(payload["report_date"]))
        except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
            continue
        reports.append(
            {
                "report_date": report_date.isoformat(),
                "daily_through": payload.get("daily_through"),
                "notion_through": payload.get("notion_through"),
                "counts": payload.get("counts", {}),
                "previous_report_date": payload.get("previous_report_date"),
            }
        )
    reports.sort(key=lambda item: str(item["report_date"]), reverse=True)
    return {
        "version": 1,
        "latest_report_date": reports[0]["report_date"] if reports else None,
        "reports": reports,
    }


def _percent(value: object) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:.2f}%"


def _number(value: object, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}"


def _markdown_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    escaped = [[cell.replace("|", "\\|") for cell in row] for row in rows]
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in escaped)
    return "\n".join(lines)


def _html_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    head = "".join(f"<th>{html.escape(value)}</th>" for value in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(value)}</td>" for value in row) + "</tr>"
        for row in rows
    )
    return (
        f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def _report_rows(summary_rows: list[dict[str, object]]) -> tuple[list[list[str]], list[list[str]]]:
    overall: list[list[str]] = []
    by_type: list[list[str]] = []
    for row in summary_rows:
        if (
            row["sample"] == "all"
            and row["dimension"] == "all"
            and row["group_value"] == "all"
        ):
            overall.append(
                [
                    str(row["metric"]),
                    f"{row['horizon']}日",
                    str(row["events"]),
                    _number(row["effective_events"]),
                    _percent(row["weighted_mean_return"]),
                    _percent(row["median_return"]),
                    _percent(row["weighted_win_rate"]),
                    f"{_percent(row['ci95_low'])} – {_percent(row['ci95_high'])}",
                ]
            )
        if (
            row["sample"] == "non_overlapping"
            and row["dimension"] == "event_type"
            and row["metric"] == "abnormal_return"
            and row["horizon"] in {0, 5, 20}
        ):
            by_type.append(
                [
                    str(row["group_value"]),
                    f"{row['horizon']}日",
                    str(row["events"]),
                    _percent(row["weighted_mean_return"]),
                    _percent(row["median_return"]),
                    _percent(row["weighted_win_rate"]),
                ]
            )
    return overall, by_type


def _render_reports(
    metadata: dict[str, object],
    summary_rows: list[dict[str, object]],
    report_payload: dict[str, object],
) -> tuple[str, str]:
    overall, by_type = _report_rows(summary_rows)
    overall_headers = [
        "指標",
        "期間",
        "イベント数",
        "実効件数",
        "加重平均",
        "中央値",
        "勝率",
        "95% CI",
    ]
    type_headers = ["イベント種別", "期間", "イベント数", "加重平均", "中央値", "勝率"]
    unmatched = ", ".join(metadata["unmatched_symbols"]) or "なし"  # type: ignore[arg-type]
    latest_daily = html.escape(str(metadata["latest_daily_date"]))
    latest_news = html.escape(str(metadata["latest_news_edit"]))
    report_date = str(report_payload["report_date"])
    previous_date = report_payload["previous_report_date"]
    comparison = report_payload["comparison"]  # type: ignore[assignment]
    count_deltas = comparison["count_deltas"]  # type: ignore[index]
    matched_delta = count_deltas["matched_events"]  # type: ignore[index]
    previous_note = (
        f"前回 `{previous_date}` から日足接続イベント "
        f"{int(matched_delta):+d}件。"
        if previous_date and matched_delta is not None
        else "初回スナップショットのため、前回比較はありません。"
    )
    markdown = f"""# Notionイベント × 株価変動レポート

レポート日: `{report_date}`<br>
データ版: 日足 `{metadata["latest_daily_date"]}` / Notion `{metadata["latest_news_edit"]}`

## カバレッジ

- Notionページ: {metadata["news_pages"]}
- ticker展開後イベント: {metadata["ticker_events"]}
- 日足へ接続できたイベント: {metadata["matched_events"]}
- 未接続イベント: {metadata["unmatched_events"]}（銘柄: {unmatched}）
- 時刻なしイベント: {metadata["date_only_events"]}
- 20取引日窓が重なるイベント: {metadata["overlapping_events"]}
- benchmark最低peer数: {metadata["min_peers"]}

## 前回比較

{previous_note}

このレポートはその日時点の全日足・全Notionコーパスを再計算したスナップショットです。
日付別に保持するため、分析母集団や統計値の変化を後から比較できます。

## 全イベント

{_markdown_table(overall_headers, overall)}

## イベント種別別・重複窓除外

{_markdown_table(type_headers, by_type)}

## 読み方

- `raw_return`: 反応取引日の直前終値を基準にした調整済みリターン。
- `abnormal_return`: raw returnから、対象を除く同subsector銘柄の等ウェイトreturnを差し引いた値。
- 類似記事は削除せず、同一銘柄・反応日・event typeのページ数の逆数で重み付け。
- 日中発表の発表前変動は日足では分離できないため、結果は関連であり因果効果ではない。
"""
    html_report = f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Notionイベント × 株価変動レポート</title>
  <style>
    :root {{ color-scheme: dark; font-family: Inter, system-ui, sans-serif; }}
    body {{ margin: 0; background: #071019; color: #dce7f2; }}
    main {{ max-width: 1120px; margin: auto; padding: 32px 20px 64px; }}
    h1 {{ margin-bottom: 8px; }} h2 {{ margin-top: 36px; }}
    .meta, .note {{ color: #92a9bd; }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(auto-fit,minmax(150px,1fr));
      gap: 12px;
    }}
    .card {{ background: #0e1c28; border: 1px solid #20384a; border-radius: 10px; padding: 14px; }}
    .card strong {{ display: block; font-size: 1.5rem; color: #7dd3fc; margin-top: 4px; }}
    .table-wrap {{ overflow-x: auto; border: 1px solid #20384a; border-radius: 10px; }}
    table {{ width: 100%; border-collapse: collapse; background: #0e1c28; }}
    th, td {{
      padding: 10px 12px;
      text-align: right;
      border-bottom: 1px solid #20384a;
      white-space: nowrap;
    }}
    th:first-child, td:first-child {{ text-align: left; }} th {{ color: #8fd8ff; }}
    code {{ color: #f8c36a; }} li {{ margin: 8px 0; }}
  </style>
</head>
<body><main>
  <h1>Notionイベント × 株価変動</h1>
  <p class="meta">
    レポート {html.escape(report_date)} / 日足 {latest_daily} / Notion {latest_news}
  </p>
  <section class="cards">
    <div class="card">Notionページ<strong>{metadata["news_pages"]}</strong></div>
    <div class="card">tickerイベント<strong>{metadata["ticker_events"]}</strong></div>
    <div class="card">日足接続<strong>{metadata["matched_events"]}</strong></div>
    <div class="card">未接続<strong>{metadata["unmatched_events"]}</strong></div>
    <div class="card">時刻なし<strong>{metadata["date_only_events"]}</strong></div>
    <div class="card">重複窓あり<strong>{metadata["overlapping_events"]}</strong></div>
  </section>
  <h2>前回比較</h2>
  <p>{html.escape(previous_note)}</p>
  <p class="note">
    各日付のレポートは、その日時点の全日足・全Notionコーパスを再計算した
    スナップショットです。
  </p>
  <h2>全イベント</h2>
  {_html_table(overall_headers, overall)}
  <h2>イベント種別別・重複窓除外</h2>
  {_html_table(type_headers, by_type)}
  <h2>読み方</h2>
  <ul>
    <li><code>raw_return</code>は反応取引日の直前終値を基準にした調整済みリターン。</li>
    <li><code>abnormal_return</code>は同subsectorの等ウェイトreturnを差し引いた値。</li>
    <li>類似記事は同一銘柄・反応日・event typeのページ数の逆数で重み付け。</li>
    <li>日足分析なので、日中発表について示せるのは関連であり因果効果ではない。</li>
  </ul>
  <p class="note">
    未接続銘柄: {html.escape(unmatched)}
    / benchmark最低peer数: {metadata["min_peers"]}
  </p>
</main></body></html>
"""
    return markdown, html_report


def _scalar(connection: Any, query: str) -> object:
    return connection.execute(query).fetchone()[0]


def _metadata(connection: Any, min_peers: int) -> dict[str, object]:
    unmatched_symbols = [
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT symbol FROM event_unmatched ORDER BY symbol"
        ).fetchall()
    ]
    return {
        "news_pages": _scalar(connection, "SELECT count(*) FROM news_input"),
        "ticker_events": _scalar(connection, "SELECT count(*) FROM events_timed_input"),
        "matched_events": _scalar(connection, "SELECT count(*) FROM aligned_events"),
        "unmatched_events": _scalar(connection, "SELECT count(*) FROM event_unmatched"),
        "date_only_events": _scalar(
            connection,
            "SELECT count(*) FROM events_timed_input WHERE timing_quality = 'date_only'",
        ),
        "overlapping_events": _scalar(
            connection,
            "SELECT count(*) FROM aligned_decorated WHERE overlap_count > 0",
        ),
        "latest_daily_date": _scalar(connection, "SELECT max(date) FROM daily_indexed"),
        "latest_news_edit": _scalar(connection, "SELECT max(last_edited_at) FROM news_input"),
        "unmatched_symbols": unmatched_symbols,
        "min_peers": min_peers,
    }


def run(
    settings: Settings,
    *,
    uploader: Uploader = aws_upload,
    upload: bool = True,
    now: datetime | None = None,
) -> int:
    duckdb, pa, pq = _analysis_modules()
    local_root = settings.corpus_local_dir
    daily_paths, news_paths, sectors_path = _discover_inputs(local_root)
    run_at = now or datetime.now(tz=UTC)
    if run_at.tzinfo is None:
        run_at = run_at.replace(tzinfo=UTC)
    report_date = run_at.astimezone(JST).date()
    analysis_root = (
        settings.analysis_output_dir.parent
        if settings.analysis_output_dir is not None
        else local_root / "analysis"
    )
    latest_dir = settings.analysis_output_dir or analysis_root / "latest"
    daily_dir = analysis_root / "daily" / f"date={report_date.isoformat()}"
    previous_report = _previous_report(analysis_root, report_date)
    daily_dir.mkdir(parents=True, exist_ok=True)

    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET threads = 1")
        connection.execute("SET memory_limit = '256MB'")
        connection.from_parquet(
            [str(path) for path in daily_paths],
            hive_partitioning=False,
        ).create_view("daily_input")
        connection.from_parquet(
            [str(path) for path in news_paths],
            hive_partitioning=False,
        ).create_view("news_input")
        connection.from_parquet(
            str(sectors_path),
            hive_partitioning=False,
        ).create_view("sectors_input")

        timed_events = _build_timed_events(connection, pa)
        connection.register("events_timed_input", timed_events)
        connection.register(
            "analysis_parameters",
            pa.Table.from_pylist(
                [{"min_peers": settings.analysis_min_peers}],
                schema=pa.schema([("min_peers", pa.int64())]),
            ),
        )
        sql = (
            resources.files("usstocks.corpus")
            .joinpath("sql/event_study.sql")
            .read_text(encoding="utf-8")
        )
        connection.execute(sql)

        event_returns = connection.execute(
            "SELECT * FROM event_returns ORDER BY reaction_date, symbol, page_id"
        ).to_arrow_table()
        event_summary = connection.execute("SELECT * FROM event_summary").to_arrow_table()
        event_unmatched = connection.execute(
            "SELECT * FROM event_unmatched ORDER BY symbol, candidate_date, page_id"
        ).to_arrow_table()
        metadata = _metadata(connection, settings.analysis_min_peers)
    except duckdb.Error as exc:
        raise CorpusError(f"event study query failed: {exc}") from exc
    finally:
        connection.close()

    paths = {name: daily_dir / name for name in OUTPUT_NAMES}
    _write_parquet(paths["event_returns.parquet"], event_returns, pq)
    _write_parquet(paths["event_summary.parquet"], event_summary, pq)
    _write_parquet(paths["event_unmatched.parquet"], event_unmatched, pq)
    summary_rows = event_summary.to_pylist()
    report_payload = _report_payload(
        report_date,
        metadata,
        summary_rows,
        previous_report,
    )
    _write_text(
        paths["report.json"],
        json.dumps(report_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    markdown, html_report = _render_reports(metadata, summary_rows, report_payload)
    _write_text(paths["report.md"], markdown)
    _write_text(paths["report.html"], html_report)

    content_digests = {name: _digest(path) for name, path in paths.items()}
    manifest = {
        "version": 2,
        "report_date": report_date.isoformat(),
        "daily_through": str(metadata["latest_daily_date"]),
        "notion_through": str(metadata["latest_news_edit"]),
        "horizons": list(HORIZONS),
        "min_peers": settings.analysis_min_peers,
        "counts": {
            "news_pages": metadata["news_pages"],
            "ticker_events": metadata["ticker_events"],
            "matched_events": metadata["matched_events"],
            "unmatched_events": metadata["unmatched_events"],
        },
        "files": content_digests,
    }
    manifest_path = daily_dir / "manifest.json"
    _write_text(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    paths["manifest.json"] = manifest_path
    all_digests = {name: _digest(path) for name, path in paths.items()}

    latest_paths = {name: latest_dir / name for name in paths}
    for name, path in paths.items():
        _copy_file(path, latest_paths[name])

    index_path = analysis_root / "index.json"
    _write_text(
        index_path,
        json.dumps(
            _history_index(analysis_root),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    index_digest = _digest(index_path)

    changed: list[str] = []
    state_path = local_root / "analysis-state.json"
    state = _load_analysis_state(state_path)
    if upload:
        s3_root = analysis_s3_root(settings)
        report_key = report_date.isoformat()
        previous_daily = state["daily"].get(report_key, {})
        for name in (*OUTPUT_NAMES, "manifest.json"):
            if previous_daily.get(name) == all_digests[name]:
                continue
            uploader(paths[name], f"{s3_root}/daily/date={report_key}/{name}")
            changed.append(f"daily/{name}")

        previous_latest = state["latest"]
        for name in (*OUTPUT_NAMES, "manifest.json"):
            if previous_latest.get(name) == all_digests[name]:
                continue
            uploader(latest_paths[name], f"{s3_root}/latest/{name}")
            changed.append(f"latest/{name}")

        if state.get("index_digest") != index_digest:
            uploader(index_path, f"{s3_root}/index.json")
            changed.append("index.json")

        state["daily"][report_key] = all_digests
        state.update(
            {
                "latest": all_digests,
                "index_digest": index_digest,
                "last_success_utc": run_at.isoformat(),
                "matched_events": metadata["matched_events"],
                "unmatched_events": metadata["unmatched_events"],
            }
        )
        save_state(state_path, state)

    log.info(
        "event study complete: report=%s, %s/%s ticker event(s) matched, "
        "%d file(s) uploaded",
        report_date,
        metadata["matched_events"],
        metadata["ticker_events"],
        len(changed),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="write local outputs without uploading to S3",
    )
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    settings = get_settings()
    if args.output_dir is not None:
        settings = settings.model_copy(update={"analysis_output_dir": args.output_dir})
    configure_logging(settings.log_level)
    try:
        return run(settings, upload=not args.local_only)
    except CorpusError as exc:
        log.error("%s", exc)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
