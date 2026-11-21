//! Finite-sample confidence bounds for bounded sentiment signals (`s in [-1, 1]`).
//!
//! Implements distribution-free, non-asymptotic confidence intervals from the
//! additive sufficient statistics `(post_count, signal_sum, signal_sq_sum)`
//! already stored in `hourly_sentiment_facts` and computed from live `posts`:
//!
//! - **Hoeffding's inequality** (valid for all `n >= 1`):
//!   `half_width = sqrt(2 * ln(2 / alpha) / n)`
//! - **Empirical Bernstein bound** (Maurer & Pontil 2009, Theorem 4, scaled to
//!   range `R = 2` for `s in [-1, 1]`, valid for all `n >= 2`):
//!   `half_width = sqrt(2 * V_n * ln(4 / alpha) / n) + 14 * ln(4 / alpha) / (3 * (n - 1))`
//! - **Exact union-bound hybrid** (`n >= 2`): evaluates both bounds at `alpha / 2`
//!   and takes `min(hoeffding, empirical_bernstein)`, preserving exact `>= 1 - alpha`
//!   finite-sample coverage without data-dependent selection bias while automatically
//!   tightening when sample variance is small.

use crate::timeseries::sqrt_nonneg;

/// Default significance level (`alpha = 0.05` -> 95% confidence interval).
pub const DEFAULT_ALPHA: f64 = 0.05;

/// Popoviciu upper bound on the unbiased sample variance of `s in [-1, 1]`
/// (achieved at `n = 2` with `{-1, +1}`: `V_2 = 2.0`).
pub const MAX_BOUNDED_SAMPLE_VARIANCE: f64 = 2.0;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BoundMethod {
    Hoeffding,
    EmpiricalBernstein,
}

impl BoundMethod {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Hoeffding => "hoeffding",
            Self::EmpiricalBernstein => "empirical_bernstein",
        }
    }
}

/// Finite-sample `(1 - alpha)` confidence interval for mean sentiment in `[-1, 1]`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ConfidenceInterval {
    pub mean: f64,
    pub lower: f64,
    pub upper: f64,
    pub half_width: f64,
    pub sample_count: u64,
    pub sample_variance: Option<f64>,
    pub method: BoundMethod,
    /// `true` iff the confidence interval strictly excludes `0.0` (`lower > 0` or `upper < 0`).
    pub significant_nonzero: bool,
}

/// Natural logarithm restricted to finite inputs strictly greater than `1.0`
/// (guaranteeing a strictly positive finite result).
#[inline]
pub fn ln_gt_one(x: f64) -> f64 {
    debug_assert!(x.is_finite() && x > 1.0);
    x.ln()
}

/// Compute unbiased sample variance from sufficient statistics `(n, sum, sq_sum)`
/// for signals bounded in `[-1, 1]`, clamped to `[0.0, MAX_BOUNDED_SAMPLE_VARIANCE]`.
pub fn unbiased_sample_variance_bounded(n: u64, sum: f64, sq_sum: f64) -> Option<f64> {
    if n < 2 || !sum.is_finite() || !sq_sum.is_finite() || sq_sum < 0.0 {
        return None;
    }
    let nf = n as f64;
    let sum_sq_over_n = (sum * sum) / nf;
    if !sum_sq_over_n.is_finite() {
        return None;
    }
    let ss = (sq_sum - sum_sq_over_n).max(0.0);
    let var = ss / (nf - 1.0);
    var.is_finite()
        .then_some(var.clamp(0.0, MAX_BOUNDED_SAMPLE_VARIANCE))
}

/// Two-sided Hoeffding half-width for `n >= 1` bounded observations in `[-1, 1]` (`R = 2`).
pub fn hoeffding_half_width(n: u64, alpha: f64) -> Option<f64> {
    if n == 0 || !alpha.is_finite() || alpha <= 0.0 || alpha >= 1.0 {
        return None;
    }
    let ratio = 2.0 / alpha;
    if !ratio.is_finite() || ratio <= 1.0 {
        return None;
    }
    let log_term = ln_gt_one(ratio);
    let rad = (2.0 * log_term) / (n as f64);
    if !rad.is_finite() || rad < 0.0 {
        return None;
    }
    let hw = sqrt_nonneg(rad);
    hw.is_finite().then_some(hw)
}

/// Two-sided Maurer & Pontil (2009) Empirical Bernstein half-width for `n >= 2`
/// bounded observations in `[-1, 1]` (`R = 2`).
pub fn empirical_bernstein_half_width(n: u64, sample_variance: f64, alpha: f64) -> Option<f64> {
    if n < 2
        || !sample_variance.is_finite()
        || sample_variance < 0.0
        || !alpha.is_finite()
        || alpha <= 0.0
        || alpha >= 1.0
    {
        return None;
    }
    let ratio = 4.0 / alpha;
    if !ratio.is_finite() || ratio <= 1.0 {
        return None;
    }
    let log_term = ln_gt_one(ratio);
    let nf = n as f64;
    let rad = (2.0 * sample_variance * log_term) / nf;
    let linear = (14.0 * log_term) / (3.0 * (nf - 1.0));
    if !rad.is_finite() || rad < 0.0 || !linear.is_finite() || linear < 0.0 {
        return None;
    }
    let hw = sqrt_nonneg(rad) + linear;
    hw.is_finite().then_some(hw)
}

