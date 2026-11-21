//! Leakage-free temporal alignment, MACD, percentile, and correlation math.

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct MacdPoint {
    pub macd: Option<f64>,
    pub signal: Option<f64>,
    pub hist: Option<f64>,
}

/// Qualitative correlation strength bucket.
pub fn correlation_strength(correlation: Option<f64>) -> Option<&'static str> {
    let val = correlation?;
    if !val.is_finite() {
        return None;
    }
    let absolute = val.abs();
    Some(if absolute < 0.20 {
        "weak"
    } else if absolute < 0.40 {
        "moderate"
    } else {
        "strong"
    })
}

/// Find the `lag`-th strictly later stock close index for a single factor close
/// timestamp (`stock_ends[idx] > factor_end`).
///
/// Precondition: `stock_ends` is sorted in non-decreasing order.
#[inline]
pub fn align_single_factor(factor_end: i64, stock_ends: &[i64], lag: usize) -> Option<usize> {
    let first_later = stock_ends.partition_point(|stock_end| *stock_end <= factor_end);
    let target = first_later.checked_add(lag)?;
    (target < stock_ends.len()).then_some(target)
}

/// Pair each factor close timestamp with the `lag`-th strictly later stock close
/// index (`stock_ends[stock_idx] > factor_ends[factor_idx]`).
///
/// Precondition: `stock_ends` is sorted in non-decreasing order.
pub fn align_lagged_indices(
    factor_ends: &[i64],
    stock_ends: &[i64],
    lag: usize,
) -> Vec<(usize, usize)> {
    let mut pairs = Vec::with_capacity(factor_ends.len());
    for (factor_idx, &factor_end) in factor_ends.iter().enumerate() {
        if let Some(target) = align_single_factor(factor_end, stock_ends, lag) {
            pairs.push((factor_idx, target));
        }
    }
    pairs
}

/// Find `(before_idx, after_idx)` strictly bracketing `occurred_at` in a sorted
/// timestamp slice: `sorted_ends[before_idx] < occurred_at < sorted_ends[after_idx]`.
///
/// Precondition: `sorted_ends` is sorted in non-decreasing order.
pub fn next_close_bracket(sorted_ends: &[i64], occurred_at: i64) -> Option<(usize, usize)> {
    let before_index = sorted_ends.partition_point(|&ts| ts < occurred_at);
    let after_index = sorted_ends.partition_point(|&ts| ts <= occurred_at);
    if before_index == 0 || after_index >= sorted_ends.len() {
        return None;
    }
    Some((before_index - 1, after_index))
}

/// Nearest-rank percentile over a sorted slice, panic-free for all `f64` fractions.
pub fn percentile(sorted: &[f64], fraction: f64) -> f64 {
    if sorted.is_empty() || !fraction.is_finite() {
        return 0.0;
    }
    let clamped = fraction.clamp(0.0, 1.0);
    let max_idx = sorted.len() - 1;
    let index = ((max_idx as f64) * clamped).floor() as usize;
    sorted[index.min(max_idx)]
}

pub fn sma(seq: &[Option<f64>], i: usize, period: usize) -> Option<f64> {
    if period == 0 || i + 1 < period || i >= seq.len() {
        return None;
    }
    let window = &seq[i + 1 - period..=i];
    let mut sum = 0.0;
    for &val in window {
        match val {
            Some(v) if v.is_finite() => sum += v,
            _ => return None,
        }
    }
    let mean = sum / period as f64;
    mean.is_finite().then_some(mean)
}

/// Compute carry-forward sentiment MACD `(fast SMA - slow SMA, signal SMA, histogram)`.
pub fn compute_sentiment_macd(
    indices: &[Option<f64>],
    fast: usize,
    slow: usize,
    signal: usize,
) -> Vec<MacdPoint> {
    let n = indices.len();
    if n == 0 {
        return Vec::new();
    }

    let mut filled = Vec::with_capacity(n);
    let mut last = None;
    for &v in indices {
        if v.is_some() {
            last = v;
        }
        filled.push(last);
    }

    let mut macd_line = Vec::with_capacity(n);
    for i in 0..n {
        let f = sma(&filled, i, fast);
        let s = sma(&filled, i, slow);
        macd_line.push(match (f, s) {
            (Some(f_val), Some(s_val)) => {
                let diff = f_val - s_val;
                diff.is_finite().then_some(diff)
            }
            _ => None,
        });
    }

    let mut out = Vec::with_capacity(n);
    for i in 0..n {
        let sig = sma(&macd_line, i, signal);
        let m = macd_line[i];
        let hist = match (m, sig) {
            (Some(m_val), Some(sig_val)) => {
                let diff = m_val - sig_val;
                diff.is_finite().then_some(diff)
            }
            _ => None,
        };
        out.push(MacdPoint {
            macd: m,
            signal: sig,
            hist,
        });
    }
    out
}

