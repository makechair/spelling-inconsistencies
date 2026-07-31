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
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from importlib import resources
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..config import Settings, get_settings
from ..logging_setup import configure_logging
from .daily import CorpusError, Uploader, aws_upload, corpus_s3_root, save_state

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
HORIZONS = (0, 1, 2, 5, 20)
OUTPUT_NAMES = (
    "event_returns.parquet",
    "event_summary.parquet",
    "event_unmatched.parquet",
    "report.md",
    "report.html",
)


def analysis_s3_root(settings: Settings) -> str:
    explicit = (settings.analysis_s3_uri or "").strip().rstrip("/")
    return explicit or f"{corpus_s3_root(settings)}/analysis/latest"


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


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _load_analysis_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "files": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorpusError(f"cannot read analysis state: {type(exc).__name__}") from exc
    if state.get("version") != 1 or not isinstance(state.get("files"), dict):
        raise CorpusError("unsupported analysis state format")
    return state


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
    markdown = f"""# Notionイベント × 株価変動レポート

データ版: 日足 `{metadata["latest_daily_date"]}` / Notion `{metadata["latest_news_edit"]}`

## カバレッジ

- Notionページ: {metadata["news_pages"]}
- ticker展開後イベント: {metadata["ticker_events"]}
- 日足へ接続できたイベント: {metadata["matched_events"]}
- 未接続イベント: {metadata["unmatched_events"]}（銘柄: {unmatched}）
- 時刻なしイベント: {metadata["date_only_events"]}
- 20取引日窓が重なるイベント: {metadata["overlapping_events"]}
- benchmark最低peer数: {metadata["min_peers"]}

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
  <p class="meta">日足 {latest_daily} / Notion {latest_news}</p>
  <section class="cards">
    <div class="card">Notionページ<strong>{metadata["news_pages"]}</strong></div>
    <div class="card">tickerイベント<strong>{metadata["ticker_events"]}</strong></div>
    <div class="card">日足接続<strong>{metadata["matched_events"]}</strong></div>
    <div class="card">未接続<strong>{metadata["unmatched_events"]}</strong></div>
    <div class="card">時刻なし<strong>{metadata["date_only_events"]}</strong></div>
    <div class="card">重複窓あり<strong>{metadata["overlapping_events"]}</strong></div>
  </section>
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
    output_dir = settings.analysis_output_dir or local_root / "analysis" / "latest"
    output_dir.mkdir(parents=True, exist_ok=True)

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

    paths = {name: output_dir / name for name in OUTPUT_NAMES}
    _write_parquet(paths["event_returns.parquet"], event_returns, pq)
    _write_parquet(paths["event_summary.parquet"], event_summary, pq)
    _write_parquet(paths["event_unmatched.parquet"], event_unmatched, pq)
    markdown, html_report = _render_reports(metadata, event_summary.to_pylist())
    _write_text(paths["report.md"], markdown)
    _write_text(paths["report.html"], html_report)

    content_digests = {name: _digest(path) for name, path in paths.items()}
    manifest = {
        "version": 1,
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
    manifest_path = output_dir / "manifest.json"
    _write_text(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    paths["manifest.json"] = manifest_path
    all_digests = {name: _digest(path) for name, path in paths.items()}

    changed: list[str] = []
    state_path = local_root / "analysis-state.json"
    state = _load_analysis_state(state_path)
    previous = state["files"]
    if upload:
        s3_root = analysis_s3_root(settings)
        for name in (*OUTPUT_NAMES, "manifest.json"):
            if previous.get(name) == all_digests[name]:
                continue
            uploader(paths[name], f"{s3_root}/{name}")
            changed.append(name)
        state.update(
            {
                "files": all_digests,
                "last_success_utc": (now or datetime.now(tz=UTC)).isoformat(),
                "matched_events": metadata["matched_events"],
                "unmatched_events": metadata["unmatched_events"],
            }
        )
        save_state(state_path, state)

    log.info(
        "event study complete: %s/%s ticker event(s) matched, %d file(s) uploaded",
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
