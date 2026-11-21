# Formal Verification Strategy for `social-sentiment`

This document records how formal mathematics and automated verification apply to the `social-sentiment` codebase—what to verify, what not to verify, invariants and bugs surfaced and resolved across the codebase, and the phased roadmap.

---

## 1. Framework: Four Categories of Formal Guarantees in AI Projects

Formal mathematics cannot prove open-ended empirical claims (e.g., *"FinTwitBERT always classifies sarcasm accurately"*). Every formal proof starts by translating a claim into explicit mathematical assumptions and guarantees across four categories:

1. **Theoretical / Algorithmic Properties**: Mathematical invariants of algorithms, transformations, numerical formulas, and aggregation algebra (holds for all inputs matching stated preconditions).
2. **Properties of a Trained Model (Model Verification)**: Weight-level proofs (e.g., local adversarial robustness via $\alpha,\beta$-CROWN or Marabou) for a concrete set of weights.
3. **Output Correctness via Certificates / Runtime Shields (Neuro-Symbolic)**: Keep the AI model untrusted and route every output through a small, formally verified Trusted Computing Base (TCB) checker.
4. **Statistical / Distributional Guarantees**: Finite-sample probabilistic bounds (e.g., Empirical Bernstein / Hoeffding intervals, split/adaptive conformal prediction coverage $\mathbb{P}(Y \in C(X)) \ge 1 - \alpha$, Horvitz–Thompson debiasing) under stated data assumptions (independence/exchangeability, known inclusion probabilities).

---

## 2. Where *Not* to Apply Formal Math Here

The two ML models in `social-sentiment` are quantized ONNX transformers (all 11 backend runtime services now run in Rust, with `shared/topics.py` retained for the `preprocessing-service/backfill_topics.py` maintenance CLI):

- **Sentiment**: FinTwitBERT (`BertForSequenceClassification`, 12 layers, GELU, hidden size 768) in `crates/sentiment-service/src/sentiment_model.rs`.
- **Topics**: DistilBERT-MNLI (`DistilBertForSequenceClassification`, 6 layers, GELU, zero-shot NLI over 8 hypotheses) in `crates/preprocessing-service/src/topic_model.rs` and `shared/topics.py`.

**Category 2 (direct neural-network weight verification via $\alpha,\beta$-CROWN / Marabou)** is intractable for 6–12 layer GELU BERT transformers over discrete token sequences and low-ROI here.

Instead, treat both ONNX models as **untrusted black boxes** and apply **Categories 1, 3, and 4** to the **Trusted Computing Base (TCB)** surrounding them (`crates/social-sentiment-math`).

---

## 3. High-ROI Applications in `social-sentiment`

### A. Inference Post-Processing & Numerical Soundness *(Category 1 + Kani — Implemented)*

All ONNX post-processing now delegates to `crates/social-sentiment-math/src/postprocess.rs`, verified by Kani using assume-guarantee stubs for `exp_nonpositive` and `div_unit_by_sum`:

1. **Topic NLI `exp()` overflow (resolved)**: Replaced unshifted `exp()` (which overflowed `f32` above `~88.7` to `inf / inf = NaN`) with a branch-stable `f64` sigmoid in both `crates/social-sentiment-math/src/postprocess.rs` (`topic_entailment_prob`) and `shared/topics.py`.
2. **Sentiment softmax tie-breaking & simplex stability (resolved)**: Promoted `f32` logits to `f64` before shifting by `max_l`, removed the ad-hoc `*val += diff` overshoot risk, and standardized conservative `Neutral`-first argmax tie-breaking in `sentiment_softmax_and_label`.

### B. Cross-Boundary Decision Logic & Monotonicity *(Category 1 + Kani — Implemented)*

Opportunity scoring and ex-post signal evaluation live in `crates/social-sentiment-math/src/scoring.rs` and `crates/social-sentiment-math/src/market.rs`:

- **Compile-time sign convention (`SectorRelativeScore`)**: Replaced raw `Option<f64>` with `SectorRelativeScore` (`lower_is_better`, `higher_is_better`, `from_stored`) so `> 0.0` always means better than sector across `market-producer`, PostgreSQL `stock_metrics`, `api-service`, and the UI scorecard—permanently fixing the `pe_relative_sector` sign inversion bug.
- **Bounds, Exact Accounting & Strategy Matrix**: Kani proves `score ∈ [0, 100]` finite, exact `score == item_points * 10.0` accounting (which caught `score / 10.0 * 100.0` rounding drift), full `(OpportunityClass × VIX) → OpportunityStrategy` safety, and `10 / 10` `kani::cover!` reachability across all 4 classes and 6 strategies.
- **Bounded Support Band (`[-2.5%, +2.5%]`) & Trailing Local Support**: Prevents severe breakdowns (`< -2.5%` below support) from firing bullish support bonuses, proves above-support monotonicity and 3-point divergence monotonicity, moves `is_buy_signal()` and `evaluate_signal_quality()` into the verified kernel, and uses each historical bucket's trailing `local_support` in `correlation_handler`.

### C. Additive-Statistics Monoid & IEEE-754 Error Budget *(Category 1 + Category 3 — Implemented)*

`schema-migrate/0006_analytical_constraints.sql` acts as a **Category 3 runtime checker** over `hourly_sentiment_facts`, paired with incremental `ON CONFLICT DO UPDATE` rollups in `crates/storage-service/src/db.rs`:

