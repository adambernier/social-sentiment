# Rust API correlation parity port

Working spec for the gap the API contract gate reports
(`scripts/verify_api_contract.py` against `api-service` and
`rust-api-service`). Python in `api-service/main.py` is the behavioral oracle;
`crates/api-service/src/main.rs` must reproduce its numbers and strings
exactly, per `docs/rust-backend-migration.md` (1e-6 float tolerance, strings
must match).

Observed gate failures this port must clear:

```text
- dashboard: $.vix_quote / $.vix_delta: type dict != NoneType
- correlation: $.coverageMode: 'legacy-unfiltered+canonical' != 'live'
- correlation: $.opportunity: type dict != NoneType
- correlation: $.coverageNotice: wording mismatch
- correlation: $.data[*].signalQuality / resistancePrice / supportPct /
  supportPrice / resistancePct / buyScore / buySignal: populated != null
```

## 1. Sentiment aggregation parity (the data under everything)

Python builds each hourly bucket from three layers
(`_sentiment_aggregate_query`, main.py:1683-1750, and the handler at
1838-1948):

1. **Canonical facts** (`hourly_sentiment_facts`): weighted sums per
   `bucket_hour`, always filtered by symbol and cutoff; optionally by
   `platform` and `topic_label`. Weighted columns consumed:
   `positive_weight_sum`, `negative_weight_sum`, `neutral_weight_sum`,
   `weight_sum`, `weighted_signal_sum`; and
   `sentiment_index = weighted_signal_sum / NULLIF(weight_sum, 0)`.
2. **Legacy aggregate** (`hourly_sentiment_agg`): same columns plus
   `weighted_signal_sum = positive_weight_sum - negative_weight_sum`.
   Eligible **only when the request is unfiltered** (`is_filtered == false`)
   and **only for hours with no canonical fact**
   (`NOT EXISTS` anti-join).
3. **Live posts overlay** (`posts` table): for every post in window,
   `weight = ln(1 + max(0, engagement ?? 1))`; add to the matching bucket's
   counts and weighted sums; `weightedSignal += weight *
   (scores.positive - scores.negative)` with defaults `1.0/0.0` by label.

Bucket `sentimentIndex` is then `weightedSignal / totalWeighted` when
`totalWeighted > 0`, else `0.0`.

Rust currently counts unweighted `posts` rows only and computes
`(positive - negative) / count`. That is the divergence behind every numeric
mismatch. Port layers 1-3 above; keep the query parameterized (sqlx bind),
never interpolate request values.

`is_filtered` in Rust: `platform.is_some() || topic.is_some()` after the
existing `"all"` filtering.

## 2. Coverage query parity

Python `_sentiment_coverage_query` (main.py:1753-1783): coverage start is
`LEAST(MIN(posts.timestamp), MIN(hourly_sentiment_facts.bucket_hour))` for
the same filters; unfiltered requests additionally consider
`MIN(hourly_sentiment_agg.bucket_hour)`. Rust currently uses posts only;
extend it to match. `coverage_complete = coverage_start <= cutoff`.

Response semantics:

- `coverageMode`: `"dimension-preserving"` if filtered, else
  `"legacy-unfiltered+canonical"` (main.py:2262-2264). Rust's `"live"` is
  wrong for the unfiltered case.
- `coverageNotice` when `!coverage_complete`, verbatim (main.py:2239-2245):
  `"Requested history begins before {boundary}; earlier buckets are reported as unavailable zeros."`
  where `boundary = coverage_start_text or "the first canonical observation"`.
  Rust's current wording is out of sync; align it exactly.

## 3. Sentiment momentum oscillator (MACD)

Python `compute_sentiment_macd` (main.py:676-731), fed from main.py:2049-2073:

- Input: per bucket, `Some(sentimentIndex)` when `totalWeighted > 0`, else
  `None`. Carry previous real value forward over empty hours; leading `None`s
  stay `None`.
- `sma(seq, i, period)`: `None` if `i + 1 < period`; `None` if any value in
  the trailing window is `None`; else mean of the window.
- `macd = sma(filled, fast) - sma(filled, slow)` when both exist, else `None`.
- `signal = sma(macd_line, i, signal_period)` (allowing `None`s inside the
  window to poison it, per the helper above).
- `hist = macd - signal` when both exist, else `None`.
- Periods: `fast = adaptive sma_period` (hours <=1 -> 3, <=24 -> 5,
  <=168 -> 12, <=720 -> 24, else 52); `slow/signal` = 6/3, 12/5, 26/9, 52/12
  for the same hour bands.

Rust already allocates the three fields as `None`; fill them.

## 4. Per-bucket support/resistance and pricePct

Python main.py:2110-2140, rolling window 24:

- For bucket `i`: take buckets `max(0, i-23)..=i`, collect non-null
  `rawPrice`, sort, pick indexes `int((n-1) * 0.05)` and `int((n-1) * 0.95)`
  (truncation, not rounding). This equals Rust's existing `percentile()`
  helper semantics.
- If window prices exist and `latest_price` is `Some(p)` with `p != 0.0`:
  set local support/resistance plus pct offsets relative to `latest_price`,
  and set `pricePct = (rawPrice_i - latest_price) / latest_price * 100` when
  the bucket has a raw price.
- Else (empty window): fall back to the global `support_price` /
  `resistance_price` and their pcts.
- `latest_price` = last non-null `rawPrice` in bucket order
  (Python `prices[-1]` from the query-ordered rows).

