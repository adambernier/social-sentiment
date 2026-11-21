//! Split conformal prediction, Adaptive Conformal Inference (ACI), and
//! Horvitz–Thompson debiasing — formally verified with Kani.
//!
//! References:
//! - Vovk, Gammerman, Shafer (2005); Lei et al. (2018): finite-sample split conformal
//!   coverage `P(Y in C(X)) >= 1 - alpha` under exchangeability.
//! - Gibbs & Candès (2021): Adaptive Conformal Inference (ACI) for distribution shift.
//! - Horvitz & Thompson (1952) / Hájek (1971): inverse-probability-weighted debiasing
//!   from `raw_post_archive_staging` (`cohort = 'probability'`).

use crate::postprocess::{select_topic_index, SentimentLabel};

/// Compute the 1-based split-conformal order-statistic rank `ceil((n + 1) * (1 - alpha))`.
///
/// Returns `None` if `n == 0` or `alpha` is not strictly inside `(0, 1)`.
/// Returns `n + 1` when `(n + 1) * (1 - alpha) > n`, signalling that `n` is too
/// small for a non-trivial quantile and the caller must fall back to `q_hat = 1.0`.
pub fn split_conformal_rank(n: usize, alpha: f64) -> Option<usize> {
    if n == 0 || !alpha.is_finite() || alpha <= 0.0 || alpha >= 1.0 {
        return None;
    }
    let level = ((n as f64) + 1.0) * (1.0 - alpha);
    if !level.is_finite() {
        return None;
    }
    let ceil_val = level.ceil();
    if ceil_val <= 1.0 {
        Some(1)
    } else if ceil_val > (n as f64) {
        Some(n + 1)
    } else {
        Some(ceil_val as usize)
    }
}

/// Exact finite-sample split-conformal quantile `q_hat` over a sorted non-decreasing
/// slice of bounded nonconformity scores in `[0.0, 1.0]` (e.g. `1 - p(y_true | x)`).
///
/// - Precondition: `sorted_scores` is sorted in non-decreasing order with all elements
///   finite in `[0.0, 1.0]`.
/// - When `ceil((n + 1) * (1 - alpha)) > n`, returns `Some(1.0)` (vacuous full-set
///   threshold) so finite-sample coverage `>= 1 - alpha` is never violated silently.
pub fn split_conformal_quantile(sorted_scores: &[f64], alpha: f64) -> Option<f64> {
    if sorted_scores.is_empty()
        || sorted_scores
            .iter()
            .any(|s| !s.is_finite() || !(0.0..=1.0).contains(s))
    {
        return None;
    }
    let rank = split_conformal_rank(sorted_scores.len(), alpha)?;
    if rank > sorted_scores.len() {
        Some(1.0)
    } else {
        Some(sorted_scores[rank - 1])
    }
}

/// Conformal prediction set over the 3 FinTwitBERT sentiment classes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SentimentPredictionSet {
    pub neutral: bool,
    pub positive: bool,
    pub negative: bool,
}

impl SentimentPredictionSet {
    pub const fn len(self) -> u8 {
        (self.neutral as u8) + (self.positive as u8) + (self.negative as u8)
    }

    pub const fn is_empty(self) -> bool {
        self.len() == 0
    }

    pub const fn is_singleton(self) -> bool {
        self.len() == 1
    }

    /// Returns `true` when the conformal set does not commit to a single class
    /// (empty or ambiguous multi-class set).
    pub const fn is_ambiguous(self) -> bool {
        self.len() != 1
    }

    pub const fn singleton_label(self) -> Option<SentimentLabel> {
        match (self.neutral, self.positive, self.negative) {
            (true, false, false) => Some(SentimentLabel::Neutral),
            (false, true, false) => Some(SentimentLabel::Positive),
            (false, false, true) => Some(SentimentLabel::Negative),
            _ => None,
        }
    }

    pub const fn contains(self, label: SentimentLabel) -> bool {
        match label {
            SentimentLabel::Neutral => self.neutral,
            SentimentLabel::Positive => self.positive,
            SentimentLabel::Negative => self.negative,
        }
    }

    pub const fn is_subset_of(self, other: Self) -> bool {
        (!self.neutral || other.neutral)
            && (!self.positive || other.positive)
            && (!self.negative || other.negative)
    }
}

