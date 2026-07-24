import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from shared.global_context import (
    DailyClose,
    calculate_relationship,
    correlation_strength,
    next_close_move,
)


def _close_series(
    start: datetime,
    returns: list[float],
    *,
    initial: float = 100.0,
) -> list[DailyClose]:
    closes = [DailyClose(ends_at=start, close_price=initial)]
    price = initial
    for index, daily_return in enumerate(returns, start=1):
        price *= 1 + daily_return
        closes.append(
            DailyClose(
                ends_at=start + timedelta(days=index),
                close_price=price,
            )
        )
    return closes


def test_relationship_selects_the_largest_absolute_lag_and_beta():
    factor_returns = [
        0.011,
        -0.006,
        0.018,
        0.004,
        -0.014,
        0.009,
        -0.002,
        0.016,
        -0.011,
        0.006,
    ] * 4
    stock_returns = [0.003, *factor_returns]
    factor = _close_series(
        datetime(2026, 1, 1, 8, tzinfo=timezone.utc),
        factor_returns,
    )
    stock = _close_series(
        datetime(2026, 1, 1, 21, tzinfo=timezone.utc),
        stock_returns,
    )

    result = calculate_relationship(
        factor,
        stock,
        horizon_sessions=30,
    )

    assert result.selected_lag == 1
    assert result.correlation == pytest.approx(1.0)
    assert result.beta == pytest.approx(1.0)
    assert result.sample_count == 30
    assert result.strength == "strong"


def test_relationship_returns_null_below_minimum_and_for_zero_variance():
    varying_stock = _close_series(
        datetime(2026, 1, 1, 21, tzinfo=timezone.utc),
        [0.01, -0.01] * 10,
    )
    too_short = calculate_relationship(
        _close_series(
            datetime(2026, 1, 1, 8, tzinfo=timezone.utc),
            [0.02, -0.01] * 9,
        ),
        varying_stock,
        horizon_sessions=30,
    )
    assert too_short.correlation is None
    assert too_short.beta is None
    assert too_short.selected_lag is None
    assert too_short.sample_count < 20

    constant_factor = calculate_relationship(
        _close_series(
            datetime(2026, 1, 1, 8, tzinfo=timezone.utc),
            [0.01] * 25,
        ),
        _close_series(
            datetime(2026, 1, 1, 21, tzinfo=timezone.utc),
            [0.01, -0.02, 0.005, 0.013, -0.006] * 5,
        ),
        horizon_sessions=30,
    )
    assert constant_factor.correlation is None
    assert constant_factor.beta is None


@pytest.mark.parametrize(
    ("correlation", "expected"),
    [
        (0.0, "weak"),
        (-0.1999, "weak"),
        (0.20, "moderate"),
        (-0.3999, "moderate"),
        (0.40, "strong"),
        (-0.80, "strong"),
        (None, None),
    ],
)
def test_correlation_strength_boundaries(correlation, expected):
    assert correlation_strength(correlation) == expected


def test_alignment_handles_weekend_holiday_and_dst_aware_closes():
    new_york = ZoneInfo("America/New_York")
    tokyo = ZoneInfo("Asia/Tokyo")
    factor = [
        DailyClose(datetime(2026, 10, 30, 15, tzinfo=tokyo), 100),
        DailyClose(datetime(2026, 11, 2, 15, tzinfo=tokyo), 102),
        DailyClose(datetime(2026, 11, 3, 15, tzinfo=tokyo), 101),
    ]
    # U.S. clocks changed on Nov 1; UTC close shifts while local close remains 16:00.
    stock = [
        DailyClose(datetime(2026, 10, 30, 16, tzinfo=new_york), 200),
        DailyClose(datetime(2026, 11, 2, 16, tzinfo=new_york), 204),
        DailyClose(datetime(2026, 11, 3, 16, tzinfo=new_york), 202),
        DailyClose(datetime(2026, 11, 4, 16, tzinfo=new_york), 205),
    ]

    result = calculate_relationship(
        factor,
        stock,
        horizon_sessions=3,
        minimum_samples=2,
    )

    assert result.lag_statistics[0].sample_count == 2
    assert all(close.ends_at.utcoffset() is not None for close in stock)


def test_next_close_move_uses_strict_pre_and_post_event_closes():
    closes = [
        DailyClose(datetime(2026, 7, 20, 20, tzinfo=timezone.utc), 100),
        DailyClose(datetime(2026, 7, 21, 20, tzinfo=timezone.utc), 105),
        DailyClose(datetime(2026, 7, 22, 20, tzinfo=timezone.utc), 102),
    ]

    assert next_close_move(
        closes,
        datetime(2026, 7, 21, 3, tzinfo=timezone.utc),
    ) == pytest.approx(5.0)
    assert next_close_move(
        closes,
        datetime(2026, 7, 21, 20, tzinfo=timezone.utc),
    ) == pytest.approx(2.0)
    assert (
        next_close_move(
            closes,
            datetime(2026, 7, 19, tzinfo=timezone.utc),
        )
        is None
    )


def test_relationship_rejects_invalid_options():
    with pytest.raises(ValueError, match="horizon"):
        calculate_relationship([], [], horizon_sessions=0)
    with pytest.raises(ValueError, match="minimum"):
        calculate_relationship([], [], horizon_sessions=30, minimum_samples=1)
    with pytest.raises(ValueError, match="lags"):
        calculate_relationship([], [], horizon_sessions=30, lags=(-1,))


def test_calculated_values_are_finite():
    result = calculate_relationship(
        _close_series(
            datetime(2026, 1, 1, 8, tzinfo=timezone.utc),
            [0.01, -0.02, 0.005, 0.03, -0.01] * 6,
        ),
        _close_series(
            datetime(2026, 1, 1, 21, tzinfo=timezone.utc),
            [0.008, -0.01, 0.006, 0.02, -0.004] * 6,
        ),
        horizon_sessions=30,
    )
    assert result.correlation is not None
    assert result.beta is not None
    assert math.isfinite(result.correlation)
    assert math.isfinite(result.beta)
