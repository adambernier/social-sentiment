//! Numerically stable post-processing for ONNX sentiment and zero-shot topic models.

/// Canonical 3-class sentiment labels matching FinTwitBERT `id2label`:
/// `{0: neutral, 1: positive, 2: negative}`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SentimentLabel {
    Neutral,
    Positive,
    Negative,
}

impl SentimentLabel {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Neutral => "neutral",
            Self::Positive => "positive",
            Self::Negative => "negative",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct SentimentPrediction {
    pub label: SentimentLabel,
    pub neutral: f64,
    pub positive: f64,
    pub negative: f64,
}

impl SentimentPrediction {
    pub const fn probability_for(self, label: SentimentLabel) -> f64 {
        match label {
            SentimentLabel::Neutral => self.neutral,
            SentimentLabel::Positive => self.positive,
            SentimentLabel::Negative => self.negative,
        }
    }
}

/// Exponential restricted to `x <= 0.0`.
///
/// Separating this 1D primitive lets Kani prove domain safety (`x <= 0.0`) and
/// verify all downstream rational/simplex/argmax logic under a sound stub contract
/// (`exp(0.0) == 1.0`, `0.0 <= exp(x) <= 1.0`), while unit tests verify `f64::exp`
/// satisfies that contract.
#[inline]
pub fn exp_nonpositive(x: f64) -> f64 {
    debug_assert!(x <= 0.0);
    x.exp()
}

#[inline]
fn div_unit_by_sum(num: f64, denom: f64) -> f64 {
    debug_assert!(num.is_finite() && denom.is_finite());
    debug_assert!(num >= 0.0 && num <= denom && denom >= 1.0);
    num / denom
}

/// Stable 3-class softmax and canonical argmax for `[neutral, positive, negative]` logits.
///
/// - Rejects any non-finite (`NaN`/`inf`) logit by returning `None`.
/// - Promotes finite `f32` logits to `f64` before subtracting `max_l`, so all
///   exponents are finite and `<= 0.0`, `sum_e` lies in `[1.0, 3.0]`, and every
///   output probability lies strictly within `[0.0, 1.0]` with sum within `1e-12` of `1.0`.
/// - Breaks exact argmax ties conservatively in canonical index order:
///   `Neutral (0) > Positive (1) > Negative (2)`.
pub fn sentiment_softmax_and_label(logits: [f32; 3]) -> Option<SentimentPrediction> {
    let [l0_f32, l1_f32, l2_f32] = logits;
    if !l0_f32.is_finite() || !l1_f32.is_finite() || !l2_f32.is_finite() {
        return None;
    }

    let l0 = f64::from(l0_f32);
    let l1 = f64::from(l1_f32);
    let l2 = f64::from(l2_f32);
    let max_l = l0.max(l1).max(l2);

    let e0 = exp_nonpositive(l0 - max_l);
    let e1 = exp_nonpositive(l1 - max_l);
    let e2 = exp_nonpositive(l2 - max_l);
    let sum_e = e0 + e1 + e2;

    let p0 = div_unit_by_sum(e0, sum_e).clamp(0.0, 1.0);
    let p1 = div_unit_by_sum(e1, sum_e).clamp(0.0, 1.0);
    let p2 = div_unit_by_sum(e2, sum_e).clamp(0.0, 1.0);

    let label = if p0 >= p1 && p0 >= p2 {
        SentimentLabel::Neutral
    } else if p1 >= p2 {
        SentimentLabel::Positive
    } else {
        SentimentLabel::Negative
    };

    Some(SentimentPrediction {
        label,
        neutral: p0,
        positive: p1,
        negative: p2,
    })
}

/// Overflow-safe binary NLI entailment probability `sigmoid(entail - contra)`.
///
/// Uses branch-stable exponentiation in `f64` so `exp` is only ever called on
/// non-positive finite arguments, eliminating `f32::exp` overflow above `~88.7`.
pub fn topic_entailment_prob(entail: f32, contra: f32) -> Option<f32> {
    if !entail.is_finite() || !contra.is_finite() {
        return None;
    }

    let e = f64::from(entail);
    let c = f64::from(contra);
    let prob = if e >= c {
        let r = exp_nonpositive(c - e);
        1.0 / (1.0 + r)
    } else {
        let r = exp_nonpositive(e - c);
        r / (1.0 + r)
    };

    Some((prob as f32).clamp(0.0, 1.0))
}

/// Select the index of the highest finite entailment probability meeting `threshold`.
///
/// Returns `None` if `threshold` is `NaN`, `probs` is empty, or no finite score
/// reaches `threshold`. Ties pick the lowest index deterministically.
pub fn select_topic_index(probs: &[f32], threshold: f32) -> Option<usize> {
    if threshold.is_nan() {
        return None;
    }

    let mut best: Option<(usize, f32)> = None;
    for (idx, &p) in probs.iter().enumerate() {
        if !p.is_finite() {
            continue;
        }
        match best {
            None => best = Some((idx, p)),
            Some((_, best_p)) if p > best_p => best = Some((idx, p)),
            _ => {}
        }
    }

    match best {
        Some((idx, p)) if p >= threshold => Some(idx),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::validation::{validate_probability_simplex_with_tol, STRICT_SIMPLEX_TOLERANCE};

    #[test]
    fn softmax_matches_expected_ordering_and_simplex() {
        let pred = sentiment_softmax_and_label([1.0, 2.0, 0.5]).expect("finite logits");
        assert_eq!(pred.label, SentimentLabel::Positive);
        assert!(pred.positive > pred.neutral && pred.neutral > pred.negative);
        assert!(validate_probability_simplex_with_tol(
            pred.positive,
            pred.neutral,
            pred.negative,
            STRICT_SIMPLEX_TOLERANCE,
        )
        .is_ok());
    }

    #[test]
    fn softmax_breaks_exact_ties_conservatively_to_neutral() {
        let equal = sentiment_softmax_and_label([1.5, 1.5, 1.5]).expect("finite logits");
        assert_eq!(equal.label, SentimentLabel::Neutral);

        let neu_pos_tie = sentiment_softmax_and_label([2.0, 2.0, 0.0]).expect("finite logits");
        assert_eq!(neu_pos_tie.label, SentimentLabel::Neutral);

        let neu_neg_tie = sentiment_softmax_and_label([2.0, 0.0, 2.0]).expect("finite logits");
        assert_eq!(neu_neg_tie.label, SentimentLabel::Neutral);
    }

    #[test]
    fn softmax_handles_extreme_f32_logits_without_overflow() {
        let pred = sentiment_softmax_and_label([f32::MAX, -f32::MAX, 0.0]).expect("finite logits");
        assert_eq!(pred.label, SentimentLabel::Neutral);
        assert_eq!(pred.neutral, 1.0);
        assert_eq!(pred.positive, 0.0);
        assert_eq!(pred.negative, 0.0);
    }

    #[test]
    fn topic_entailment_does_not_overflow_above_89() {
        // Previously `entail.exp()` overflowed f32 (> 88.7) and yielded NaN.
        let p = topic_entailment_prob(100.0, 99.0).expect("finite");
        assert!(p.is_finite());
        assert!((p - 0.7310586).abs() < 1e-5);

        let extreme = topic_entailment_prob(f32::MAX, f32::MAX).expect("finite");
        assert_eq!(extreme, 0.5);

        let max_vs_min = topic_entailment_prob(f32::MAX, -f32::MAX).expect("finite");
        assert_eq!(max_vs_min, 1.0);

        let min_vs_max = topic_entailment_prob(-f32::MAX, f32::MAX).expect("finite");
        assert_eq!(min_vs_max, 0.0);
    }

    #[test]
    fn exp_nonpositive_satisfies_kani_stub_contract() {
        // Verify the mathematical contract assumed by the Kani stub across
        // representative boundaries and a dense sweep of non-positive values.
        assert_eq!(exp_nonpositive(0.0), 1.0);
        assert_eq!(exp_nonpositive(-0.0), 1.0);
        let samples = [
            -1e-15_f64,
            -1e-6,
            -0.5,
            -1.0,
            -10.0,
            -88.7,
            -709.0,
            -745.0,
            -1e6,
            -f64::from(f32::MAX) * 2.0,
        ];
        let mut prev = 1.0_f64;
        for x in samples {
            let y = exp_nonpositive(x);
            assert!(y.is_finite());
            assert!((0.0..=1.0).contains(&y));
            assert!(y <= prev);
            prev = y;
        }
    }
}

#[cfg(kani)]
mod kani_proofs {
    use super::*;

    /// Sound mathematical abstraction of `exp(x)` on `x <= 0.0`:
    /// asserts the domain precondition (`x.is_finite() && x <= 0.0`), returns `1.0`
    /// at `0.0`, and returns an arbitrary value in `[0.0, 1.0]` for `x < 0.0`.
    #[allow(dead_code)]
    fn stub_exp_nonpositive(x: f64) -> f64 {
        assert!(x.is_finite(), "exp input must be finite");
        assert!(x <= 0.0, "exp input must be non-positive to avoid overflow");
        if x == 0.0 {
            1.0
        } else {
            let y: f64 = kani::any();
            kani::assume(y.is_finite() && (0.0..=1.0).contains(&y));
            y
        }
    }

    /// Assume-guarantee stub for `div_unit_by_sum`: asserts the exact domain
    /// preconditions verified independently by `verify_div_unit_by_sum_is_unit_interval`,
    /// avoiding three coupled 64-bit divider circuits in a single SAT query.
    #[allow(dead_code)]
    fn stub_div_unit_by_sum(num: f64, denom: f64) -> f64 {
        assert!(num.is_finite() && denom.is_finite());
        assert!(num >= 0.0 && num <= 1.0);
        assert!(denom >= 1.0 && denom <= 3.0);
        assert!(num <= denom);
        let q: f64 = kani::any();
        kani::assume(q.is_finite() && (0.0..=1.0).contains(&q));
        q
    }

    /// Discharge the `div_unit_by_sum` contract over full IEEE-754 `f64` division.
    #[kani::proof]
    fn verify_div_unit_by_sum_is_unit_interval() {
        let num: f64 = kani::any();
        let denom: f64 = kani::any();
        kani::assume(num.is_finite() && denom.is_finite());
        kani::assume(num >= 0.0 && num <= 1.0);
        kani::assume(denom >= 1.0 && denom <= 3.0);
        kani::assume(num <= denom);

        let q = div_unit_by_sum(num, denom);
        assert!(q.is_finite());
        assert!((0.0..=1.0).contains(&q));
    }

    #[kani::proof]
    #[kani::stub(exp_nonpositive, stub_exp_nonpositive)]
    #[kani::stub(div_unit_by_sum, stub_div_unit_by_sum)]
    fn verify_sentiment_softmax_bounds_and_argmax() {
        let logits: [f32; 3] = [kani::any(), kani::any(), kani::any()];
        let all_finite = logits.iter().all(|v| v.is_finite());

        let result = sentiment_softmax_and_label(logits);
        if !all_finite {
            assert!(result.is_none());
            return;
        }

        let pred = result.expect("finite logits must produce a prediction");
        assert!(pred.neutral.is_finite() && (0.0..=1.0).contains(&pred.neutral));
        assert!(pred.positive.is_finite() && (0.0..=1.0).contains(&pred.positive));
        assert!(pred.negative.is_finite() && (0.0..=1.0).contains(&pred.negative));

        // Label is a true argmax with neutral-first conservative tie-breaking.
        let top_p = pred.probability_for(pred.label);
        assert!(top_p >= pred.neutral);
        assert!(top_p >= pred.positive);
        assert!(top_p >= pred.negative);
        if pred.neutral == top_p {
            assert_eq!(pred.label, SentimentLabel::Neutral);
        }
    }

    #[kani::proof]
    #[kani::stub(exp_nonpositive, stub_exp_nonpositive)]
    fn verify_topic_entailment_prob_bounds_and_half_split() {
        let entail: f32 = kani::any();
        let contra: f32 = kani::any();

        let result = topic_entailment_prob(entail, contra);
        if !entail.is_finite() || !contra.is_finite() {
            assert!(result.is_none());
            return;
        }

        let prob = result.expect("finite logits must yield a finite probability");
        assert!(prob.is_finite());
        assert!((0.0..=1.0).contains(&prob));
        if entail >= contra {
            assert!(prob >= 0.5);
        } else {
            assert!(prob <= 0.5);
        }
    }

    #[kani::proof]
    fn verify_select_topic_index_respects_threshold_and_maximality() {
        let probs: [f32; 4] = [kani::any(), kani::any(), kani::any(), kani::any()];
        let threshold: f32 = kani::any();

        let selected = select_topic_index(&probs, threshold);
        if threshold.is_nan() {
            assert!(selected.is_none());
            return;
        }

        match selected {
            Some(idx) => {
                assert!(idx < probs.len());
                let chosen = probs[idx];
                assert!(chosen.is_finite());
                assert!(chosen >= threshold);
                for (j, &p) in probs.iter().enumerate() {
                    if p.is_finite() {
                        assert!(chosen >= p);
                        if j < idx {
                            assert!(chosen > p);
                        }
                    }
                }
            }
            None => {
                for &p in &probs {
                    if p.is_finite() {
                        assert!(p < threshold);
                    }
                }
            }
        }
    }
}
