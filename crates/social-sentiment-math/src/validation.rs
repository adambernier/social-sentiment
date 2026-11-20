//! Probability simplex validation shared across inference and schema boundaries.

use core::fmt;

/// Absolute sum tolerance accepted by `ScoredPost::validate`.
pub const SIMPLEX_TOLERANCE: f64 = 0.0001;

/// Tighter tolerance matching PostgreSQL `hourly_sentiment_facts` checks.
pub const STRICT_SIMPLEX_TOLERANCE: f64 = 1e-6;

#[derive(Debug, Clone, PartialEq)]
pub enum SimplexError {
    OutOfRange { label: &'static str, value: f64 },
    SumMismatch { sum: f64, tolerance: f64 },
}

impl fmt::Display for SimplexError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::OutOfRange { label, value } => {
                write!(
                    f,
                    "score for {label} must be finite and in [0, 1], got {value}"
                )
            }
            Self::SumMismatch { sum, tolerance } => {
                write!(
                    f,
                    "scores sum to {sum}, which is not 1.0 (abs_tol {tolerance})"
                )
            }
        }
    }
}

impl std::error::Error for SimplexError {}

/// Validate that `(positive, neutral, negative)` lie in `[0, 1]` and sum to `1.0`
/// within `SIMPLEX_TOLERANCE`.
pub fn validate_probability_simplex(
    positive: f64,
    neutral: f64,
    negative: f64,
) -> Result<(), SimplexError> {
    validate_probability_simplex_with_tol(positive, neutral, negative, SIMPLEX_TOLERANCE)
}

/// Validate a 3-class probability simplex with an explicit tolerance.
pub fn validate_probability_simplex_with_tol(
    positive: f64,
    neutral: f64,
    negative: f64,
    tolerance: f64,
) -> Result<(), SimplexError> {
    for (label, val) in [
        ("positive", positive),
        ("neutral", neutral),
        ("negative", negative),
    ] {
        if !val.is_finite() || !(0.0..=1.0).contains(&val) {
            return Err(SimplexError::OutOfRange { label, value: val });
        }
    }

    let sum = positive + neutral + negative;
    if !sum.is_finite() || (sum - 1.0).abs() > tolerance {
        return Err(SimplexError::SumMismatch { sum, tolerance });
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_valid_simplex() {
        assert!(validate_probability_simplex(0.7, 0.2, 0.1).is_ok());
    }

    #[test]
    fn rejects_non_summing_and_non_finite() {
        assert!(validate_probability_simplex(0.8, 0.2, 0.3).is_err());
        assert!(validate_probability_simplex(f64::NAN, 0.5, 0.5).is_err());
        assert!(validate_probability_simplex(-0.1, 0.6, 0.5).is_err());
    }
}

#[cfg(kani)]
mod kani_proofs {
    use super::*;

    #[kani::proof]
    fn verify_simplex_rejects_non_finite_and_out_of_bounds() {
        let pos: f64 = kani::any();
        let neu: f64 = kani::any();
        let neg: f64 = kani::any();

        let result = validate_probability_simplex(pos, neu, neg);

        if result.is_ok() {
            assert!(pos.is_finite() && (0.0..=1.0).contains(&pos));
            assert!(neu.is_finite() && (0.0..=1.0).contains(&neu));
            assert!(neg.is_finite() && (0.0..=1.0).contains(&neg));
            let sum = pos + neu + neg;
            assert!(sum.is_finite());
            assert!((sum - 1.0).abs() <= SIMPLEX_TOLERANCE);
        }
    }
}
