# Formal Verification Strategy for `social-sentiment`

This document records how formal mathematics and automated verification apply to the `social-sentiment` codebase—what to verify, what not to verify, real invariants and bugs already surfaced by inspecting the code with a formal specification mindset, and a phased roadmap.

---

## 1. Framework: Four Categories of Formal Guarantees in AI Projects

Formal mathematics cannot prove open-ended empirical claims (e.g., *"FinTwitBERT always classifies sarcasm accurately"*). Every formal proof starts by translating a claim into explicit mathematical assumptions and guarantees across four categories:

1. **Theoretical / Algorithmic Properties**: Mathematical invariants of algorithms, transformations, numerical formulas, and aggregation algebra (holds for all inputs matching stated preconditions).
2. **Properties of a Trained Model (Model Verification)**: Weight-level proofs (e.g., local adversarial robustness via $\alpha,\beta$-CROWN or Marabou) for a concrete set of weights.
3. **Output Correctness via Certificates / Runtime Shields (Neuro-Symbolic)**: Keep the AI model untrusted and route every output through a small, formally verified Trusted Computing Base (TCB) checker.
4. **Statistical / Distributional Guarantees**: Finite-sample probabilistic bounds (e.g., split/adaptive conformal prediction coverage $\mathbb{P}(Y \in C(X)) \ge 1 - \alpha$, empirical Bernstein/Hoeffding intervals, Horvitz–Thompson debiasing) under stated data assumptions (exchangeability, known inclusion probabilities).

---

## 2. Where *Not* to Apply Formal Math Here

The two ML models in `social-sentiment` are quantized ONNX transformers:

- **Sentiment**: FinTwitBERT (`BertForSequenceClassification`, 12 layers, GELU, hidden size 768) in `crates/sentiment-service/src/sentiment_model.rs` and `sentiment-service/model.py`.
- **Topics**: DistilBERT-MNLI (`DistilBertForSequenceClassification`, 6 layers, GELU, zero-shot NLI over 8 hypotheses) in `crates/preprocessing-service/src/topic_model.rs` and `shared/topics.py`.

**Category 2 (direct neural-network weight verification via $\alpha,\beta$-CROWN / Marabou)** is intractable for 6–12 layer GELU BERT transformers over discrete token sequences and low-ROI here.

Instead, treat both ONNX models as **untrusted black boxes** and apply **Categories 1, 3, and 4** to the **Trusted Computing Base (TCB)** surrounding them.

---

## 3. High-ROI Applications in `social-sentiment`

### A. Inference Post-Processing & Numerical Soundness *(Category 1 + Rust Verification)*

Because core workers are implemented in Rust, production post-processing functions can be verified directly with **Kani** (bounded model checking for Rust) or **Verus/Creusot**, closing the spec–implementation gap.

Issues already surfaced in the current code:

1. **Overflow / `NaN` in Topic NLI probability**: Both `crates/preprocessing-service/src/topic_model.rs` (lines 176–189) and `shared/topics.py` (lines 118–131) compute unshifted `exp()`:
   ```rust
   let exp_entail = entail.exp();
   let exp_contra = contra.exp();
   let entail_prob = exp_entail / (exp_entail + exp_contra);
   ```
   Any finite `f32` logit $> 88.7$ overflows `exp()` to `+inf`, producing `NaN`; `max_entail_prob < 0.60` then evaluates to `false` on `NaN`. Switching to max-shifted / branch-stable sigmoid and proving finiteness and $[0, 1]$ bounds for all finite `f32` logits eliminates this class of failure.
2. **Rust ↔ Python Softmax Tie-Breaking & Bounds Parity**:
   - `crates/sentiment-service/src/sentiment_model.rs` (lines 136–142) breaks argmax ties as `positive` > `negative` > `neutral`, whereas `sentiment-service/model.py` (line 48) uses `np.argmax`, breaking ties as `neutral` > `positive` > `negative`.
   - The rounding adjustment `*val += diff` (lines 150–156) lacks a proof that adjusted probabilities remain within $[0.0, 1.0]$ as enforced by `ScoredPost::validate()` in `crates/social-sentiment-core/src/schemas.rs`.

### B. Cross-Boundary Decision Logic & Monotonicity *(Category 1 + SMT / Kani)*

`compute_opportunity` in `crates/api-service/src/main.rs` (lines 1215–1329) and `api-service/main.py` (lines 615–620) turns AI sentiment and market fundamentals into buy scores, classifications, and options strategies. This piecewise-threshold logic is ideal for **Kani / Z3**:

- **Bounds & Totality**: `score ∈ [0, 100]` and finite for all finite inputs; every `(score, vix)` pair maps cleanly to a non-contradictory `(classification, strategy)`.
- **Cross-Module Monotonicity**: Lowering a stock's P/E ratio relative to its sector (making it more undervalued) must never decrease its opportunity score.
- **Bug already surfaced**:
  - `crates/market-producer/src/calculations.rs` (line 84) and `market-producer/main.py` (lines 211–212) compute `pe_relative_sector` with `invert = true` (`-(pe - sector_pe) / sector_pe`), so **undervalued (`pe < sector_pe`) is positive (`> 0`)**.
  - `compute_opportunity` in `crates/api-service/src/main.rs` (lines 1266–1274) and `api-service/main.py` (line 616) awards `+2.0 ("Undervalued relative to sector benchmarks")` when `pe_rel < 0.0`.
  - Isolated unit tests pass in both services while the end-to-end system rewards *overvalued* stocks. A cross-module formal contract catches sign-inversion bugs immediately.

