"""Provider-neutral global-context models and session-aware statistics."""

from __future__ import annotations

import bisect
import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from itertools import pairwise
from typing import Literal

BarInterval = Literal["1h", "1d"]
RelationshipStrength = Literal["weak", "moderate", "strong"]


@dataclass(frozen=True)
class InstrumentMetadata:
    instrument_key: str
    display_name: str
    asset_class: str
    currency: str
    exchange: str | None
    timezone: str
    provider_aliases: dict[str, str]
    session_metadata: dict[str, object]
    quote_convention: str | None = None


@dataclass(frozen=True)
class NormalizedMarketBar:
    instrument_key: str
    interval: BarInterval
    starts_at: datetime
    ends_at: datetime
    session_date: date
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: float | None
    provider: str


@dataclass(frozen=True)
class DailyClose:
    ends_at: datetime
    close_price: float


@dataclass(frozen=True)
class LagStatistic:
    lag_sessions: int
    correlation: float | None
    beta: float | None
    sample_count: int


@dataclass(frozen=True)
class RelationshipStatistic:
    correlation: float | None
    beta: float | None
    selected_lag: int | None
    sample_count: int
    strength: RelationshipStrength | None
    lag_statistics: tuple[LagStatistic, ...]


def correlation_strength(
    correlation: float | None,
) -> RelationshipStrength | None:
    if correlation is None:
        return None
    absolute = abs(correlation)
    if absolute < 0.20:
        return "weak"
    if absolute < 0.40:
        return "moderate"
    return "strong"


def _returns(closes: list[DailyClose]) -> list[tuple[datetime, float]]:
    output: list[tuple[datetime, float]] = []
    for previous, current in pairwise(closes):
        if previous.close_price <= 0 or current.close_price <= 0:
            continue
        output.append(
            (
                current.ends_at,
                current.close_price / previous.close_price - 1.0,
            )
        )
    return output


def _pearson_and_beta(
    pairs: list[tuple[float, float]],
    minimum_samples: int,
) -> tuple[float | None, float | None]:
    if len(pairs) < minimum_samples:
        return None, None

    factor_values = [pair[0] for pair in pairs]
    stock_values = [pair[1] for pair in pairs]
    factor_mean = math.fsum(factor_values) / len(factor_values)
    stock_mean = math.fsum(stock_values) / len(stock_values)
    factor_ss = math.fsum((value - factor_mean) ** 2 for value in factor_values)
    stock_ss = math.fsum((value - stock_mean) ** 2 for value in stock_values)
    if factor_ss <= 0 or stock_ss <= 0:
        return None, None

    covariance_sum = math.fsum(
        (factor - factor_mean) * (stock - stock_mean) for factor, stock in pairs
    )
    correlation = covariance_sum / math.sqrt(factor_ss * stock_ss)
    beta = covariance_sum / factor_ss
    return correlation, beta


def calculate_relationship(
    factor_closes: Iterable[DailyClose],
    stock_closes: Iterable[DailyClose],
    *,
    horizon_sessions: int,
    lags: tuple[int, ...] = (0, 1, 2),
    minimum_samples: int = 20,
) -> RelationshipStatistic:
    """Align factor closes to the first later U.S. close and score each lag."""
    if horizon_sessions <= 0:
        raise ValueError("horizon_sessions must be positive")
    if minimum_samples <= 1:
        raise ValueError("minimum_samples must be greater than one")
    if not lags or any(lag < 0 for lag in lags):
        raise ValueError("lags must contain non-negative values")

    factors = sorted(factor_closes, key=lambda close: close.ends_at)
    stocks = sorted(stock_closes, key=lambda close: close.ends_at)
    factor_returns = _returns(factors)
    stock_returns = _returns(stocks)
    stock_return_ends = [point[0] for point in stock_returns]

    lag_statistics: list[LagStatistic] = []
    for lag in lags:
        pairs: list[tuple[float, float]] = []
        for factor_end, factor_return in factor_returns:
            first_later = bisect.bisect_right(stock_return_ends, factor_end)
            stock_index = first_later + lag
            if stock_index >= len(stock_returns):
                continue
            pairs.append((factor_return, stock_returns[stock_index][1]))

        pairs = pairs[-horizon_sessions:]
        correlation, beta = _pearson_and_beta(pairs, minimum_samples)
        lag_statistics.append(
            LagStatistic(
                lag_sessions=lag,
                correlation=correlation,
                beta=beta,
                sample_count=len(pairs),
            )
        )

    valid = [
        statistic for statistic in lag_statistics if statistic.correlation is not None
    ]
    if not valid:
        best_sample_count = max(
            (statistic.sample_count for statistic in lag_statistics),
            default=0,
        )
        return RelationshipStatistic(
            correlation=None,
            beta=None,
            selected_lag=None,
            sample_count=best_sample_count,
            strength=None,
            lag_statistics=tuple(lag_statistics),
        )

    selected = max(
        valid,
        key=lambda statistic: (
            abs(statistic.correlation or 0.0),
            -statistic.lag_sessions,
        ),
    )
    return RelationshipStatistic(
        correlation=selected.correlation,
        beta=selected.beta,
        selected_lag=selected.lag_sessions,
        sample_count=selected.sample_count,
        strength=correlation_strength(selected.correlation),
        lag_statistics=tuple(lag_statistics),
    )


def next_close_move(
    stock_closes: Iterable[DailyClose],
    occurred_at: datetime,
) -> float | None:
    """Return the percent move from the last pre-event close to the next close."""
    closes = sorted(stock_closes, key=lambda close: close.ends_at)
    ends = [close.ends_at for close in closes]
    before_index = bisect.bisect_left(ends, occurred_at) - 1
    after_index = bisect.bisect_right(ends, occurred_at)
    if before_index < 0 or after_index >= len(closes):
        return None
    before = closes[before_index].close_price
    after = closes[after_index].close_price
    if before <= 0 or after <= 0:
        return None
    return (after / before - 1.0) * 100.0
