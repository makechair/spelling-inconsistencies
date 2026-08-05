"""The fundamentals reading guide.

The point of these is the boundary, not the prose: the model may pick facts
and explain them, and everything numeric the reader sees comes from the
metrics. Ollama is never contacted here.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from usstocks.corpus.fundamentals_narrative import build_facts, enrich, enrich_symbol

ROW = {
    "symbol": "MU",
    "subsector": "memory_storage",
    "basis": "ttm",
    "period_end": "2025-08-28",
    "revenue_yoy": 0.489,
    "revenue_yoy_change": -0.127,
    "gross_margin": 0.398,
    "gross_margin_yoy_change": 0.174,
    "inventory_days": 135.1,
    "inventory_days_yoy_change": -30.5,
}


def reply(payload: dict) -> httpx.Response:
    # raise_for_status needs a request attached, even on a synthetic response.
    return httpx.Response(
        200,
        json={"message": {"content": json.dumps(payload, ensure_ascii=False)}},
        request=httpx.Request("POST", "http://x/api/chat"),
    )


def test_a_metric_the_filer_never_reported_becomes_no_fact():
    """A blank handed to the model is a blank it might read as zero."""
    facts = build_facts({"symbol": "ARM", "basis": "annual", "period_end": "2026-03-31",
                         "revenue_yoy": 0.2})
    texts = [fact["text"] for fact in facts]
    assert any("増収率" in text for text in texts)
    assert not any("在庫日数" in text for text in texts)
    assert texts[-1].startswith("集計基準: 通期決算")


def test_the_basis_travels_with_the_facts():
    assert "直近12ヶ月の合計" in build_facts(ROW)[-1]["text"]


def test_a_figure_the_model_invented_is_rejected(monkeypatch):
    """The whole safeguard: prose may explain the evidence, never restate a
    number that is not in it."""
    def handler(_request: httpx.Request) -> httpx.Response:
        return reply({
            "overview": "在庫調整が進んだ",
            "points": [{
                "title": "在庫",
                "interpretation": "在庫日数は 999.9日 まで伸びた",
                "evidence_ids": ["F7"],
            }],
            "caution": "単一期の数値である",
        })

    monkeypatch.setattr(httpx, "post", lambda *a, **k: handler(None))
    with pytest.raises(ValueError, match="unsupported figure"):
        enrich_symbol(ROW, model="qwen3:14b", ollama_url="http://x", timeout=1)


def test_evidence_text_is_copied_from_the_metrics(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return reply({
            "overview": "在庫の積み上がりが解消しつつある",
            "points": [{
                "title": "在庫の正常化",
                "interpretation": "短縮は需要側の回復と整合する",
                "evidence_ids": ["F5", "F6"],
            }],
            "caution": "循環の局面は一期では確かめられない",
        })

    monkeypatch.setattr(httpx, "post", lambda *a, **k: handler(None))
    digest = enrich_symbol(ROW, model="qwen3:14b", ollama_url="http://x", timeout=1)
    texts = [item["text"] for item in digest["points"][0]["evidence"]]
    assert any("135日" in text for text in texts)
    assert any("-30.5日" in text for text in texts)


def test_one_bad_symbol_does_not_cost_the_others(tmp_path: Path, monkeypatch):
    """Fifty-three symbols run in one pass on a local model; a single mangled
    reply must not discard the rest."""
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({
        "generated_at": "2026-08-05T00:00:00+00:00",
        "symbols": [ROW, {**ROW, "symbol": "NVDA"}],
    }), encoding="utf-8")

    calls = {"n": 0}

    def post(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return reply({"overview": "壊れている"})  # no points, no caution
        return reply({
            "overview": "増収が続いている",
            "points": [{"title": "成長", "interpretation": "拡大が続く",
                        "evidence_ids": ["F1"]}],
            "caution": "一期のみの観測である",
        })

    monkeypatch.setattr(httpx, "post", post)
    payload = enrich(summary, tmp_path / "digest.json",
                     model="qwen3:14b", ollama_url="http://x", timeout=1)

    assert set(payload["symbols"]) == {"NVDA"}
    assert payload["failed"] == {"MU": "ValueError"}
    assert json.loads((tmp_path / "digest.json").read_text())["model"] == "qwen3:14b"


def test_a_symbol_with_nothing_to_interpret_is_skipped(tmp_path: Path, monkeypatch):
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({
        "symbols": [{"symbol": "X", "basis": "annual", "period_end": "2026-01-01"}],
    }), encoding="utf-8")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("must not call Ollama"))
    payload = enrich(summary, tmp_path / "digest.json",
                     model="qwen3:14b", ollama_url="http://x", timeout=1)
    assert payload["symbols"] == {}