## 5. Buy signals and opportunity (the layer Rust hardcodes to None)

Python main.py:2142-2237:

- For each bucket `i`, build trailing window `max(0, i-2)..=i`; collect
  non-null `rawPrice` into `prev_prices` and non-null `sentimentIndex` into
  `prev_sentiments` (note: `sentimentIndex` is a float and is always non-null
  in Rust, mirroring Python's pre-filled 0.0 buckets).
- `opp = compute_opportunity(price = rawPrice_i, support = support_price,
  resistance = resistance_price, sentiment = sentimentIndex_i,
  sentimentSma = sentimentSma_i, vixPrice = vix_price,
  peRelative = pe_relative, prevPrices = prev_prices,
  prevSentiments = prev_sentiments)`.

`compute_opportunity` (main.py:565-668), port verbatim:

1. Price near support: if `price` / `support` non-null and `price > 0`:
   `dist = (price - support) / price`; `<= 0.025` -> +3.0 "Price near
   support level"; else `<= 0.05` -> +1.5 "Price approaching support level".
2. Sentiment crossover: if `sentiment > sentiment_sma`: if
   `sentiment_sma > 0` -> +3.0 "Bullish sentiment crossover (above positive
   SMA)"; else -> +1.5 "Bullish sentiment crossover (above negative SMA)".
3. Divergence: if `prev_prices.len() >= 3` and `prev_sentiments.len() >= 3`:
   `price_trend = prev_prices[-1] - prev_prices[0]`,
   `sentiment_trend = prev_sentiments[-1] - prev_sentiments[0]`;
   if `price_trend < 0 && sentiment_trend > 0.1` -> +2.0 "Bullish sentiment
   divergence (price down, sentiment rising)".
4. Valuation: `pe_relative < 0` -> +2.0 "Undervalued relative to sector
   benchmarks"; `== 0` -> +1.0 "Fairly valued relative to sector benchmarks".
5. `opportunity_pct = min(100.0, (score / 10.0) * 100.0)`.
6. Classification/color: `>= 75` STRONG BUY/emerald; `>= 50`
   ACCUMULATE/teal; `>= 30` HOLD / NEUTRAL/slate; else
   CAUTION / OVERBOUGHT/rose.
7. Strategy: default "Hold / Sell Covered Calls" with description "Neutral
   market stance. Capitalize on range-bound behavior using covered call
   writing."; `vix = vix_price ?? 15.0`; for STRONG BUY/ACCUMULATE:
   vix < 15 -> "Long Call Option (Buy Call)", `<= 22` -> "Long Shares / Bull
   Call Spread", else "Sell Put Credit Spreads" (descriptions verbatim from
   main.py:645-654); for CAUTION / OVERBOUGHT: vix > 22 -> "Sell Call Credit
   Spreads", else "Protect Longs / Buy Puts" (main.py:656-662).

Inputs Rust needs but does not currently fetch:

- `vix_price`: latest `stock_quotes.price` for the configured VIX symbol,
  default **15.0** (main.py:1976-1980). (Rust has `state.vix_symbol`.)
- `pe_relative`: latest `stock_metrics.pe_relative_sector` (main.py:1970-1974).

Per-bucket assignment (main.py:2173-2175):
`buyScore = opp.score`, `buySignal = opp.score >= 50.0`,
`signalQuality = "pending"`.

Forward evaluation (main.py:2177-2200): only for buckets with
`buySignal == true` and non-null `rawPrice`; walk forward up to 24 buckets
(`i + 1 .. min(len, i + 25)`), collecting non-null `rawPrice` until two are
collected; if any collected forward price `> current_price * 1.002` ->
`"good"`, else (when at least one collected) `"bad"`. Signals without
forward prices keep `"pending"`.

Final `opportunity` (main.py:2202-2237): computed over the trailing window
`last-3..=last` (all three buckets, not requiring 3 samples), with
`sentiment`/`sentiment_sma` from the last bucket (0.0 if empty), same support/
resistance/vix/pe inputs, `None` only when there are no buckets. Rust
currently returns `opportunity: None` unconditionally.

## 6. Dashboard VIX fields (separate but same gate run)

`dashboard: $.vix_quote / $.vix_delta` fail as `dict != NoneType`. Rust
dashboard fetches the VIX series with `fetch_market_series` (main.rs:543-560);
Python builds vix_quote/vix_delta from the *latest* VIX snapshot
(main.py:1505-1506, `_quote_from_snapshot` / `_delta_from_snapshot` on
`stock_quotes`). If the series query returns nothing where the snapshot query
returns a row, the Rust response goes null. Align the fetch with Python's
latest-snapshot semantics (or prove the divergence is environmental by
diffing both responses against the same seeded DB).

## Constraints

- Python remains the oracle and the production default; do not change
  `api-service/` or the public contract.
- Keep `docs/rust-backend-migration.md` naming/structure; update its status
  boxes only after the gate actually passes.
- Verification sequence for this port (in order):
  1. `cargo check -p service-api` (offline; use `SQLX_OFFLINE=true` if the
     macro path needs it) — must compile.
  2. `cargo test -p service-api` and the existing harness test
     `pytest -q tests/test_api_contract_harness.py`.
  3. Rebuild both containers and run
     `python3 scripts/verify_api_contract.py --python-url http://127.0.0.1:8000 --rust-url http://127.0.0.1:8001`
     against the same seeded database — this is the gate that counts.
- Record which steps ran and their output; a compile-only result is not
  parity evidence.