/// Build a bounded `[-1, 1]` confidence interval from a finite mean and non-negative half-width.
#[inline]
pub fn build_bounded_interval(
    mean: f64,
    half_width: f64,
    sample_count: u64,
    sample_variance: Option<f64>,
    method: BoundMethod,
) -> Option<ConfidenceInterval> {
    if sample_count == 0 || !mean.is_finite() || !half_width.is_finite() || half_width < 0.0 {
        return None;
    }
    let clamped_mean = mean.clamp(-1.0, 1.0);
    let lower = (clamped_mean - half_width).clamp(-1.0, 1.0);
    let upper = (clamped_mean + half_width).clamp(-1.0, 1.0);
    let significant_nonzero = lower > 0.0 || upper < 0.0;
    Some(ConfidenceInterval {
        mean: clamped_mean,
        lower,
        upper,
        half_width,
        sample_count,
        sample_variance,
        method,
        significant_nonzero,
    })
}

/// Compute a finite-sample `(1 - alpha)` confidence interval for the mean sentiment
/// of `n` posts with bounded signals `s_i in [-1, 1]`.
///
/// - For `n == 1`, applies Hoeffding at `alpha`.
/// - For `n >= 2`, applies the exact union-bound hybrid `min(Hoeffding(alpha/2), EB(alpha/2))`
///   so coverage stays provably `>= 1 - alpha` with no data-snooping penalty.
pub fn sentiment_confidence_interval(
    n: u64,
    signal_sum: f64,
    signal_sq_sum: f64,
    alpha: f64,
) -> Option<ConfidenceInterval> {
    if n == 0
        || !signal_sum.is_finite()
        || !signal_sq_sum.is_finite()
        || signal_sq_sum < 0.0
        || !alpha.is_finite()
        || alpha <= 0.0
        || alpha >= 1.0
    {
        return None;
    }

    let mean = signal_sum / (n as f64);
    if !mean.is_finite() {
        return None;
    }

    if n == 1 {
        let hw = hoeffding_half_width(1, alpha)?;
        return build_bounded_interval(mean, hw, 1, None, BoundMethod::Hoeffding);
    }

    let half_alpha = alpha * 0.5;
    let hoeff = hoeffding_half_width(n, half_alpha)?;
    let var = unbiased_sample_variance_bounded(n, signal_sum, signal_sq_sum)?;
    let eb = empirical_bernstein_half_width(n, var, half_alpha)?;

    let (hw, method) = if eb < hoeff {
        (eb, BoundMethod::EmpiricalBernstein)
    } else {
        (hoeff, BoundMethod::Hoeffding)
    };

    build_bounded_interval(mean, hw, n, Some(var), method)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn single_post_is_never_statistically_significant() {
        let ci = sentiment_confidence_interval(1, 1.0, 1.0, DEFAULT_ALPHA).expect("valid n=1");
        assert_eq!(ci.mean, 1.0);
        assert_eq!(ci.lower, -1.0);
        assert_eq!(ci.upper, 1.0);
        assert_eq!(ci.method, BoundMethod::Hoeffding);
        assert!(!ci.significant_nonzero);
    }

    #[test]
    fn high_volume_consensus_triggers_empirical_bernstein_and_significance() {
        // 300 posts all at +0.75 -> zero variance -> Empirical Bernstein beats Hoeffding
        let n = 300_u64;
        let sum = 300.0 * 0.75;
        let sq_sum = 300.0 * 0.75 * 0.75;
        let ci = sentiment_confidence_interval(n, sum, sq_sum, DEFAULT_ALPHA).expect("valid");
        assert_eq!(ci.method, BoundMethod::EmpiricalBernstein);
        assert!(ci.significant_nonzero);
        assert!(ci.lower > 0.5);
        assert!(ci.lower <= ci.mean && ci.mean <= ci.upper);
    }

    #[test]
    fn low_volume_noisy_sentiment_is_not_significant() {
        // 8 posts: 5 positive (+1), 3 negative (-1) -> mean = +0.25, high variance
        let ci = sentiment_confidence_interval(8, 2.0, 8.0, DEFAULT_ALPHA).expect("valid");
        assert!(!ci.significant_nonzero);
        assert!(ci.lower < 0.0 && ci.upper > 0.0);
    }

    #[test]
    fn rejects_invalid_counts_and_alpha() {
        assert!(sentiment_confidence_interval(0, 0.0, 0.0, DEFAULT_ALPHA).is_none());
        assert!(sentiment_confidence_interval(10, 1.0, 1.0, 0.0).is_none());
        assert!(sentiment_confidence_interval(10, 1.0, 1.0, 1.0).is_none());
        assert!(sentiment_confidence_interval(10, 1.0, 1.0, f64::NAN).is_none());
        assert!(sentiment_confidence_interval(10, 1.0, -0.5, DEFAULT_ALPHA).is_none());
    }

    #[test]
    fn ln_gt_one_satisfies_kani_stub_contract() {
        for x in [1.0001_f64, 2.0, 40.0, 80.0, 1e6, 1e100] {
            let y = ln_gt_one(x);
            assert!(y.is_finite() && y > 0.0);
        }
    }
}

