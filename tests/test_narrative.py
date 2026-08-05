from __future__ import annotations

import pytest

from usstocks.corpus.narrative import _facts, validate_digest


def test_digest_resolves_model_selected_ids_to_deterministic_evidence():
    facts = _facts({
        "findings": [{"title": "反応", "body": "5日後は+2.10%、18件です。"}],
        "focus_symbol": "MU",
        "walk_forward_simulations": {},
    })

    digest = validate_digest({
        "overview": "短期反応を慎重に確認します",
        "points": [{
            "title": "短期",
            "interpretation": "5日後の傾向は限定的です",
            "evidence_ids": ["F1"],
        }],
        "caution": "因果関係は示しません",
    }, facts)

    assert digest["points"][0]["evidence"][0]["text"] == "反応: 5日後は+2.10%、18件です。"


def test_digest_rejects_a_figure_not_present_in_selected_evidence():
    facts = [{"id": "F1", "text": "勝率56.0%です。"}]
    with pytest.raises(ValueError, match="unsupported figure"):
        validate_digest({
            "overview": "傾向を確認します",
            "points": [{
                "title": "検証",
                "interpretation": "勝率は70%です",
                "evidence_ids": ["F1"],
            }],
            "caution": "標本数に注意します",
        }, facts)
