//! Operational math verified with Kani: market session partitioning and exponential backoff.

/// U.S. equity trading session classification.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EquitySession {
    Pre,
    Regular,
    After,
    Closed,
}

impl EquitySession {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Pre => "pre",
            Self::Regular => "regular",
            Self::After => "after",
            Self::Closed => "closed",
        }
    }
}

/// U.S. equity index futures session classification.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FuturesSession {
    Open,
    Break,
    Closed,
}

impl FuturesSession {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Open => "futures_open",
            Self::Break => "futures_break",
            Self::Closed => "futures_closed",
        }
    }
}

/// Local America/New_York session cutoffs in seconds since midnight.
pub const EQUITY_PRE_SECS: u32 = 4 * 3600; // 04:00:00
pub const EQUITY_OPEN_SECS: u32 = 9 * 3600 + 30 * 60; // 09:30:00
pub const EQUITY_EARLY_CLOSE_SECS: u32 = 13 * 3600; // 13:00:00
pub const EQUITY_CLOSE_SECS: u32 = 16 * 3600; // 16:00:00
pub const EQUITY_AFTER_SECS: u32 = 20 * 3600; // 20:00:00

pub const FUTURES_BREAK_START_SECS: u32 = 17 * 3600; // 17:00:00
pub const FUTURES_BREAK_END_SECS: u32 = 18 * 3600; // 18:00:00

/// Classify a U.S. equity session from trading-day flags and local NY seconds since midnight.
pub const fn classify_equity_session(
    is_trading_day: bool,
    is_early_close: bool,
    secs_since_midnight: u32,
) -> EquitySession {
    if !is_trading_day {
        return EquitySession::Closed;
    }
    let close = if is_early_close {
        EQUITY_EARLY_CLOSE_SECS
    } else {
        EQUITY_CLOSE_SECS
    };
    if secs_since_midnight >= EQUITY_OPEN_SECS && secs_since_midnight <= close {
        EquitySession::Regular
    } else if secs_since_midnight >= EQUITY_PRE_SECS && secs_since_midnight < EQUITY_OPEN_SECS {
        EquitySession::Pre
    } else if secs_since_midnight > close && secs_since_midnight <= EQUITY_AFTER_SECS {
        EquitySession::After
    } else {
        EquitySession::Closed
    }
}

/// Classify a U.S. futures session from `weekday_from_mon` (`0=Mon..=6=Sun`)
/// and local NY seconds since midnight.
pub const fn classify_futures_session(
    weekday_from_mon: u8,
    secs_since_midnight: u32,
) -> FuturesSession {
    // Friday = 4, Saturday = 5, Sunday = 6
    if weekday_from_mon == 5
        || (weekday_from_mon == 4 && secs_since_midnight >= FUTURES_BREAK_START_SECS)
        || (weekday_from_mon == 6 && secs_since_midnight < FUTURES_BREAK_END_SECS)
    {
        FuturesSession::Closed
    } else if secs_since_midnight >= FUTURES_BREAK_START_SECS
        && secs_since_midnight < FUTURES_BREAK_END_SECS
    {
        FuturesSession::Break
    } else {
        FuturesSession::Open
    }
}

/// Normalize `(base_nanos, max_nanos)` so `1 <= base <= max` always holds.
pub const fn normalize_backoff_bounds(base_nanos: u128, max_nanos: u128) -> (u128, u128) {
    let base = if base_nanos == 0 { 1 } else { base_nanos };
    let max = if max_nanos < base { base } else { max_nanos };
    (base, max)
}

