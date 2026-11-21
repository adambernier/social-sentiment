//! Pure, zero-dependency mathematical kernel for `social-sentiment`.
//!
//! This crate forms the formally verified Trusted Computing Base (TCB) for
//! numerical post-processing, probability validation, market/FX/NAV math,
//! opportunity scoring, and leakage-free time-series alignment.
//!
//! Run formal verification with:
//! ```text
//! cargo kani -p social-sentiment-math
//! ```

pub mod bounds;
pub mod market;
pub mod postprocess;
pub mod rollup;
pub mod scoring;
pub mod symbols;
pub mod timeseries;
pub mod validation;