/// Construct the 3-class sentiment conformal prediction set from a minimum
/// class probability threshold `min_prob = 1.0 - q_hat` in `[0.0, 1.0]`.
pub fn sentiment_conformal_set_min_prob(
    probs: [f64; 3],
    min_prob: f64,
) -> Option<SentimentPredictionSet> {
    if !min_prob.is_finite() || !(0.0..=1.0).contains(&min_prob) {
        return None;
    }
    if probs
        .iter()
        .any(|p| !p.is_finite() || !(0.0..=1.0).contains(p))
    {
        return None;
    }
    Some(SentimentPredictionSet {
        neutral: probs[0] >= min_prob,
        positive: probs[1] >= min_prob,
        negative: probs[2] >= min_prob,
    })
}

/// Construct the 3-class sentiment conformal prediction set for probabilities
/// `[neutral, positive, negative]` and calibrated quantile `q_hat in [0.0, 1.0]`.
///
/// Includes class `c` iff `probs[c] >= 1.0 - q_hat` (equivalent to nonconformity
/// score `1.0 - probs[c] <= q_hat`).
pub fn sentiment_conformal_set(probs: [f64; 3], q_hat: f64) -> Option<SentimentPredictionSet> {
    if !q_hat.is_finite() || !(0.0..=1.0).contains(&q_hat) {
        return None;
    }
    sentiment_conformal_set_min_prob(probs, (1.0 - q_hat).clamp(0.0, 1.0))
}

/// Convert a calibrated topic nonconformity quantile `q_hat in [0.0, 1.0]` into
/// the minimum entailment probability threshold `1.0 - q_hat` for `select_topic_index`.
pub fn conformal_topic_threshold(q_hat: f64) -> Option<f32> {
    if !q_hat.is_finite() || !(0.0..=1.0).contains(&q_hat) {
        return None;
    }
    Some(((1.0 - q_hat).clamp(0.0, 1.0)) as f32)
}

/// Select the top topic clearing the calibrated conformal entailment threshold `1.0 - q_hat`.
pub fn select_topic_conformal(probs: &[f32], q_hat: f64) -> Option<usize> {
    let threshold = conformal_topic_threshold(q_hat)?;
    select_topic_index(probs, threshold)
}

/// Gibbs & Candès (2021) Adaptive Conformal Inference (ACI) online update:
/// `alpha_{t+1} = clamp(alpha_t + gamma * (alpha_target - err_t), alpha_min, alpha_max)`.
///
/// - `miscovered == true` (`err_t = 1`) lowers `alpha_t`, widening future sets.
/// - `miscovered == false` (`err_t = 0`) raises `alpha_t`, tightening future sets.
pub fn aci_update_alpha(
    alpha_t: f64,
    alpha_target: f64,
    gamma: f64,
    miscovered: bool,
    alpha_min: f64,
    alpha_max: f64,
) -> Option<f64> {
    if !alpha_t.is_finite()
        || !alpha_target.is_finite()
        || !gamma.is_finite()
        || !alpha_min.is_finite()
        || !alpha_max.is_finite()
        || alpha_target <= 0.0
        || alpha_target >= 1.0
        || gamma <= 0.0
        || alpha_min <= 0.0
        || alpha_max >= 1.0
        || alpha_min > alpha_max
    {
        return None;
    }

    let err_t = if miscovered { 1.0 } else { 0.0 };
    let updated = alpha_t + gamma * (alpha_target - err_t);
    if !updated.is_finite() {
        return None;
    }
    Some(updated.clamp(alpha_min, alpha_max))
}

