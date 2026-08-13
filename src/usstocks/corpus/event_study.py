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
import re
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
    "symbol_news_coverage.parquet",
    "symbol_risk_profile.parquet",
    "report.json",
    "report.md",
    "report.html",
)
EVENT_TICKER_ALIASES = {
    "MU": (
        (re.compile(r"(?<![A-Z0-9])MU(?![A-Z0-9])", re.IGNORECASE), "MU"),
        (
            re.compile(
                r"(?<![A-Z0-9])Micron(?: Technology)?(?![A-Z0-9])",
                re.IGNORECASE,
            ),
            "Micron",
        ),
        (re.compile("マイクロン"), "マイクロン"),
    ),
    "WDC": (
        (re.compile(r"(?<![A-Z0-9])WDC(?![A-Z0-9])", re.IGNORECASE), "WDC"),
        (
            re.compile(
                r"(?<![A-Z0-9])Western Digital(?![A-Z0-9])", re.IGNORECASE
            ),
            "Western Digital",
        ),
        (re.compile("ウエスタンデジタル"), "ウエスタンデジタル"),
    ),
    "STX": (
        (re.compile(r"(?<![A-Z0-9])STX(?![A-Z0-9])", re.IGNORECASE), "STX"),
        (
            re.compile(r"(?<![A-Z0-9])Seagate(?![A-Z0-9])", re.IGNORECASE),
            "Seagate",
        ),
        (re.compile("シーゲイト"), "シーゲイト"),
    ),
}


def analysis_s3_root(settings: Settings) -> str:
    explicit = (settings.analysis_s3_uri or "").strip().rstrip("/")
    # Before daily archives existed this setting was documented as the latest
    # directory itself. Accept that old form during the transition.
    if explicit.endswith("/latest"):
        explicit = explicit.removesuffix("/latest")
    return explicit or f"{corpus_s3_root(settings)}/analysis"


def analysis_exchange_s3_root(settings: Settings) -> str:
    explicit = (settings.analysis_exchange_s3_uri or "").strip().rstrip("/")
    if explicit:
        return explicit
    backup = (settings.backup_s3_uri or "").strip().rstrip("/")
    if not backup:
        raise CorpusError(
            "set USSTOCKS_ANALYSIS_EXCHANGE_S3_URI or USSTOCKS_BACKUP_S3_URI"
        )
    return f"{backup}/analysis-exchange"


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
            ("ticker_origin", pa.string()),
            ("ticker_evidence", pa.string()),
            ("event_date", pa.date32()),
            ("published_at", pa.string()),
            ("headline", pa.string()),
            ("summary_ja", pa.string()),
            ("my_take", pa.string()),
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


def _event_symbols(
    explicit_symbols: Sequence[str],
    text: str,
) -> list[tuple[str, str, str]]:
    """Return symbol, origin and evidence without changing the source news row."""

    resolved: dict[str, tuple[str, str]] = {}
    for symbol in explicit_symbols:
        normalized = symbol.strip().upper()
        if normalized:
            resolved[normalized] = ("explicit", "notion_ticker")
    for symbol, aliases in EVENT_TICKER_ALIASES.items():
        if symbol in resolved:
            continue
        for pattern, label in aliases:
            if pattern.search(text):
                resolved[symbol] = ("inferred_alias", label)
                break
    return [
        (symbol, origin, evidence)
        for symbol, (origin, evidence) in resolved.items()
    ]


def _build_timed_events(connection: Any, pa: Any) -> Any:
    news_rows = connection.execute(
        """
        SELECT
            page_id,
            event_date,
            published_at,
            headline,
            summary_ja,
            my_take,
            tickers,
            event_type,
            sentiment,
            confidence,
            importance,
            category,
            source,
            url,
            notion_url
        FROM news_input
        ORDER BY page_id
        """
    ).to_arrow_table()
    rows: list[dict[str, object]] = []
    for news_row in news_rows.to_pylist():
        candidate, quality, bucket = classify_event_time(
            news_row["published_at"],
            news_row["event_date"],
        )
        article_text = " ".join(
            str(news_row.get(field) or "")
            for field in ("headline", "summary_ja", "my_take")
        )
        for symbol, origin, evidence in _event_symbols(
            news_row.get("tickers") or [], article_text
        ):
            row = {
                key: value
                for key, value in news_row.items()
                if key != "tickers"
            }
            row["event_key"] = f"{news_row['page_id']}:{symbol}"
            row["symbol"] = symbol
            row["ticker_origin"] = origin
            row["ticker_evidence"] = evidence
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