#[cfg(kani)]
mod kani_proofs {
    use super::*;

    #[allow(dead_code)]
    fn stub_sqrt_nonneg(x: f64) -> f64 {
        assert!(x.is_finite() && x >= 0.0);
        if x == 0.0 {
            0.0
        } else {
            let y: f64 = kani::any();
            kani::assume(y.is_finite() && y > 0.0);
            y
        }
    }

    #[allow(dead_code)]
    fn stub_ln_gt_one(x: f64) -> f64 {
        assert!(x.is_finite() && x > 1.0);
        let y: f64 = kani::any();
        kani::assume(y.is_finite() && y > 0.0);
        y
    }

    #[kani::proof]
    fn verify_build_bounded_interval_containment_and_significance() {
        let mean: f64 = kani::any();
        let half_width: f64 = kani::any();
        let n: u64 = kani::any();
        let method = if kani::any() {
            BoundMethod::Hoeffding
        } else {
            BoundMethod::EmpiricalBernstein
        };

        let result = build_bounded_interval(mean, half_width, n, None, method);
        if n == 0 || !mean.is_finite() || !half_width.is_finite() || half_width < 0.0 {
            assert!(result.is_none());
            return;
        }

        let ci = result.expect("valid interval inputs must succeed");
        assert!(ci.mean.is_finite() && (-1.0..=1.0).contains(&ci.mean));
        assert!(ci.lower.is_finite() && (-1.0..=1.0).contains(&ci.lower));
        assert!(ci.upper.is_finite() && (-1.0..=1.0).contains(&ci.upper));
        assert!(ci.lower <= ci.mean && ci.mean <= ci.upper);
        assert_eq!(ci.significant_nonzero, ci.lower > 0.0 || ci.upper < 0.0);

        // Directional consistency: significant positive interval never crosses <= 0,
        // and significant negative interval never crosses >= 0.
        if ci.significant_nonzero && ci.mean > 0.0 {
            assert!(ci.lower > 0.0);
        }
        if ci.significant_nonzero && ci.mean < 0.0 {
            assert!(ci.upper < 0.0);
        }
        if ci.mean == 0.0 {
            assert!(!ci.significant_nonzero);
        }
    }

    #[kani::proof]
    fn verify_unbiased_sample_variance_bounds() {
        let n: u64 = kani::any();
        let sum_f32: f32 = kani::any();
        let sq_sum_f32: f32 = kani::any();

        let res = unbiased_sample_variance_bounded(n, f64::from(sum_f32), f64::from(sq_sum_f32));
        if n < 2 || !sum_f32.is_finite() || !sq_sum_f32.is_finite() || sq_sum_f32 < 0.0 {
            assert!(res.is_none());
            return;
        }

        if let Some(var) = res {
            assert!(var.is_finite());
            assert!((0.0..=MAX_BOUNDED_SAMPLE_VARIANCE).contains(&var));
        }
    }

    #[kani::proof]
    #[kani::stub(sqrt_nonneg, stub_sqrt_nonneg)]
    #[kani::stub(ln_gt_one, stub_ln_gt_one)]
    fn verify_hoeffding_and_empirical_bernstein_domain_safety() {
        let n: u64 = kani::any();
        let alpha_f32: f32 = kani::any();
        let var_f32: f32 = kani::any();
        let alpha = f64::from(alpha_f32);
        let var = f64::from(var_f32);

        let h = hoeffding_half_width(n, alpha);
        if n == 0 || !alpha.is_finite() || !(alpha > 0.0 && alpha < 1.0) {
            assert!(h.is_none());
        } else if let Some(hw) = h {
            assert!(hw.is_finite() && hw >= 0.0);
        }

        let eb = empirical_bernstein_half_width(n, var, alpha);
        if n < 2
            || !var.is_finite()
            || var < 0.0
            || !alpha.is_finite()
            || !(alpha > 0.0 && alpha < 1.0)
        {
            assert!(eb.is_none());
        } else if let Some(hw) = eb {
            assert!(hw.is_finite() && hw >= 0.0);
        }
    }
}