/// Hájek self-normalized Horvitz–Thompson estimator for bounded signals `y_i in [-1, 1]`
/// sampled with known inclusion probabilities `pi_i in (0, 1]`.
///
/// Each element of `samples` is `(signal, inclusion_probability)`.
/// Returns `None` on empty input or any non-finite / out-of-range element.
pub fn horvitz_thompson_mean(samples: &[(f64, f64)]) -> Option<f64> {
    if samples.is_empty() {
        return None;
    }
    let mut weighted_num = 0.0;
    let mut weight_sum = 0.0;

    for &(signal, inclusion_prob) in samples {
        if !signal.is_finite()
            || !(-1.0..=1.0).contains(&signal)
            || !inclusion_prob.is_finite()
            || inclusion_prob <= 0.0
            || inclusion_prob > 1.0
        {
            return None;
        }
        let inv_pi = 1.0 / inclusion_prob;
        let term = signal * inv_pi;
        if !inv_pi.is_finite() || !term.is_finite() {
            return None;
        }
        weighted_num += term;
        weight_sum += inv_pi;
    }

    if !weighted_num.is_finite() || !weight_sum.is_finite() || weight_sum <= 0.0 {
        return None;
    }
    let est = weighted_num / weight_sum;
    est.is_finite().then_some(est.clamp(-1.0, 1.0))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn split_conformal_rank_and_quantile_match_exact_formula() {
        // n = 19, alpha = 0.10 => ceil(20 * 0.90) = 18 => index 17
        assert_eq!(split_conformal_rank(19, 0.10), Some(18));
        let scores: Vec<f64> = (1..=19).map(|i| i as f64 / 20.0).collect();
        assert_eq!(split_conformal_quantile(&scores, 0.10), Some(18.0 / 20.0));

        // n = 10, alpha = 0.05 => ceil(11 * 0.95) = 11 > 10 => vacuous fallback 1.0
        assert_eq!(split_conformal_rank(10, 0.05), Some(11));
        assert_eq!(split_conformal_quantile(&scores[..10], 0.05), Some(1.0));
    }

    #[test]
    fn sentiment_conformal_set_identifies_singleton_and_ambiguous_cases() {
        let confident = sentiment_conformal_set([0.05, 0.90, 0.05], 0.20).unwrap();
        assert!(confident.is_singleton());
        assert_eq!(confident.singleton_label(), Some(SentimentLabel::Positive));

        let ambiguous = sentiment_conformal_set([0.45, 0.50, 0.05], 0.60).unwrap();
        assert!(ambiguous.is_ambiguous());
        assert_eq!(ambiguous.len(), 2);
        assert!(ambiguous.neutral && ambiguous.positive && !ambiguous.negative);
    }

    #[test]
    fn aci_update_moves_in_correct_direction() {
        let tightened = aci_update_alpha(0.10, 0.10, 0.01, true, 0.01, 0.50).unwrap();
        assert!(tightened < 0.10);

        let relaxed = aci_update_alpha(0.10, 0.10, 0.01, false, 0.01, 0.50).unwrap();
        assert!(relaxed > 0.10);
    }

    #[test]
    fn horvitz_thompson_recovers_weighted_population_mean() {
        // Two strata: +1.0 sampled at p=0.10 (weight 10), -1.0 sampled at p=0.50 (weight 2)
        // Expected: (10 - 2) / (10 + 2) = 8/12 = 2/3
        let est = horvitz_thompson_mean(&[(1.0, 0.10), (-1.0, 0.50)]).unwrap();
        assert!((est - (2.0 / 3.0)).abs() < 1e-12);
    }
}

#[cfg(kani)]
mod kani_proofs {
    use super::*;

    #[kani::proof]
    #[kani::unwind(6)]
    fn verify_split_conformal_quantile_bounds_and_alpha_monotonicity() {
        let scores: [f64; 4] = [kani::any(), kani::any(), kani::any(), kani::any()];
        let alpha1: f64 = kani::any();
        let alpha2: f64 = kani::any();

        // Precondition: sorted non-decreasing scores in [0.0, 1.0].
        kani::assume(
            scores[0].is_finite()
                && scores[1].is_finite()
                && scores[2].is_finite()
                && scores[3].is_finite(),
        );
        kani::assume(
            0.0 <= scores[0]
                && scores[0] <= scores[1]
                && scores[1] <= scores[2]
                && scores[2] <= scores[3]
                && scores[3] <= 1.0,
        );

        let q1_opt = split_conformal_quantile(&scores, alpha1);
        if !alpha1.is_finite() || alpha1 <= 0.0 || alpha1 >= 1.0 {
            assert!(q1_opt.is_none());
            return;
        }

        let q1 = q1_opt.expect("valid scores and alpha must produce a quantile");
        assert!(q1.is_finite());
        assert!((0.0..=1.0).contains(&q1));
        assert!(q1 >= scores[0]);

        // Smaller alpha (stricter coverage 1 - alpha) never yields a smaller quantile.
        if alpha2.is_finite() && alpha2 > 0.0 && alpha2 < 1.0 && alpha1 <= alpha2 {
            let q2 = split_conformal_quantile(&scores, alpha2).unwrap();
            assert!(q1 >= q2);
        }
    }