/// Square root restricted to finite non-negative inputs (`x >= 0.0`).
#[inline]
pub fn sqrt_nonneg(x: f64) -> f64 {
    debug_assert!(x.is_finite() && x >= 0.0);
    x.sqrt()
}

/// Compute Pearson correlation (clamped to `[-1.0, 1.0]`) and OLS beta (`cov / var_x`).
pub fn pearson_and_beta(
    pairs: &[(f64, f64)],
    minimum_samples: usize,
) -> (Option<f64>, Option<f64>) {
    if pairs.len() < minimum_samples.max(2) {
        return (None, None);
    }
    if pairs.iter().any(|(x, y)| !x.is_finite() || !y.is_finite()) {
        return (None, None);
    }

    let count = pairs.len() as f64;
    let factor_mean = pairs.iter().map(|(factor, _)| factor).sum::<f64>() / count;
    let stock_mean = pairs.iter().map(|(_, stock)| stock).sum::<f64>() / count;
    if !factor_mean.is_finite() || !stock_mean.is_finite() {
        return (None, None);
    }

    let factor_ss = pairs
        .iter()
        .map(|(factor, _)| (factor - factor_mean).powi(2))
        .sum::<f64>();
    let stock_ss = pairs
        .iter()
        .map(|(_, stock)| (stock - stock_mean).powi(2))
        .sum::<f64>();
    if !factor_ss.is_finite() || !stock_ss.is_finite() || factor_ss <= 0.0 || stock_ss <= 0.0 {
        return (None, None);
    }

    let covariance = pairs
        .iter()
        .map(|(factor, stock)| (factor - factor_mean) * (stock - stock_mean))
        .sum::<f64>();
    if !covariance.is_finite() {
        return (None, None);
    }

    let prod = factor_ss * stock_ss;
    let correlation = if prod.is_finite() && prod > 0.0 {
        let denom = sqrt_nonneg(prod);
        if denom.is_finite() && denom > f64::EPSILON {
            let r = covariance / denom;
            r.is_finite().then_some(r.clamp(-1.0, 1.0))
        } else {
            None
        }
    } else {
        None
    };

    let beta_val = covariance / factor_ss;
    let beta = beta_val.is_finite().then_some(beta_val);

    (correlation, beta)
}

/// Pearson correlation with minimum 3 observations (used by API correlation sweeps).
pub fn pearson(pairs: &[(f64, f64)]) -> Option<f64> {
    pearson_and_beta(pairs, 3).0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn alignment_never_looks_backward() {
        let factors = [10, 20, 30];
        let stocks = [5, 10, 15, 25, 35];
        assert_eq!(
            align_lagged_indices(&factors, &stocks, 0),
            vec![(0, 2), (1, 3), (2, 4)]
        );
        assert_eq!(
            align_lagged_indices(&factors, &stocks, 1),
            vec![(0, 3), (1, 4)]
        );
    }

    #[test]
    fn bracket_strictly_surrounds_event() {
        let closes = [100, 200, 300];
        assert_eq!(next_close_bracket(&closes, 150), Some((0, 1)));
        assert_eq!(next_close_bracket(&closes, 200), Some((0, 2)));
        assert_eq!(next_close_bracket(&closes, 100), None);
        assert_eq!(next_close_bracket(&closes, 300), None);
    }

    #[test]
    fn percentile_clamps_out_of_range_fractions() {
        let sorted = [1.0, 2.0, 3.0, 4.0];
        assert_eq!(percentile(&sorted, 0.5), 2.0);
        assert_eq!(percentile(&sorted, 1.5), 4.0);
        assert_eq!(percentile(&sorted, -0.5), 1.0);
        assert_eq!(percentile(&sorted, f64::NAN), 0.0);
    }

    #[test]
    fn macd_handles_zero_period_without_panic() {
        let res = compute_sentiment_macd(&[Some(0.1), Some(0.2)], 0, 0, 0);
        assert_eq!(res.len(), 2);
        assert!(res
            .iter()
            .all(|p| p.macd.is_none() && p.signal.is_none() && p.hist.is_none()));
    }

    #[test]
    fn sqrt_nonneg_satisfies_kani_stub_contract() {
        assert_eq!(sqrt_nonneg(0.0), 0.0);
        for x in [1e-12_f64, 0.25, 1.0, 4.0, 1e6, 1e100] {
            let y = sqrt_nonneg(x);
            assert!(y.is_finite() && y >= 0.0);
        }
    }
}

#[cfg(kani)]
mod kani_proofs {
    use super::*;

    #[kani::proof]
    fn verify_align_single_factor_strictly_prevents_lookahead() {
        let factor_end: i64 = kani::any();
        let stocks: [i64; 6] = [
            kani::any(),
            kani::any(),
            kani::any(),
            kani::any(),
            kani::any(),
            kani::any(),
        ];
        let lag: usize = kani::any();

        // Precondition: stock timestamps are sorted non-decreasingly.
        kani::assume(
            stocks[0] <= stocks[1]
                && stocks[1] <= stocks[2]
                && stocks[2] <= stocks[3]
                && stocks[3] <= stocks[4]
                && stocks[4] <= stocks[5],
        );

        if let Some(si) = align_single_factor(factor_end, &stocks, lag) {
            assert!(si < stocks.len());
            // Zero-lookahead theorem: stock close is strictly after factor close.
            assert!(factor_end < stocks[si]);
        }
    }

