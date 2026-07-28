"""Configuration defaults and safety checks."""

from __future__ import annotations

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


def test_quotes_are_not_subscribed_by_default():
    """Threshold 5 = trades only: quotes cannot produce OHLCV and multiply
    bandwidth (docs/spec-review.md A-1, B-3)."""
    assert Settings().tiingo_threshold_level == 5


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