    #[kani::proof]
    fn verify_sentiment_conformal_set_monotonicity_and_argmax_inclusion() {
        let probs: [f64; 3] = [kani::any(), kani::any(), kani::any()];
        let min_p1: f64 = kani::any();
        let min_p2: f64 = kani::any();

        let s1_opt = sentiment_conformal_set_min_prob(probs, min_p1);
        let valid_probs = probs[0].is_finite()
            && probs[1].is_finite()
            && probs[2].is_finite()
            && (0.0..=1.0).contains(&probs[0])
            && (0.0..=1.0).contains(&probs[1])
            && (0.0..=1.0).contains(&probs[2]);
        let valid_p1 = min_p1.is_finite() && (0.0..=1.0).contains(&min_p1);

        if !valid_probs || !valid_p1 {
            assert!(s1_opt.is_none());
            return;
        }

        let s1 = s1_opt.unwrap();
        assert!(s1.len() <= 3);

        // Vacuous quantile q_hat == 1.0 (min_prob == 0.0) always yields the full 3-class set.
        let full = sentiment_conformal_set(probs, 1.0).unwrap();
        assert_eq!(full.len(), 3);

        // If the prediction set is a singleton, that class is a true argmax of `probs`.
        if let Some(label) = s1.singleton_label() {
            let chosen = match label {
                SentimentLabel::Neutral => probs[0],
                SentimentLabel::Positive => probs[1],
                SentimentLabel::Negative => probs[2],
            };
            assert!(chosen >= probs[0] && chosen >= probs[1] && chosen >= probs[2]);
        }

        // Lower min_prob (larger q_hat) can only enlarge the prediction set (subset monotonicity).
        if min_p2.is_finite() && (0.0..=1.0).contains(&min_p2) && min_p2 <= min_p1 {
            let s2 = sentiment_conformal_set_min_prob(probs, min_p2).unwrap();
            assert!(s1.is_subset_of(s2));
            assert!(s1.len() <= s2.len());
        }
    }

    #[kani::proof]
    fn verify_aci_update_bounds_and_direction() {
        let alpha_t: f64 = kani::any();
        let alpha_target: f64 = kani::any();
        let gamma: f64 = kani::any();
        let miscovered: bool = kani::any();
        let alpha_min: f64 = kani::any();
        let alpha_max: f64 = kani::any();

        let res = aci_update_alpha(
            alpha_t,
            alpha_target,
            gamma,
            miscovered,
            alpha_min,
            alpha_max,
        );
        if let Some(next) = res {
            assert!(next.is_finite());
            assert!(next >= alpha_min && next <= alpha_max);
            assert!(next > 0.0 && next < 1.0);

            if alpha_t >= alpha_min && alpha_t <= alpha_max {
                if miscovered {
                    assert!(next <= alpha_t);
                } else {
                    assert!(next >= alpha_t);
                }
            }
        }
    }

    #[kani::proof]
    #[kani::unwind(4)]
    fn verify_horvitz_thompson_mean_bounds_and_validation() {
        let s0_f32: f32 = kani::any();
        let p0_f32: f32 = kani::any();
        let s1_f32: f32 = kani::any();
        let p1_f32: f32 = kani::any();

        let samples = [
            (f64::from(s0_f32), f64::from(p0_f32)),
            (f64::from(s1_f32), f64::from(p1_f32)),
        ];

        let valid = samples.iter().all(|&(s, p)| {
            s.is_finite() && (-1.0..=1.0).contains(&s) && p.is_finite() && p > 0.0 && p <= 1.0
        });

        let res = horvitz_thompson_mean(&samples);
        if !valid {
            assert!(res.is_none());
            return;
        }

        if let Some(est) = res {
            assert!(est.is_finite());
            assert!((-1.0..=1.0).contains(&est));
        }
    }
}
