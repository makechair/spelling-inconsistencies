"""Parametric value at risk.

The arithmetic is small and the assumptions are large, so these tests pin
both: the closed forms that can be checked by hand, and the choices made
where the data runs out.
"""

from __future__ import annotations

import math

import pytest

from usstocks.risk import portfolio_var


def test_a_single_position_is_the_textbook_formula():
    result = portfolio_var({"A": 1_000_000.0}, {"A": 0.32}, {})
    daily = 0.32 / math.sqrt(252)
    assert result["value_at_risk"] == pytest.approx(1.6449 * 1_000_000 * daily)
    assert result["value_at_risk_fraction"] == pytest.approx(1.6449 * daily)
    # One position cannot diversify against itself.
    assert result["diversification_benefit"] == pytest.approx(0.0)


def test_two_uncorrelated_positions_add_in_quadrature():
    """The whole point of the covariance term: half a million in each of two
    unrelated names risks less than a million in one of them."""
    result = portfolio_var(
        {"A": 500_000.0, "B": 500_000.0},
        {"A": 0.32, "B": 0.32},
        {("A", "B"): 0.0},
    )
    single = portfolio_var({"A": 1_000_000.0}, {"A": 0.32}, {})
    assert result["value_at_risk"] == pytest.approx(single["value_at_risk"] / math.sqrt(2))
    assert result["diversification_benefit"] > 0


def test_perfectly_correlated_positions_get_no_benefit():
    result = portfolio_var(
        {"A": 500_000.0, "B": 500_000.0},
        {"A": 0.32, "B": 0.32},
        {("A", "B"): 1.0},
    )
    assert result["diversification_benefit"] == pytest.approx(0.0, abs=1e-6)
    assert result["value_at_risk"] == pytest.approx(result["undiversified_value_at_risk"])


def test_an_unmeasured_pair_is_assumed_to_move_together():
    """Assuming independence would quietly reduce the reported risk of the
    pairs the corpus knows least about -- the wrong direction to be wrong in,
    and these symbols share one cycle anyway."""
    unknown = portfolio_var({"A": 500_000.0, "B": 500_000.0}, {"A": 0.3, "B": 0.3}, {})
    together = portfolio_var(
        {"A": 500_000.0, "B": 500_000.0}, {"A": 0.3, "B": 0.3}, {("A", "B"): 1.0}
    )
    assert unknown["value_at_risk"] == pytest.approx(together["value_at_risk"])


def test_the_correlation_lookup_does_not_depend_on_the_order_asked():
    stored = {("A", "B"): 0.25}
    forwards = portfolio_var({"A": 1.0, "B": 1.0}, {"A": 0.3, "B": 0.3}, stored)
    backwards = portfolio_var({"B": 1.0, "A": 1.0}, {"A": 0.3, "B": 0.3}, stored)
    assert forwards["value_at_risk"] == pytest.approx(backwards["value_at_risk"])


def test_the_horizon_scales_by_the_square_root_of_time():
    one = portfolio_var({"A": 1_000_000.0}, {"A": 0.32}, {}, horizon_days=1)
    ten = portfolio_var({"A": 1_000_000.0}, {"A": 0.32}, {}, horizon_days=10)
    assert ten["value_at_risk"] == pytest.approx(one["value_at_risk"] * math.sqrt(10))


def test_a_position_with_no_volatility_is_named_rather_than_treated_as_safe():
    """A position whose risk is unknown is not a position without risk."""
    result = portfolio_var({"A": 1_000.0, "B": 1_000.0}, {"A": 0.3}, {})
    assert result["skipped_symbols"] == ["B"]
    assert result["portfolio_value"] == pytest.approx(1_000.0)


def test_no_usable_position_reports_nothing_rather_than_zero():
    result = portfolio_var({"B": 1_000.0}, {}, {})
    assert result["value_at_risk"] is None
    assert result["skipped_symbols"] == ["B"]


def test_the_worst_actual_day_travels_beside_the_normal_estimate():
    """The normal assumption has thin tails. Carrying the real worst day is
    the correction, and it is usually the larger number."""
    result = portfolio_var(
        {"A": 1_000_000.0}, {"A": 0.32}, {}, worst_days={"A": -0.18}
    )
    position = result["positions"][0]
    assert position["worst_day"] == -0.18
    assert position["worst_day_loss"] == pytest.approx(180_000.0)
    assert position["worst_day_loss"] > result["value_at_risk"]


def test_an_unsupported_confidence_is_refused():
    with pytest.raises(ValueError):
        portfolio_var({"A": 1.0}, {"A": 0.3}, {}, confidence=0.90)
