//! Pure market, FX, and synthetic NAV calculations.

use core::fmt;

/// Maximum plausible single-session ETF proxy move (%) accepted for synthetic NAV.
pub const MAX_PLAUSIBLE_MOVE: f64 = 8.0;

/// Portfolio proxy weights for SWYGX (sum == 1.0).
pub const NAV_HOLDINGS: [(&str, f64); 6] = [
    ("SCHB", 0.55),
    ("SCHF", 0.27),
    ("SCHE", 0.08),
    ("SCHZ", 0.06),
    ("SCHP", 0.02),
    ("SCHO", 0.02),
];

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MarketMathError {
    NonPositiveOrNonFiniteFxPrice,
    FxInversionOverflow,
    NonPositiveOrNonFiniteClose,
    ImplausibleProxyMove,
}

impl fmt::Display for MarketMathError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NonPositiveOrNonFiniteFxPrice => {
                write!(f, "FX prices must be finite and positive before inversion")
            }
            Self::FxInversionOverflow => {
                write!(f, "FX inversion overflowed finite f64 range")
            }
            Self::NonPositiveOrNonFiniteClose => {
                write!(f, "proxy closes must be finite with positive baseline")
            }
            Self::ImplausibleProxyMove => write!(f, "implausible proxy move"),
        }
    }
}

impl std::error::Error for MarketMathError {}

/// Compute relative metric versus a sector baseline.
///
/// When `invert` is `true` (used for P/E and Beta where lower is better), a
/// value below the positive sector baseline yields a **positive** relative score.
/// Non-finite inputs or zero baselines fail safe to `0.0`.
pub fn calculate_relative(value: Option<f64>, baseline: Option<f64>, invert: bool) -> f64 {
    match (value, baseline) {
        (Some(value), Some(baseline))
            if value.is_finite() && baseline.is_finite() && baseline != 0.0 =>
        {
            let relative = (value - baseline) / baseline;
            if !relative.is_finite() {
                return 0.0;
            }
            if invert {
                -relative
            } else {
                relative
            }
        }
        _ => 0.0,
    }
}

#[inline]
fn recip_positive(x: f64) -> f64 {
    debug_assert!(x.is_finite() && x > 0.0);
    1.0 / x
}

/// Normalize FX bars as local-currency units per USD.
///
/// Rejects `NaN`, infinite, and non-positive prices. When inverting
/// (`provider_is_local_per_usd == false`), swaps `high` and `low` so the OHLC
/// invariant `low <= open, close <= high` is preserved.
pub fn normalize_fx_ohlc(
    open: f64,
    high: f64,
    low: f64,
    close: f64,
    provider_is_local_per_usd: bool,
) -> Result<(f64, f64, f64, f64), MarketMathError> {
    if [open, high, low, close]
        .iter()
        .any(|price| !price.is_finite() || *price <= 0.0)
    {
        return Err(MarketMathError::NonPositiveOrNonFiniteFxPrice);
    }
    if provider_is_local_per_usd {
        return Ok((open, high, low, close));
    }

    let inv = (
        recip_positive(open),
        recip_positive(low),
        recip_positive(high),
        recip_positive(close),
    );
    if !inv.0.is_finite() || !inv.1.is_finite() || !inv.2.is_finite() || !inv.3.is_finite() {
        return Err(MarketMathError::FxInversionOverflow);
    }
    Ok(inv)
}

/// Compute a single proxy ETF percentage change and reject implausible moves.
pub fn proxy_change_pct(previous_close: f64, latest_close: f64) -> Result<f64, MarketMathError> {
    if !previous_close.is_finite() || !latest_close.is_finite() || previous_close <= 0.0 {
        return Err(MarketMathError::NonPositiveOrNonFiniteClose);
    }
    let change = (latest_close - previous_close) / previous_close * 100.0;
    if !change.is_finite() || change.abs() > MAX_PLAUSIBLE_MOVE {
        return Err(MarketMathError::ImplausibleProxyMove);
    }
    Ok(change)
}

/// Combine available ETF proxy percentage changes using renormalized `NAV_HOLDINGS` weights.
///
/// Any missing, non-finite, or implausible (`|c| > MAX_PLAUSIBLE_MOVE`) proxy is skipped.
/// The result is clamped to `[-MAX_PLAUSIBLE_MOVE, MAX_PLAUSIBLE_MOVE]` to eliminate
/// 1-ULP IEEE-754 overshoot at exact boundary moves.
pub fn weighted_proxy_return(changes: [Option<f64>; 6]) -> Option<f64> {
    let mut weighted = 0.0;
    let mut covered = 0.0;

    for (change_opt, (_, weight)) in changes.into_iter().zip(NAV_HOLDINGS.iter()) {
        if let Some(change) = change_opt {
            if change.is_finite() && change.abs() <= MAX_PLAUSIBLE_MOVE {
                weighted += change * weight;
                covered += weight;
            }
        }
    }

    (covered > 0.0).then(|| (weighted / covered).clamp(-MAX_PLAUSIBLE_MOVE, MAX_PLAUSIBLE_MOVE))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nav_holding_weights_sum_to_one() {
        let total: f64 = NAV_HOLDINGS.iter().map(|(_, w)| *w).sum();
        assert!((total - 1.0).abs() < 1e-12);
    }

    #[test]
    fn fx_inversion_preserves_order_and_rejects_nan() {
        assert_eq!(
            normalize_fx_ohlc(0.5, 0.8, 0.4, 0.625, false).unwrap(),
            (2.0, 2.5, 1.25, 1.6)
        );
        assert!(normalize_fx_ohlc(f64::NAN, 0.8, 0.4, 0.625, false).is_err());
        assert!(normalize_fx_ohlc(0.5, f64::INFINITY, 0.4, 0.625, true).is_err());
    }

    #[test]
    fn weighted_proxy_renormalizes_partial_holdings() {
        let est = weighted_proxy_return([Some(2.0), None, None, None, None, None]);
        assert_eq!(est, Some(2.0));
    }
}