/// Compute the next per-symbol backoff penalty in nanoseconds.
///
/// Guarantees:
/// - Always `>= 1` nanosecond and `<= normalized_max`.
/// - Overflow-free: doubling saturates at `normalized_max` instead of resetting to `base`.
/// - Explicit `Retry-After` overrides local doubling (clamped to `[1, normalized_max]`).
pub const fn next_backoff_nanos(
    base_nanos: u128,
    max_nanos: u128,
    current_penalty_nanos: Option<u128>,
    retry_after_nanos: Option<u128>,
) -> u128 {
    let (base, max) = normalize_backoff_bounds(base_nanos, max_nanos);
    let raw = match retry_after_nanos {
        Some(ra) => ra,
        None => match current_penalty_nanos {
            Some(current) => current.saturating_mul(2),
            None => base,
        },
    };
    if raw == 0 {
        1
    } else if raw > max {
        max
    } else {
        raw
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn equity_session_boundaries_match_nyse_schedule() {
        assert_eq!(
            classify_equity_session(false, false, EQUITY_OPEN_SECS),
            EquitySession::Closed
        );
        assert_eq!(
            classify_equity_session(true, false, EQUITY_PRE_SECS - 1),
            EquitySession::Closed
        );
        assert_eq!(
            classify_equity_session(true, false, EQUITY_PRE_SECS),
            EquitySession::Pre
        );
        assert_eq!(
            classify_equity_session(true, false, EQUITY_OPEN_SECS),
            EquitySession::Regular
        );
        assert_eq!(
            classify_equity_session(true, false, EQUITY_CLOSE_SECS),
            EquitySession::Regular
        );
        assert_eq!(
            classify_equity_session(true, true, EQUITY_EARLY_CLOSE_SECS + 1),
            EquitySession::After
        );
        assert_eq!(
            classify_equity_session(true, false, EQUITY_AFTER_SECS + 1),
            EquitySession::Closed
        );
    }

    #[test]
    fn backoff_handles_zero_bounds_and_u128_saturation() {
        assert_eq!(normalize_backoff_bounds(0, 0), (1, 1));
        assert_eq!(next_backoff_nanos(0, 0, None, None), 1);
        assert_eq!(
            next_backoff_nanos(10, 100, Some(u128::MAX), None),
            100,
            "overflowing double must saturate at max rather than drop to base"
        );
        assert_eq!(next_backoff_nanos(10, 100, Some(80), Some(45)), 45);
    }
}

#[cfg(kani)]
mod kani_proofs {
    use super::*;

    #[kani::proof]
    fn verify_equity_session_partitions_day_cleanly() {
        let is_trading_day: bool = kani::any();
        let is_early_close: bool = kani::any();
        let secs: u32 = kani::any();
        kani::assume(secs < 86_400);

        let session = classify_equity_session(is_trading_day, is_early_close, secs);
        if !is_trading_day {
            assert_eq!(session, EquitySession::Closed);
            return;
        }

        let close = if is_early_close {
            EQUITY_EARLY_CLOSE_SECS
        } else {
            EQUITY_CLOSE_SECS
        };

        if secs < EQUITY_PRE_SECS || secs > EQUITY_AFTER_SECS {
            assert_eq!(session, EquitySession::Closed);
        } else if secs < EQUITY_OPEN_SECS {
            assert_eq!(session, EquitySession::Pre);
        } else if secs <= close {
            assert_eq!(session, EquitySession::Regular);
        } else {
            assert_eq!(session, EquitySession::After);
        }

        // Early close never makes a Regular session longer than a full session.
        let full = classify_equity_session(true, false, secs);
        let early = classify_equity_session(true, true, secs);
        if early == EquitySession::Regular {
            assert_eq!(full, EquitySession::Regular);
        }
    }

    #[kani::proof]
    fn verify_futures_session_partitions_week_cleanly() {
        let weekday: u8 = kani::any();
        let secs: u32 = kani::any();
        kani::assume(weekday <= 6);
        kani::assume(secs < 86_400);

        let session = classify_futures_session(weekday, secs);
        if weekday == 5
            || (weekday == 4 && secs >= FUTURES_BREAK_START_SECS)
            || (weekday == 6 && secs < FUTURES_BREAK_END_SECS)
        {
            assert_eq!(session, FuturesSession::Closed);
        } else if secs >= FUTURES_BREAK_START_SECS && secs < FUTURES_BREAK_END_SECS {
            assert_eq!(session, FuturesSession::Break);
        } else {
            assert_eq!(session, FuturesSession::Open);
        }
    }

    #[kani::proof]
    fn verify_polling_backoff_bounds_monotonicity_and_retry_after() {
        let base_nanos: u128 = kani::any();
        let max_nanos: u128 = kani::any();
        let current = if kani::any() { Some(kani::any()) } else { None };
        let retry_after = if kani::any() { Some(kani::any()) } else { None };

        let (norm_base, norm_max) = normalize_backoff_bounds(base_nanos, max_nanos);
        assert!(norm_base >= 1);
        assert!(norm_max >= norm_base);

        let next = next_backoff_nanos(base_nanos, max_nanos, current, retry_after);
        assert!(next >= 1 && next <= norm_max);

        // Retry-After in [1, norm_max] strictly dominates local doubling.
        if let Some(ra) = retry_after {
            if ra >= 1 && ra <= norm_max {
                assert_eq!(next, ra);
            }
        } else if let Some(cur) = current {
            // Without Retry-After, backoff is monotonically non-decreasing up to norm_max
            // and never resets downward on u128::MAX overflow.
            if cur <= norm_max {
                assert!(next >= cur);
            } else {
                assert_eq!(next, norm_max);
            }
        } else {
            assert_eq!(next, norm_base);
        }
    }
}
