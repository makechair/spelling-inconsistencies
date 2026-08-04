"""Configuration defaults and safety checks."""

from __future__ import annotations

import pytest

from usstocks.config import Settings


def test_symbol_cap_defaults_to_ten():
    """The spec said "up to about 30 symbols", but at 30 the free tier's
    1 GB/month ingress is exceeded under any realistic assumption
    (docs/spec-review.md A-1). The cap was lowered to 10."""
    assert Settings().max_symbols == 10


def test_free_tier_budgets_match_the_documented_limits():
    settings = Settings()
    assert settings.rest_calls_per_hour == 50
    assert settings.rest_calls_per_day == 1_000
    assert settings.monthly_bandwidth_bytes == 1_000_000_000
    assert settings.background_poll_seconds == 3_600


def test_corpus_staging_admits_new_symbols_faster_than_it_refreshes():
    """These are our pacing choice, not a provider limit: the monthly
    unique-symbol cap is still unmeasured. Staging too slowly is not free
    either -- an event only matches once its symbol has daily bars, so the
    event study stays narrow while the universe trickles in.
    """
    settings = Settings()
    assert settings.corpus_max_new_symbols_per_run == 10
    assert settings.corpus_max_symbols_per_run == 20
    assert settings.corpus_max_new_symbols_per_run <= settings.corpus_max_symbols_per_run


def test_threshold_level_is_left_to_the_plan_by_default():
    """Level 5 (trades only) is what the workload wants -- quotes cannot produce
    OHLCV and multiply bandwidth (docs/spec-review.md A-1, B-3) -- but it is not
    accepted on every tier. A free key is refused at subscribe time and the
    socket closed, so a hard-coded 5 meant a fresh install never connected.
    """
    assert Settings().tiingo_threshold_level is None


def test_threshold_level_is_sent_only_when_configured():
    from usstocks.adapters.tiingo import TiingoAdapter

    assert TiingoAdapter("k")._threshold_level is None
    assert TiingoAdapter("k", threshold_level=5)._threshold_level == 5


def test_second_level_data_is_not_stored_by_default():
    assert Settings().tick_retention_days == 0


def test_csv_env_values_are_split():
    settings = Settings(
        source_priority="tiingo, alpaca",
        allowed_emails="A@Example.com, b@example.com",
    )
    assert settings.source_priority == ["tiingo", "alpaca"]
    # Emails are lowercased so the allow-list check is case-insensitive.
    assert settings.allowed_emails == ["a@example.com", "b@example.com"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("owner@example.com", ["owner@example.com"]),
        ("A@Example.com, b@example.com", ["a@example.com", "b@example.com"]),
        ('["owner@example.com"]', ["owner@example.com"]),
    ],
)
def test_list_fields_parse_from_the_environment(monkeypatch, raw, expected):
    """The path production actually uses.

    test_csv_env_values_are_split constructs Settings directly, which skips the
    settings source entirely. pydantic-settings JSON-decodes list-typed fields
    inside that source, so for a long time the plain address documented in
    .env.example raised SettingsError on the instance while the suite stayed
    green.
    """
    monkeypatch.setenv("USSTOCKS_ALLOWED_EMAILS", raw)
    assert Settings().allowed_emails == expected


def test_source_priority_parses_from_the_environment(monkeypatch):
    monkeypatch.setenv("USSTOCKS_SOURCE_PRIORITY", "alpaca, tiingo")
    assert Settings().source_priority == ["alpaca", "tiingo"]