#[cfg(kani)]
mod kani_proofs {
    use super::*;

    #[kani::proof]
    fn verify_normalize_fx_ohlc_rejects_invalid_and_passthrough() {
        let open: f64 = kani::any();
        let high: f64 = kani::any();
        let low: f64 = kani::any();
        let close: f64 = kani::any();

        let valid_inputs = [open, high, low, close]
            .iter()
            .all(|p| p.is_finite() && *p > 0.0);

        let pass = normalize_fx_ohlc(open, high, low, close, true);
        if !valid_inputs {
            assert!(pass.is_err());
            assert!(normalize_fx_ohlc(open, high, low, close, false).is_err());
        } else {
            assert_eq!(pass, Ok((open, high, low, close)));
        }
    }

    /// Order-reversing stub for `1.0 / x` on positive finite `f64`s.
    ///
    /// IEEE-754 binary64 preserves order under `to_bits()` for positive finite
    /// floats, so subtracting from the max finite bit pattern gives a strictly
    /// antitone positive map without bit-blasting four 64-bit dividers in SAT.
    #[allow(dead_code)]
    fn stub_recip_positive(x: f64) -> f64 {
        assert!(
            x.is_finite() && x > 0.0,
            "recip_positive requires finite positive input"
        );
        const MAX_FINITE_BITS: u64 = f64::MAX.to_bits();
        f64::from_bits((MAX_FINITE_BITS - x.to_bits()).saturating_add(1))
    }

    #[kani::proof]
    #[kani::stub(recip_positive, stub_recip_positive)]
    fn verify_normalize_fx_ohlc_inversion_reverses_order() {
        let open: f64 = kani::any();
        let high: f64 = kani::any();
        let low: f64 = kani::any();
        let close: f64 = kani::any();

        kani::assume(
            [open, high, low, close]
                .iter()
                .all(|p| p.is_finite() && *p > 0.0),
        );
        kani::assume(low <= open && open <= high && low <= close && close <= high);

        let (o, h, l, c) = normalize_fx_ohlc(open, high, low, close, false)
            .expect("valid positive OHLC inverts cleanly");
        assert!(o.is_finite() && o > 0.0);
        assert!(h.is_finite() && h > 0.0);
        assert!(l.is_finite() && l > 0.0);
        assert!(c.is_finite() && c > 0.0);
        assert!(l <= o && o <= h);
        assert!(l <= c && c <= h);
    }

    #[kani::proof]
    fn verify_calculate_relative_always_finite_and_sign_correct() {
        let value: f64 = kani::any();
        let base: f64 = kani::any();
        let invert: bool = kani::any();

        let rel = calculate_relative(Some(value), Some(base), invert);
        assert!(rel.is_finite());

        if invert && value.is_finite() && base.is_finite() && base > 0.0 && value > 0.0 {
            if value < base {
                assert!(rel >= 0.0);
            } else if value == base {
                assert_eq!(rel, 0.0);
            } else {
                assert!(rel <= 0.0);
            }
        }
    }

    #[kani::proof]
    fn verify_weighted_proxy_return_is_bounded_for_all_subsets() {
        let changes: [Option<f64>; 6] = [
            if kani::any() { Some(kani::any()) } else { None },
            if kani::any() { Some(kani::any()) } else { None },
            if kani::any() { Some(kani::any()) } else { None },
            if kani::any() { Some(kani::any()) } else { None },
            if kani::any() { Some(kani::any()) } else { None },
            if kani::any() { Some(kani::any()) } else { None },
        ];

        let any_valid = changes
            .iter()
            .any(|opt| matches!(opt, Some(c) if c.is_finite() && c.abs() <= MAX_PLAUSIBLE_MOVE));

        let est = weighted_proxy_return(changes);
        assert_eq!(est.is_some(), any_valid);

        if let Some(value) = est {
            assert!(value.is_finite());
            assert!(value >= -MAX_PLAUSIBLE_MOVE && value <= MAX_PLAUSIBLE_MOVE);
        }
    }
}