### C. Additive-Statistics Monoid & IEEE-754 Error Budget *(Category 1 + Category 3)*

`storage-service/0006_analytical_constraints.sql` already functions as a **Category 3 runtime checker** over `hourly_sentiment_facts`, paired with incremental `ON CONFLICT DO UPDATE` rollups in `crates/storage-service/src/db.rs` (lines 44–414) and OHLC rollups (lines 455–583).

Formal verification can strengthen two areas:

1. **Commutative Monoid & Inductive Invariants (Lean 4 / Z3)**: Prove that incremental merges for `hourly_sentiment_facts`, `market_hourly_facts`, and `market_daily_facts` are associative and commutative (order-independent for late-arriving data) and preserve every SQL `CHECK` constraint inductively.
2. **Tolerance / Rounding Error Budget (Gappa / FPTaylor / Z3)**:
   - Currently `ScoredPost::validate()` in `crates/social-sentiment-core/src/schemas.rs` (line 144) allows probability sum drift up to **`1e-4`**, whereas `storage-service/0006_analytical_constraints.sql` (lines 15–29) enforces **`1e-6`** (and uses a fixed `+ 1e-6` slack on `signal_sq_sum <= post_count + 1e-6` that does not scale with `post_count`).
   - Proving a unified floating-point error budget across `sentiment_model.rs` → `ScoredPost::validate()` → `ROLLUP_AND_PRUNE_POSTS_SQL` → SQL `CHECK` constraints guarantees valid ingested posts can never abort retention rollups.

### D. Leakage-Free Time-Series & Financial Math *(Category 1 + Rust Verification)*

Pure, self-contained math functions across the Rust workspace are strong verification targets:

- **Zero-lookahead temporal alignment** (`crates/social-sentiment-core/src/analytics.rs`): Prove `calculate_relationship_with_options` strictly pairs `factor_end < stock_end` for every lag even on unsorted inputs, `next_close_move` strictly brackets `before < occurred_at < after`, and Pearson $r \in [-1, 1]$ (accounting for float64 rounding / clamping).
- **Synthetic NAV proxy estimator** (`crates/market-producer/src/synthetic_nav.rs`): Prove that for any non-empty subset of the 6 ETFs in `HOLDINGS`, `estimate_pct_change` is a convex combination of valid proxy moves, bounded in `[-MAX_PLAUSIBLE_MOVE, +MAX_PLAUSIBLE_MOVE]` (`[-8.0, 8.0]`), with no division by zero or `NaN`.
- **FX OHLC inversion** (`crates/market-producer/src/calculations.rs`, lines 96–110): Prove that inverting preserves `0 < low <= open, close <= high`. Currently `*price <= 0.0` lets `NaN` slip past the guard because `NaN <= 0.0` is `false`.
- **Symbol matching precedence** (`crates/social-sentiment-core/src/symbols.rs`): Prove `block_phrases` strictly dominate cashtags/keywords/tickers and `require_cashtag` never matches bare symbols.

### E. Statistical / Conformal Guarantees on AI Outputs *(Category 4)*

The database schema already provides the provenance required for rigorous statistical guarantees:

- Deterministic uniform `probability` sampling (`inclusion_probability`, `MD5(...) / 4294967296.0`) and `stratum` in `raw_post_archive_staging` (`storage-service/0003_analytical_facts.sql`, `crates/storage-service/src/db.rs`).
- Immutable SHA-256 `model_artifacts` identities (`storage-service/0005_provenance_and_quality.sql`).

Two concrete Category 4 upgrades:

1. **Conformal Prediction / Selective Abstention**: Calibrate on human-audited posts from the `probability` cohort (pinned by `model_hash`) to replace the ad-hoc `0.60` entailment threshold in `topic_model.rs` and single-label sentiment outputs with conformal prediction sets guaranteeing $\mathbb{P}(Y \in C(X)) \ge 1 - \alpha$ under exchangeability (or Adaptive Conformal Inference to handle regime shifts).
2. **Finite-Sample Confidence Bounds on Aggregated Sentiment**: Leverage the stored sufficient statistics ($\sum s, \sum s^2, \sum w, \sum w s, \sum w s^2$ for bounded $s \in [-1, 1]$) to attach Empirical Bernstein / Hoeffding intervals and Horvitz–Thompson bias corrections to hourly sentiment indices and `buzz_z` alerts.

---

## 4. Phased Roadmap

1. **Phase 1 — Kani on Pure Rust Math**: Isolate pure, zero-I/O math/scoring/post-processing functions in the Rust workspace and verify them with Kani in CI (fixing the `topic_model.rs` `exp()` overflow, `normalize_fx_ohlc` `NaN` bypass, and `pe_relative_sector` sign inversion along the way).
2. **Phase 2 — Rollup Contract & Error Budget (Lean 4 / Z3)**: Formalize the `hourly_sentiment_facts` and OHLC commutative monoids and reconcile the `ScoredPost::validate()` (`1e-4`) vs. `0006_analytical_constraints.sql` (`1e-6`) floating-point tolerances.
3. **Phase 3 — Conformal Calibration & Finite-Sample Bounds**: Build calibration and uncertainty bounds on top of the `raw_post_archive_staging` `probability` cohort and immutable `model_artifacts` hashes.