1. **Commutative Monoid & Inductive Invariants (`rollup.rs`)**: Proved commutativity/associativity of sentiment counts and watermarks, OHLC invariant preservation (even under exact-timestamp ties) plus distinct-endpoint monoid laws, and per-post unit-square/signal/weight `CHECK` bounds.
2. **Reconciled Tolerance Error Budget (`validation.rs`, `shared/schemas.py`)**: Tightened `SIMPLEX_TOLERANCE` from `1e-4` to `5e-7` (half of PostgreSQL's `1e-6` per-post budget) and proved inductive error-budget preservation across bucket scales up to `100,000` posts so valid ingested posts can never abort retention rollups.

### D. Leakage-Free Time-Series, Market Math & Symbol Precedence *(Category 1 + Kani — Implemented)*

Verified in `crates/social-sentiment-math/src/{timeseries,market,symbols}.rs`:

- **Zero-lookahead temporal alignment**: Proved `align_single_factor` strictly pairs `factor_end < stock_end`, `next_close_bracket` strictly brackets `before < occurred_at < after`, `percentile` and `sma`/`compute_sentiment_macd` are panic-free, and `pearson_and_beta` always produces finite `r ∈ [-1, 1]` and finite `beta`.
- **Synthetic NAV proxy estimator**: Proved `weighted_proxy_return` stays bounded in `[-MAX_PLAUSIBLE_MOVE, +MAX_PLAUSIBLE_MOVE]` for all $2^6$ subsets (fixing an eager `.then_some(weighted / covered)` `0.0 / 0.0` `NaN` division Kani discovered in `synthetic_nav.rs`).
- **FX OHLC inversion**: Proved `normalize_fx_ohlc` rejects non-finite/`NaN`/non-positive inputs and preserves `0 < low <= open, close <= high`.
- **Symbol matching precedence**: Proved `evaluate_symbol_match` across all boolean combinations (`block_phrases` strictly dominate, `require_cashtag` blocks bare tickers, `require_uppercase` enforces case).

### E. Statistical / Conformal Guarantees on AI Outputs *(Category 4)*

The database schema provides the provenance required for rigorous statistical guarantees:

- Deterministic uniform `probability` sampling (`inclusion_probability`, `MD5(...) / 4294967296.0`) and `stratum` in `raw_post_archive_staging` (`schema-migrate/0003_analytical_facts.sql`, `crates/storage-service/src/db.rs`).
- Immutable SHA-256 `model_artifacts` identities (`schema-migrate/0005_provenance_and_quality.sql`).

Two concrete Category 4 upgrades:

1. **Finite-Sample Confidence Bounds on Aggregated Sentiment *(Implemented — Phase 3a)***:
   - Implemented Kani-verified Hoeffding + Maurer & Pontil (2009) Empirical Bernstein confidence intervals (using an exact union-bound hybrid at `alpha / 2` for `n >= 2` so coverage stays provably `>= 1 - alpha` without data-dependent selection bias) in `crates/social-sentiment-math/src/bounds.rs`.
   - Exposed 95% confidence bounds and directional significance flags on `/api/stats/leaderboard` and `/api/stats/correlation` using `(post_count, signal_sum, signal_sq_sum)` across live `posts`, `hourly_sentiment_facts`, and legacy `hourly_sentiment_agg`.
2. **Conformal Prediction / Selective Abstention & Horvitz–Thompson Debiasing *(Remaining — Phase 3b)***:
   - Once a small human-audited calibration slice is collected from `raw_post_archive_staging` (`cohort = 'probability'`, pinned by `model_hash`), replace the fixed `0.60` NLI entailment cutoff in `topic_model.rs` and single-label sentiment outputs with split or Adaptive Conformal Inference (ACI) prediction sets guaranteeing $\mathbb{P}(Y \in C(X)) \ge 1 - \alpha$, and apply Horvitz–Thompson debiasing to aggregate sentiment indices.

---

## 4. Phased Roadmap

1. **Phase 1 — Kani on Pure Rust Math *(Implemented)***:
   - Extracted the zero-dependency Trusted Computing Base crate `crates/social-sentiment-math` and wired `social-sentiment-core`, `market-producer`, `service-preprocessing`, `service-sentiment`, and `service-api` directly to it.
   - Verified **36 Kani proof harnesses** (`cargo kani -p social-sentiment-math -Z stubbing --output-format terse`) in a dedicated path-filtered CI workflow (`.github/workflows/kani.yml`).
2. **Phase 2 — Rollup Contract & Error Budget *(Implemented)***:
   - Proved sentiment/OHLC rollup monoid laws and SQL `CHECK` invariants in `rollup.rs` and reconciled `SIMPLEX_TOLERANCE` (`5e-7`) against `schema-migrate/0006_analytical_constraints.sql` (`1e-6`).
3. **Phase 3a — Finite-Sample Confidence Bounds *(Implemented)***:
   - Added Kani-verified Hoeffding + Empirical Bernstein bounds in `crates/social-sentiment-math/src/bounds.rs` and exposed 95% confidence intervals on `/api/stats/leaderboard` and `/api/stats/correlation`.
4. **Phase 3b — Conformal Calibration & Debiasing *(Planned)***:
   - Calibrate split/adaptive conformal prediction sets and Horvitz–Thompson debiasing on human-audited samples from `raw_post_archive_staging` (`cohort = 'probability'`) keyed by `model_artifacts` SHA-256 hashes.
