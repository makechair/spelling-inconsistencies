"""Parametric value at risk over a list's positions.

Pure arithmetic over figures the corpus jobs already published: each symbol's
annualised volatility and the pairwise correlations from the event study, the
latest close from the fundamentals summary, and the quantities on the list.
Nothing here reads a Parquet file, which is why it can live in the API
process -- there is no DuckDB in it, deliberately.

**This is the variance-covariance method, and it assumes returns are
normal.** They are not: a semiconductor name drops eight per cent on a
guidance cut far more often than a normal distribution allows. The figure
below is therefore a floor on the bad day, not a bound on it, and the caller
is expected to say so. The worst single day each symbol actually had is
carried alongside for exactly that reason -- it is usually larger than the
95% figure, and seeing the two together is the correction.
"""

from __future__ import annotations

import math
from typing import Any

# One-sided normal quantiles. Only the two conventions anyone reports.
CONFIDENCE_QUANTILES = {0.95: 1.6449, 0.99: 2.3263}
TRADING_DAYS = 252


def _correlation(
    correlations: dict[tuple[str, str], float], left: str, right: str
) -> float:
    if left == right:
        return 1.0
    key = (left, right) if left < right else (right, left)
    value = correlations.get(key)
    # An unmeasured pair is treated as moving together, not as independent.
    # Assuming independence would quietly reduce the risk of exactly the pairs
    # the corpus knows least about, which is the wrong direction to be wrong
    # in. These symbols share one industry cycle in any case.
    return 1.0 if value is None else value


def portfolio_var(
    positions: dict[str, float],
    volatilities: dict[str, float],
    correlations: dict[tuple[str, str], float],
    *,
    confidence: float = 0.95,
    horizon_days: int = 1,
    worst_days: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Value at risk for a set of position values, in the currency they are in.

    ``positions`` maps symbol to market value. Symbols without a volatility
    are excluded and named in the result rather than treated as riskless: a
    position with unknown risk is not a safe position.
    """
    quantile = CONFIDENCE_QUANTILES.get(round(confidence, 2))
    if quantile is None:
        raise ValueError(f"confidence must be one of {sorted(CONFIDENCE_QUANTILES)}")
    if horizon_days < 1:
        raise ValueError("horizon must be at least one day")

    priced = {
        symbol: value
        for symbol, value in positions.items()
        if volatilities.get(symbol) is not None and value
    }
    skipped = sorted(set(positions) - set(priced))
    if not priced:
        return {
            "value_at_risk": None,
            "portfolio_value": sum(positions.values()) or 0.0,
            "skipped_symbols": skipped,
        }

    # Daily volatility from the annualised figure, then scaled to the horizon
    # by the square root of time -- the same independence assumption the
    # annualisation made, applied in the other direction.
    daily = {
        symbol: volatilities[symbol] / math.sqrt(TRADING_DAYS) for symbol in priced
    }
    variance = 0.0
    for left, left_value in priced.items():
        for right, right_value in priced.items():
            variance += (
                left_value
                * right_value
                * daily[left]
                * daily[right]
                * _correlation(correlations, left, right)
            )
    sigma = math.sqrt(max(variance, 0.0)) * math.sqrt(horizon_days)

    # Each position on its own, summed. The gap between this and the figure
    # above is the whole benefit of holding more than one thing -- and in a
    # single-industry book it is usually small, which is the point of showing
    # it.
    standalone = {
        symbol: quantile * abs(value) * daily[symbol] * math.sqrt(horizon_days)
        for symbol, value in priced.items()
    }
    undiversified = sum(standalone.values())
    value_at_risk = quantile * sigma
    return {
        "confidence": confidence,
        "horizon_days": horizon_days,
        "portfolio_value": sum(priced.values()),
        "value_at_risk": value_at_risk,
        "value_at_risk_fraction": (
            value_at_risk / sum(priced.values()) if sum(priced.values()) else None
        ),
        "undiversified_value_at_risk": undiversified,
        "diversification_benefit": undiversified - value_at_risk,
        "positions": [
            {
                "symbol": symbol,
                "value": value,
                "weight": value / sum(priced.values()) if sum(priced.values()) else None,
                "annualised_volatility": volatilities[symbol],
                "standalone_value_at_risk": standalone[symbol],
                # What this symbol actually did on its worst day, as a
                # reminder that the normal assumption above has thin tails.
                "worst_day": (worst_days or {}).get(symbol),
                "worst_day_loss": (
                    abs((worst_days or {})[symbol]) * abs(value)
                    if (worst_days or {}).get(symbol) is not None
                    else None
                ),
            }
            for symbol, value in sorted(
                priced.items(), key=lambda item: -standalone[item[0]]
            )
        ],
        # Named, not silently dropped: a position whose risk is unknown is not
        # a position without risk.
        "skipped_symbols": skipped,
    }