    #[kani::proof]
    fn verify_next_close_bracket_strictly_brackets_event() {
        let closes: [i64; 4] = [kani::any(), kani::any(), kani::any(), kani::any()];
        let occurred_at: i64 = kani::any();

        kani::assume(closes[0] <= closes[1] && closes[1] <= closes[2] && closes[2] <= closes[3]);

        if let Some((bi, ai)) = next_close_bracket(&closes, occurred_at) {
            assert!(bi < ai);
            assert!(ai < closes.len());
            assert!(closes[bi] < occurred_at);
            assert!(occurred_at < closes[ai]);
        }
    }

    #[kani::proof]
    fn verify_percentile_panic_free_and_within_range() {
        let sorted: [f64; 4] = [kani::any(), kani::any(), kani::any(), kani::any()];
        let fraction: f64 = kani::any();

        kani::assume(sorted.iter().all(|v| v.is_finite()));
        kani::assume(sorted[0] <= sorted[1] && sorted[1] <= sorted[2] && sorted[2] <= sorted[3]);

        let result = percentile(&sorted, fraction);
        if fraction.is_finite() {
            assert!(result >= sorted[0] && result <= sorted[3]);
        } else {
            assert_eq!(result, 0.0);
        }
    }

    #[kani::proof]
    #[kani::unwind(6)]
    fn verify_sma_panic_free_and_finite() {
        let seq: [Option<f64>; 4] = [
            if kani::any() { Some(kani::any()) } else { None },
            if kani::any() { Some(kani::any()) } else { None },
            if kani::any() { Some(kani::any()) } else { None },
            if kani::any() { Some(kani::any()) } else { None },
        ];
        let i: usize = kani::any();
        let period: usize = kani::any();
        kani::assume(i <= 5 && period <= 5);

        if let Some(mean) = sma(&seq, i, period) {
            assert!(period > 0 && i < seq.len() && i + 1 >= period);
            assert!(mean.is_finite());
        }
    }

    #[kani::proof]
    #[kani::unwind(6)]
    fn verify_sentiment_macd_structural_invariant() {
        let indices: [Option<f64>; 4] = [
            if kani::any() { Some(kani::any()) } else { None },
            if kani::any() { Some(kani::any()) } else { None },
            if kani::any() { Some(kani::any()) } else { None },
            if kani::any() { Some(kani::any()) } else { None },
        ];

        let out = compute_sentiment_macd(&indices, 2, 3, 2);
        assert_eq!(out.len(), indices.len());
        for pt in out {
            if let Some(m) = pt.macd {
                assert!(m.is_finite());
            }
            if let Some(s) = pt.signal {
                assert!(s.is_finite());
            }
            if let Some(h) = pt.hist {
                assert!(h.is_finite());
                assert!(pt.macd.is_some() && pt.signal.is_some());
            }
        }
    }

    #[allow(dead_code)]
    fn stub_sqrt_nonneg(x: f64) -> f64 {
        assert!(
            x.is_finite() && x >= 0.0,
            "sqrt_nonneg requires finite non-negative input"
        );
        if x == 0.0 {
            0.0
        } else {
            let y: f64 = kani::any();
            kani::assume(y.is_finite() && y > 0.0);
            y
        }
    }

    #[kani::proof]
    #[kani::unwind(5)]
    fn verify_pearson_and_beta_rejects_short_or_non_finite() {
        let pairs: [(f64, f64); 2] = [(kani::any(), kani::any()), (kani::any(), kani::any())];
        // Fewer than minimum_samples (3) always returns (None, None).
        assert_eq!(pearson_and_beta(&pairs, 3), (None, None));

        // Any non-finite entry always returns (None, None).
        let bad = [(f64::NAN, 1.0), (2.0, 3.0), (4.0, 5.0)];
        assert_eq!(pearson_and_beta(&bad, 3), (None, None));
    }

    #[kani::proof]
    #[kani::unwind(5)]
    #[kani::stub(sqrt_nonneg, stub_sqrt_nonneg)]
    fn verify_pearson_and_beta_bounds_and_domain_safety() {
        let x0: f32 = kani::any();
        let y0: f32 = kani::any();
        let x1: f32 = kani::any();
        let y1: f32 = kani::any();
        let pairs = [
            (f64::from(x0), f64::from(y0)),
            (f64::from(x1), f64::from(y1)),
        ];

        let (r_opt, beta_opt) = pearson_and_beta(&pairs, 2);
        if let Some(r) = r_opt {
            assert!(r.is_finite());
            assert!((-1.0..=1.0).contains(&r));
        }
        if let Some(b) = beta_opt {
            assert!(b.is_finite());
        }
    }
}