def _case_studies(
    event_rows: list[dict[str, object]],
    context_rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    contexts = {row["event_key"]: row for row in context_rows}
    studies: list[dict[str, object]] = []
    for event in event_rows:
        study = dict(event)
        study["historical_move_context"] = contexts.get(event["event_key"])
        studies.append(study)
    return studies


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _symbol_focus(
    case_studies: list[dict[str, object]],
    ticker_inventory: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    """Aggregate matched Notion pages within each ticker.

    The event-study summary deliberately remains the cross-sectional research
    table. This smaller view answers a different question: which ticker has the
    richest Notion history, and what do its observed event windows look like?
    """

    grouped: dict[str, list[dict[str, object]]] = {}
    for study in case_studies:
        grouped.setdefault(str(study["symbol"]), []).append(study)

    inventory = {
        str(item["symbol"]): item for item in (ticker_inventory or [])
    }
    symbols = set(grouped) | set(inventory)

    focus: list[dict[str, object]] = []
    for symbol in sorted(
        symbols,
        key=lambda value: (
            -int(inventory.get(value, {}).get("article_events") or len(grouped.get(value, []))),
            value,
        ),
    ):
        studies = grouped.get(symbol, [])
        inventory_entry = inventory.get(symbol, {})
        reaction_dates = sorted({str(study["reaction_date"]) for study in studies})
        event_types: dict[str, int] = {}
        categories: dict[str, int] = {}
        sources: dict[str, int] = {}
        ticker_origins: dict[str, int] = {}
        for study in studies:
            event_type = str(study.get("event_type") or "unknown")
            event_types[event_type] = event_types.get(event_type, 0) + 1
            category = str(study.get("category") or "unknown")
            categories[category] = categories.get(category, 0) + 1
            source = str(study.get("source") or "unknown")
            sources[source] = sources.get(source, 0) + 1
            origin = str(study.get("ticker_origin") or "unknown")
            ticker_origins[origin] = ticker_origins.get(origin, 0) + 1

        horizon_rows: list[dict[str, object]] = []
        for horizon in HORIZONS:
            field = f"raw_return_{horizon}d"
            observed = [study for study in studies if study.get(field) is not None]
            effective = sum(float(study.get("event_weight") or 1) for study in observed)
            weighted_total = sum(
                float(study.get("event_weight") or 1) * float(study[field])
                for study in observed
            )
            wins = sum(
                float(study.get("event_weight") or 1)
                for study in observed
                if float(study[field]) > 0
            )
            peer_field = f"exploratory_relative_return_{horizon}d"
            peer_observed = [
                study for study in observed if study.get(peer_field) is not None
            ]
            peer_effective = sum(
                float(study.get("event_weight") or 1) for study in peer_observed
            )
            peer_total = sum(
                float(study.get("event_weight") or 1) * float(study[peer_field])
                for study in peer_observed
            )
            date_values = {
                str(study["reaction_date"]): float(study[field]) for study in observed
            }
            horizon_rows.append(
                {
                    "horizon": horizon,
                    "events": len(observed),
                    "reaction_date_events": len(date_values),
                    "effective_events": effective,
                    "weighted_mean_return": (
                        weighted_total / effective if effective else None
                    ),
                    # Multiple articles can describe the same market session.
                    # A date-level median prevents article volume from changing
                    # the centre of the observed price distribution.
                    "median_return": _median(list(date_values.values())),
                    "weighted_win_rate": wins / effective if effective else None,
                    "peer_events": len(peer_observed),
                    "peer_effective_events": peer_effective,
                    "weighted_mean_peer_relative_return": (
                        peer_total / peer_effective if peer_effective else None
                    ),
                }
            )

        focus.append(
            {
                "symbol": symbol,
                "notion_article_events": int(
                    inventory_entry.get("article_events") or len(studies)
                ),
                "matched_events": len(studies),
                "unmatched_events": max(
                    int(inventory_entry.get("article_events") or len(studies))
                    - len(studies),
                    0,
                ),
                "article_events": len(studies),
                "effective_events": sum(
                    float(study.get("event_weight") or 1) for study in studies
                ),
                "reaction_dates": reaction_dates,
                "reaction_date_count": len(reaction_dates),
                "event_types": dict(sorted(event_types.items())),
                "categories": dict(sorted(categories.items())),
                "sources": dict(sorted(sources.items())),
                "ticker_origins": dict(sorted(ticker_origins.items())),
                "ticker_origins_inventory": inventory_entry.get(
                    "ticker_origins", dict(sorted(ticker_origins.items()))
                ),
                "horizons": horizon_rows,
            }
        )
    return focus


def _finding(
    title: str,
    body: str,
    *,
    level: str = "observation",
) -> dict[str, str]:
    return {"title": title, "body": body, "level": level}


def _focus_findings(
    metadata: dict[str, object],
    case_studies: list[dict[str, object]],
) -> list[dict[str, str]]:
    ticker_inventory = metadata.get("ticker_inventory")
    inventory = ticker_inventory if isinstance(ticker_inventory, list) else []
    focuses = _symbol_focus(case_studies, inventory)
    if not focuses:
        return []
    focus = focuses[0]
    symbol = str(focus["symbol"])
    origins = focus.get("ticker_origins_inventory", {})
    origin_label = " / ".join(
        f"{origin} {count}件"
        for origin, count in origins.items()  # type: ignore[union-attr]
    )
    findings = [
        _finding(
            f"{symbol}: ニュース資産と価格接続",
            f"Notion記事イベント{focus['notion_article_events']}件のうち"
            f"{focus['matched_events']}件を日足へ接続しました。反応取引日は"
            f"{focus['reaction_date_count']}日、ticker根拠は{origin_label or '未分類'}です。"
            "記事本文の量と独立した価格観測数を分けて読んでください。",
            level="context",
        )
    ]
    horizon_map = {
        int(row["horizon"]): row
        for row in focus.get("horizons", [])  # type: ignore[union-attr]
    }
    for horizon in (0, 2, 5):
        row = horizon_map.get(horizon)
        if not row or row.get("weighted_mean_return") is None:
            continue
        label = "反応日" if horizon == 0 else f"反応日から+{horizon}日"
        findings.append(
            _finding(
                f"{symbol}: {label}の反応日別集計",
                f"観測できた反応日は{row['reaction_date_events']}日。記事重複を"
                f"1/N補正した平均は{_percent(row['weighted_mean_return'])}、"
                f"反応日単位の中央値は{_percent(row['median_return'])}、"
                f"上昇率は{_percent(row['weighted_win_rate'])}です。",
            )
        )

    by_date: dict[str, dict[str, object]] = {}
    for study in case_studies:
        if study["symbol"] == symbol and study.get("raw_return_0d") is not None:
            by_date.setdefault(str(study["reaction_date"]), study)
    if by_date:
        low_date, low = min(
            by_date.items(), key=lambda item: float(item[1]["raw_return_0d"])
        )
        high_date, high = max(
            by_date.items(), key=lambda item: float(item[1]["raw_return_0d"])
        )
        findings.append(
            _finding(
                f"{symbol}: 反応日の振れ幅",
                f"最小は{low_date}の{_percent(low['raw_return_0d'])}"
                f"（過去累積分位{_percent(low.get('historical_percentile_0d'))}、"
                f"出来高比{_number(low.get('reaction_volume_ratio_60d'), 2)}倍）、"
                f"最大は{high_date}の{_percent(high['raw_return_0d'])}"
                f"（過去累積分位{_percent(high.get('historical_percentile_0d'))}、"
                f"出来高比{_number(high.get('reaction_volume_ratio_60d'), 2)}倍）です。"
                "平均だけでは相殺される両方向の大変動があります。",
                level="context",
            )
        )
    overlapping = sum(
        1
        for study in case_studies
        if study["symbol"] == symbol and int(study.get("overlap_count") or 0) > 0
    )
    if overlapping:
        findings.append(
            _finding(
                f"{symbol}: イベント窓の重複",
                f"接続{focus['matched_events']}件中{overlapping}件は別記事の20取引日窓と"
                "重なります。複数記事が同じテーマと相場局面を記述しているため、"
                "各記事を独立した因果イベントとして数えません。",
                level="warning",
            )
        )
    return findings


def _analysis_findings(
    metadata: dict[str, object],
    case_studies: list[dict[str, object]],
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    matched = int(metadata["matched_events"])
    ticker_events = int(metadata["ticker_events"])
    if matched < 5:
        findings.append(
            _finding(
                "結論の強さ",
                f"価格反応まで接続できたのは{ticker_events}件中{matched}件です。"
                "現段階はイベント種別の平均効果ではなく、個別ケースの記述分析として"
                "読んでください。",
                level="warning",
            )
        )

    findings.extend(_focus_findings(metadata, case_studies))

    for study in case_studies[:5]:
        symbol = str(study["symbol"])
        headline = str(study.get("headline") or "見出しなし")
        available_horizons = [
            horizon
            for horizon in reversed(HORIZONS)
            if study.get(f"raw_return_{horizon}d") is not None
        ]
        if not available_horizons:
            continue
        horizon = available_horizons[0]
        raw_return = float(study[f"raw_return_{horizon}d"])
        percentile = study.get(f"historical_percentile_{horizon}d")
        observations = int(study.get(f"historical_observations_{horizon}d") or 0)
        rarity = ""
        if percentile is not None and observations >= 252:
            tail = float(percentile) if raw_return < 0 else 1 - float(percentile)
            side = "下位" if raw_return < 0 else "上位"
            rarity = (
                f"、過去{observations:,}観測の同銘柄分布では"
                f"{side}{tail * 100:.1f}%"
            )
        elif observations:
            rarity = f"。過去分布は{observations}観測しかなく、希少性は未判定"
        findings.append(
            _finding(
                f"{symbol}: {horizon + 1}取引日累計",
                f"「{headline}」の反応日から{horizon}日後までの調整済みリターンは"
                f"{raw_return * 100:+.2f}%{rarity}です。",
            )
        )

        peers = int(study.get(f"peer_count_{horizon}d") or 0)
        exploratory = study.get(f"exploratory_relative_return_{horizon}d")
        if peers and exploratory is not None:
            threshold = int(metadata["min_peers"])
            qualifier = (
                "正式benchmark"
                if peers >= threshold
                else f"参考値（{peers}社、基準{threshold}社未満）"
            )
            findings.append(
                _finding(
                    f"{symbol}: 同業平均との差",
                    (
                        "反応日は"
                        f"{float(study['exploratory_relative_return_0d']) * 100:+.2f}ポイント、"
                        if study.get("exploratory_relative_return_0d") is not None
                        and horizon != 0
                        else ""
                    )
                    + f"{horizon}日後は{float(exploratory) * 100:+.2f}ポイント。"
                    f"これは{qualifier}です。",
                    level="context",
                )
            )

        volume_ratio = study.get("reaction_volume_ratio_60d")
        if volume_ratio is not None:
            pre_20d = study.get("pre_event_return_20d")
            momentum = (
                f"イベント直前20取引日は{float(pre_20d) * 100:+.2f}%、"
                if pre_20d is not None
                else ""
            )
            findings.append(
                _finding(
                    f"{symbol}: 事前トレンドと出来高",
                    momentum + "反応日の調整済み出来高は直前60取引日の中央値の"
                    f"{float(volume_ratio):.2f}倍でした。価格変動と売買参加の強さを"
                    "分けて評価できます。",
                    level="context",
                )
            )

        context = study.get("historical_move_context")
        if isinstance(context, dict) and int(context.get("similar_move_count") or 0) >= 20:
            count = int(context["similar_move_count"])
            mean_5d = context.get("forward_mean_5d")
            win_5d = context.get("forward_win_rate_5d")
            if mean_5d is not None and win_5d is not None:
                move_label = (
                    "下落" if context.get("move_direction") == "down" else "上昇"
                )
                findings.append(
                    _finding(
                        f"{symbol}: 同規模変動後の履歴",
                        f"反応日と同等以上の{move_label}日は過去に{count}回あり、"
                        "その後5取引日の"
                        f"平均は{float(mean_5d) * 100:+.2f}%、上昇率は"
                        f"{float(win_5d) * 100:.1f}%でした。ニュース効果ではなく、"
                        "値動きだけを条件にした参考統計です。",
                        level="context",
                    )
                )
    return findings


def _report_payload(
    report_date: date,
    metadata: dict[str, object],
    summary_rows: list[dict[str, object]],
    event_rows: list[dict[str, object]],
    context_rows: list[dict[str, object]],
    return_surface_rows: list[dict[str, object]],
    return_trade_plan_rows: list[dict[str, object]],
    smoothed_surface_rows: list[dict[str, object]],
    smoothed_trade_plan_rows: list[dict[str, object]],
    validation_result_rows: list[dict[str, object]],
    validation_example_rows: list[dict[str, object]],
    coverage_rows: list[dict[str, object]],
    risk_rows: list[dict[str, object]],
    correlation_rows: list[dict[str, object]],
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
    case_studies = _case_studies(event_rows, context_rows)
    ticker_inventory = metadata["ticker_inventory"]
    assert isinstance(ticker_inventory, list)
    symbol_focus = _symbol_focus(case_studies, ticker_inventory)
    return_surfaces: dict[str, list[dict[str, object]]] = {}
    for row in return_surface_rows:
        return_surfaces.setdefault(str(row["symbol"]), []).append(row)
    return_trade_plans: dict[str, list[dict[str, object]]] = {}
    for row in return_trade_plan_rows:
        return_trade_plans.setdefault(str(row["symbol"]), []).append(row)
    smoothed_return_surfaces: dict[str, list[dict[str, object]]] = {}
    for row in smoothed_surface_rows:
        smoothed_return_surfaces.setdefault(str(row["symbol"]), []).append(row)
    smoothed_trade_plans: dict[str, list[dict[str, object]]] = {}
    for row in smoothed_trade_plan_rows:
        smoothed_trade_plans.setdefault(str(row["symbol"]), []).append(row)
    walk_forward_simulations: dict[str, list[dict[str, object]]] = {}
    for row in validation_result_rows:
        walk_forward_simulations.setdefault(str(row["symbol"]), []).append(row)
    walk_forward_examples: dict[str, list[dict[str, object]]] = {}
    for row in validation_example_rows:
        walk_forward_examples.setdefault(str(row["symbol"]), []).append(row)
    return _jsonable(
        {
            "version": 2,
            "report_date": report_date,
            "daily_through": metadata["latest_daily_date"],
            "notion_through": metadata["latest_news_edit"],
            "min_peers": metadata["min_peers"],
            "counts": counts,
            "unmatched_symbols": metadata["unmatched_symbols"],
            # Per symbol, so that "few articles" and "articles that never
            # reached a price series" stop looking like the same shortfall.
            "symbol_news_coverage": coverage_rows,
            # What each symbol does when nothing in particular is happening.
            # Every other figure on the report is conditional, and a
            # conditional number cannot be read without this one.
            "symbol_risk_profile": risk_rows,
            "symbol_correlations": correlation_rows,
            "ticker_inventory": ticker_inventory,
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
            "findings": _analysis_findings(metadata, case_studies),
            "focus_symbol": symbol_focus[0]["symbol"] if symbol_focus else None,
            "symbol_focus": symbol_focus,
            "return_surfaces": return_surfaces,
            "return_trade_plans": return_trade_plans,
            "smoothed_return_surfaces": smoothed_return_surfaces,
            "smoothed_trade_plans": smoothed_trade_plans,
            "walk_forward_simulations": walk_forward_simulations,
            "walk_forward_examples": walk_forward_examples,
            "case_studies": case_studies,
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


def _case_report_rows(case_studies: list[dict[str, object]]) -> list[list[str]]:
    rows: list[list[str]] = []
    for study in case_studies:
        horizons = [
            horizon
            for horizon in reversed(HORIZONS)
            if study.get(f"raw_return_{horizon}d") is not None
        ]
        if not horizons:
            continue
        horizon = horizons[0]
        raw_return = study[f"raw_return_{horizon}d"]
        percentile = study.get(f"historical_percentile_{horizon}d")
        observations = int(study.get(f"historical_observations_{horizon}d") or 0)
        rarity = "—"
        if percentile is not None and observations >= 252:
            tail = float(percentile) if float(raw_return) < 0 else 1 - float(percentile)
            rarity = f"{'下位' if float(raw_return) < 0 else '上位'}{tail * 100:.1f}%"
        elif observations:
            rarity = f"不足（{observations}観測）"
        relative = study.get(f"exploratory_relative_return_{horizon}d")
        peer_count = int(study.get(f"peer_count_{horizon}d") or 0)
        context = study.get("historical_move_context")
        historical_forward = "—"
        if (
            isinstance(context, dict)
            and int(context.get("similar_move_count") or 0) >= 20
            and context.get("forward_mean_5d") is not None
        ):
            historical_forward = (
                f"{_percent(context['forward_mean_5d'])} / "
                f"勝率{_percent(context['forward_win_rate_5d'])}"
            )
        rows.append(
            [
                str(study["reaction_date"]),
                str(study["symbol"]),
                str(study.get("headline") or "見出しなし"),
                _percent(study.get("pre_event_return_20d")),
                f"{horizon}日 {_percent(raw_return)}",
                rarity,
                f"{_percent(relative)} / {peer_count}社" if relative is not None else "—",
                (
                    f"{_number(study['reaction_volume_ratio_60d'], 2)}倍"
                    if study.get("reaction_volume_ratio_60d") is not None
                    else "—"
                ),
                historical_forward,
            ]
        )
    return rows


def _focus_report_rows(
    symbol_focus: list[dict[str, object]],
) -> tuple[list[list[str]], list[list[str]], str | None]:
    overview: list[list[str]] = []
    for focus in symbol_focus:
        horizons = {
            int(row["horizon"]): row
            for row in focus.get("horizons", [])  # type: ignore[union-attr]
        }
        event_types = " / ".join(
            f"{event_type} {count}"
            for event_type, count in focus.get("event_types", {}).items()  # type: ignore[union-attr]
        )
        ticker_origins = " / ".join(
            f"{origin} {count}"
            for origin, count in focus.get("ticker_origins_inventory", {}).items()  # type: ignore[union-attr]
        )
        overview.append(
            [
                str(focus["symbol"]),
                str(focus["notion_article_events"]),
                str(focus["matched_events"]),
                _number(focus["effective_events"]),
                str(focus["reaction_date_count"]),
                ticker_origins or "—",
                event_types or "—",
                _percent(horizons.get(0, {}).get("weighted_mean_return")),
                _percent(horizons.get(2, {}).get("weighted_mean_return")),
                _percent(horizons.get(20, {}).get("weighted_mean_return")),
            ]
        )

    if not symbol_focus:
        return overview, [], None
    primary = symbol_focus[0]
    primary_rows = [
        [
            (
                "反応日（1取引日累計）"
                if int(row["horizon"]) == 0
                else f"+{row['horizon']}日（{int(row['horizon']) + 1}取引日累計）"
            ),
            str(row["events"]),
            str(row["reaction_date_events"]),
            _number(row["effective_events"]),
            _percent(row["weighted_mean_return"]),
            _percent(row["median_return"]),
            _percent(row["weighted_win_rate"]),
            _percent(row["weighted_mean_peer_relative_return"]),
        ]
        for row in primary.get("horizons", [])  # type: ignore[union-attr]
    ]
    return overview, primary_rows, str(primary["symbol"])


def _case_rarity(study: dict[str, object], horizon: int) -> str:
    value = study.get(f"raw_return_{horizon}d")
    percentile = study.get(f"historical_percentile_{horizon}d")
    observations = int(study.get(f"historical_observations_{horizon}d") or 0)
    if value is None or percentile is None:
        return "—"
    if observations < 252:
        return f"不足（{observations}観測）"
    tail = float(percentile) if float(value) < 0 else 1 - float(percentile)
    return f"{'下位' if float(value) < 0 else '上位'}{tail * 100:.1f}%"


def _case_detail_reports(
    case_studies: list[dict[str, object]], min_peers: int
) -> tuple[str, str]:
    markdown_sections: list[str] = []
    html_sections: list[str] = []
    horizon_headers = [
        "期間",
        "観測リターン",
        "過去分布での位置",
        "累積分位",
        "過去観測数",
        "peer数",
        "peer平均",
        "peerとの差",
        "正式abnormal",
    ]
    for study in case_studies:
        symbol = str(study["symbol"])
        reaction_date = str(study["reaction_date"])
        context = study.get("historical_move_context")
        metric_rows = [
            ["イベント日", str(study.get("event_date") or "—")],
            ["反応候補日", str(study.get("candidate_date") or "—")],
            ["反応取引日", reaction_date],
            [
                "ticker根拠",
                f"{study.get('ticker_origin') or 'unknown'} / "
                f"{study.get('ticker_evidence') or '—'}",
            ],
            ["カテゴリ", str(study.get("category") or "unknown")],
            ["出典", str(study.get("source") or "—")],
            [
                "時刻品質",
                f"{study.get('timing_quality') or '—'} / "
                f"{study.get('timing_bucket') or '—'}",
            ],
            ["センチメント", str(study.get("sentiment") or "—")],
            ["分類信頼度", _number(study.get("confidence"), 2)],
            ["重要度", _number(study.get("importance"), 0)],
            ["同日同種記事数", _number(study.get("event_group_size"), 0)],
            ["イベント重み", _number(study.get("event_weight"), 3)],
            ["20日窓の重複数", _number(study.get("overlap_count"), 0)],
            ["直前5取引日", _percent(study.get("pre_event_return_5d"))],
            ["直前20取引日", _percent(study.get("pre_event_return_20d"))],
            [
                "反応日出来高 / 過去60日中央値",
                (
                    f"{_number(study.get('reaction_volume_ratio_60d'), 2)}倍"
                    if study.get("reaction_volume_ratio_60d") is not None
                    else "—"
                ),
            ],
        ]
        if isinstance(context, dict):
            direction = "下落" if context.get("move_direction") == "down" else "上昇"
            metric_rows.append(
                [
                    "同規模の過去変動",
                    f"{_number(context.get('similar_move_count'), 0)}件（{direction}）",
                ]
            )

        horizon_rows: list[list[str]] = []
        for horizon in HORIZONS:
            peer_count = int(study.get(f"peer_count_{horizon}d") or 0)
            abnormal = study.get(f"abnormal_return_{horizon}d")
            abnormal_label = _percent(abnormal)
            if abnormal is None and peer_count:
                abnormal_label = f"—（{min_peers}社未満）"
            horizon_rows.append(
                [
                    (
                        "反応日（1取引日累計）"
                        if horizon == 0
                        else f"+{horizon}日（{horizon + 1}取引日累計）"
                    ),
                    _percent(study.get(f"raw_return_{horizon}d")),
                    _case_rarity(study, horizon),
                    _percent(study.get(f"historical_percentile_{horizon}d")),
                    _number(study.get(f"historical_observations_{horizon}d"), 0),
                    str(peer_count),
                    _percent(study.get(f"exploratory_benchmark_return_{horizon}d")),
                    _percent(study.get(f"exploratory_relative_return_{horizon}d")),
                    abnormal_label,
                ]
            )

        markdown = [
            f"### {symbol} · {reaction_date}",
            str(study.get("headline") or "見出しなし"),
            f"**事実要約:** {study.get('summary_ja') or '—'}",
            f"**収集時の見立て:** {study.get('my_take') or '—'}",
            _markdown_table(["項目", "値"], metric_rows),
            _markdown_table(horizon_headers, horizon_rows),
        ]
        html_parts = [
            f"<section class=\"case-detail\"><h3>{html.escape(symbol)} · "
            f"{html.escape(reaction_date)}</h3>",
            f"<p class=\"note\">{html.escape(str(study.get('headline') or '見出しなし'))}</p>",
            f"<p>{html.escape(str(study.get('summary_ja') or '—'))}</p>",
            "<p class=\"note\">収集時の見立て: "
            f"{html.escape(str(study.get('my_take') or '—'))}</p>",
            _html_table(["項目", "値"], metric_rows),
            _html_table(horizon_headers, horizon_rows),
        ]
        if isinstance(context, dict):
            forward_rows = [
                [
                    f"{days}取引日後",
                    _percent(context.get(f"forward_mean_{days}d")),
                    _percent(context.get(f"forward_median_{days}d")),
                    _percent(context.get(f"forward_win_rate_{days}d")),
                ]
                for days in (1, 5, 20)
            ]
            label = f"同程度以上の過去変動後（{context.get('similar_move_count')}件）"
            markdown.extend(
                [
                    f"#### {label}",
                    _markdown_table(
                        ["期間", "平均", "中央値", "上昇率"], forward_rows
                    ),
                ]
            )
            html_parts.extend(
                [
                    f"<h4>{html.escape(label)}</h4>",
                    _html_table(["期間", "平均", "中央値", "上昇率"], forward_rows),
                ]
            )
        html_parts.append("</section>")
        markdown_sections.append("\n\n".join(markdown))
        html_sections.append("".join(html_parts))
    return "\n\n".join(markdown_sections), "".join(html_sections)


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
    case_headers = [
        "反応日",
        "銘柄",
        "イベント",
        "直前20日",
        "観測済み反応",
        "過去分布",
        "同業差",
        "出来高",
        "同規模変動後5日",
    ]
    findings = report_payload.get("findings", [])
    symbol_focus = report_payload.get("symbol_focus", [])  # type: ignore[assignment]
    focus_overview, focus_primary, primary_symbol = _focus_report_rows(
        symbol_focus  # type: ignore[arg-type]
    )
    focus_overview_headers = [
        "銘柄",
        "Notion記事",
        "日足接続",
        "実効件数",
        "反応取引日",
        "ticker根拠",
        "イベント種別",
        "反応日平均",
        "+2日平均",
        "+20日平均",
    ]
    focus_primary_headers = [
        "期間",
        "記事観測",
        "反応日数",
        "実効件数",
        "加重平均",
        "反応日中央値",
        "上昇率",
        "peer差平均",
    ]
    case_studies = report_payload.get("case_studies", [])  # type: ignore[assignment]
    case_rows = _case_report_rows(case_studies)  # type: ignore[arg-type]
    case_details_markdown, case_details_html = _case_detail_reports(
        case_studies, int(metadata["min_peers"])  # type: ignore[arg-type]
    )
    findings_markdown = "\n".join(
        f"- **{finding['title']}**: {finding['body']}"  # type: ignore[index]
        for finding in findings  # type: ignore[union-attr]
    )
    findings_html = "".join(
        f"<li><strong>{html.escape(str(finding['title']))}</strong>: "
        f"{html.escape(str(finding['body']))}</li>"
        for finding in findings  # type: ignore[union-attr]
    )
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

## 実データからの所見

{findings_markdown}

## 銘柄フォーカス

接続できたNotion記事イベント数が多い順です。最多の `{primary_symbol or "—"}` を
この版の主対象とし、同一銘柄内のケース集積として期間別に集計します。

{_markdown_table(focus_overview_headers, focus_overview)}

### {primary_symbol or "対象なし"} の期間別集計

{_markdown_table(focus_primary_headers, focus_primary)}

## 個別ケース分析

{_markdown_table(case_headers, case_rows)}

### 算出値の全期間明細

推論文と切り離して検証できるよう、算出可能な決定論的指標を全て掲載します。

{case_details_markdown}

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
  <h2>実データからの所見</h2>
  <ul>{findings_html}</ul>
  <h2>銘柄フォーカス</h2>
  <p class="note">
    接続できたNotion記事イベント数が多い順です。最多の
    <code>{html.escape(primary_symbol or "—")}</code>をこの版の主対象とし、
    同一銘柄内のケース集積として期間別に集計します。
  </p>
  {_html_table(focus_overview_headers, focus_overview)}
  <h3>{html.escape(primary_symbol or "対象なし")} の期間別集計</h3>
  {_html_table(focus_primary_headers, focus_primary)}
  <h2>個別ケース分析</h2>
  {_html_table(case_headers, case_rows)}
  <h2>算出値の全期間明細</h2>
  <p class="note">
    推論文と切り離して検証できるよう、算出可能な決定論的指標を全て掲載します。
  </p>
  {case_details_html}
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
    ticker_inventory: dict[str, dict[str, object]] = {}
    for symbol, origin, events in connection.execute(
        """
        SELECT symbol, ticker_origin, count(*)
        FROM events_timed_input
        GROUP BY symbol, ticker_origin
        ORDER BY symbol, ticker_origin
        """
    ).fetchall():
        entry = ticker_inventory.setdefault(
            symbol,
            {"symbol": symbol, "article_events": 0, "ticker_origins": {}},
        )
        entry["article_events"] = int(entry["article_events"]) + int(events)
        origins = entry["ticker_origins"]
        assert isinstance(origins, dict)
        origins[str(origin)] = int(events)
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
        "ticker_inventory": sorted(
            ticker_inventory.values(),
            key=lambda item: (-int(item["article_events"]), str(item["symbol"])),
        ),
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
        # Split on statement terminators only. A bare split(";") also cuts at
        # semicolons inside comments, which turns a sentence of prose into a
        # parse error a hundred lines from where it was written.
        for statement_number, statement in enumerate(
            re.split(r";\s*\n", sql), start=1
        ):
            if not statement.strip():
                continue
            try:
                connection.execute(statement)
            except duckdb.Error as exc:
                raise CorpusError(
                    f"event study statement {statement_number} failed: {exc}"
                ) from exc

        event_returns = connection.execute(
            "SELECT * FROM event_returns ORDER BY reaction_date, symbol, page_id"
        ).to_arrow_table()
        event_summary = connection.execute("SELECT * FROM event_summary").to_arrow_table()
        event_unmatched = connection.execute(
            "SELECT * FROM event_unmatched ORDER BY symbol, candidate_date, page_id"
        ).to_arrow_table()
        event_case_context = connection.execute(
            "SELECT * FROM event_case_context ORDER BY event_key"
        ).to_arrow_table()
        symbol_news_coverage = connection.execute(
            "SELECT * FROM symbol_news_coverage ORDER BY events DESC, symbol"
        ).to_arrow_table()
        symbol_risk_profile = connection.execute(
            "SELECT * FROM symbol_risk_profile ORDER BY annualised_volatility DESC"
        ).to_arrow_table()
        symbol_correlations = connection.execute(
            "SELECT * FROM symbol_correlations ORDER BY correlation DESC"
        ).to_arrow_table()
        return_surface = connection.execute(
            "SELECT * FROM return_surface ORDER BY symbol, move_bucket, horizon"
        ).to_arrow_table()
        return_trade_plan = connection.execute(
            "SELECT * FROM return_trade_plan ORDER BY symbol, move_bucket"
        ).to_arrow_table()
        smoothed_return_surface = connection.execute(
            "SELECT * FROM return_surface_smoothed "
            "ORDER BY symbol, move_bucket, horizon"
        ).to_arrow_table()
        smoothed_trade_plan = connection.execute(
            "SELECT * FROM return_trade_plan_smoothed "
            "ORDER BY symbol, move_bucket"
        ).to_arrow_table()
        validation_results = connection.execute(
            "SELECT * FROM return_validation_results "
            "ORDER BY symbol, fold, move_bucket"
        ).to_arrow_table()
        validation_examples = connection.execute(
            "SELECT * FROM return_validation_examples "
            "ORDER BY symbol, fold, move_bucket, signal_date DESC"
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
    _write_parquet(paths["symbol_news_coverage.parquet"], symbol_news_coverage, pq)
    _write_parquet(paths["symbol_risk_profile.parquet"], symbol_risk_profile, pq)
    summary_rows = event_summary.to_pylist()
    event_rows = event_returns.to_pylist()
    context_rows = event_case_context.to_pylist()
    coverage_rows = symbol_news_coverage.to_pylist()
    risk_rows = symbol_risk_profile.to_pylist()
    correlation_rows = symbol_correlations.to_pylist()
    return_surface_rows = return_surface.to_pylist()
    return_trade_plan_rows = return_trade_plan.to_pylist()
    smoothed_surface_rows = smoothed_return_surface.to_pylist()
    smoothed_trade_plan_rows = smoothed_trade_plan.to_pylist()
    validation_result_rows = validation_results.to_pylist()
    validation_example_rows = validation_examples.to_pylist()
    report_payload = _report_payload(
        report_date,
        metadata,
        summary_rows,
        event_rows,
        context_rows,
        return_surface_rows,
        return_trade_plan_rows,
        smoothed_surface_rows,
        smoothed_trade_plan_rows,
        validation_result_rows,
        validation_example_rows,
        coverage_rows,
        risk_rows,
        correlation_rows,
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
        exchange_root = analysis_exchange_s3_root(settings)
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

        # The local model receives only the compact deterministic JSON, never
        # the Parquet corpus or backup database. A fixed input key also avoids
        # granting its IAM user permission to enumerate the corpus prefix.
        report_digest = all_digests["report.json"]
        if state.get("exchange_digest") != report_digest:
            uploader(paths["report.json"], f"{exchange_root}/input/latest/report.json")
            changed.append("exchange/input/latest/report.json")

        if state.get("index_digest") != index_digest:
            uploader(index_path, f"{s3_root}/index.json")
            changed.append("index.json")

        state["daily"][report_key] = all_digests
        state.update(
            {
                "latest": all_digests,
                "index_digest": index_digest,
                "exchange_digest": report_digest,
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
