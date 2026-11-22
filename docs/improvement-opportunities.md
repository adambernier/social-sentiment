# Project Improvement Opportunities

Codebase-grounded review of efficiency wins, subtle correctness/UX gaps, and build/ops hardening opportunities across `social-sentiment`.

---

## 1. High-Impact Efficiency Wins

### 1.1 ~~Precompile regexes in `SymbolConfig::matches` (Producer CPU hotspot)~~ *(Completed)*
- **Location:** `crates/social-sentiment-core/src/symbols.rs` (`CompiledSymbolMatcher`, `SymbolConfig::matcher`), `crates/social-news-producer/src/adapters/{bluesky,finnhub,alpaca,reddit}.rs`
- **Implemented:** Added `CompiledSymbolMatcher` (precompiled `Regex`es, `RegexSet` for keywords, pre-lowercased block phrases, skipped text lowercasing when `block_phrases` is empty) with a bounded process-wide cache on `SymbolConfig::matcher()`, and hoisted matcher lookups out of per-post loops across all four producer adapters.

### 1.2 ~~Aggregate hot `posts` in SQL inside `correlation_handler`~~ *(Completed)*
- **Location:** `crates/api-service/src/main.rs` (`HOT_POSTS_HOURLY_SQL`, `correlation_handler`)
- **Implemented:** Replaced the per-row hot `posts` scan and Rust JSONB loop with `HOT_POSTS_HOURLY_SQL` (`CROSS JOIN LATERAL` + `GROUP BY date_trunc('hour', p.timestamp, 'UTC')`), returning at most one numeric row per UTC hour.

### 1.3 ~~Parallelize API queries & add short TTL caching~~ *(Completed)*
- **Location:** `crates/api-service/src/main.rs` (`TtlCache`, `dashboard_handler`, `correlation_handler`, `sources_handler`, `leaderboard_handler`), `crates/api-service/src/admin.rs`
- **Implemented:**
  - Parallelized independent SQL queries with `tokio::try_join!` in `dashboard_handler`, `correlation_handler`, and `sources_handler`.
  - Added an in-memory `TtlCache` (default 15s, configurable via `API_STATS_CACHE_TTL_SECONDS`, `0` disables) for `/api/stats/leaderboard` and `/api/stats/sources`, with immediate `leaderboard_cache.invalidate()` on admin symbol create/update/delete.

### 1.4 ~~Bulk `UNNEST` insert + batch ACK in `storage-service`~~ *(Completed)*
- **Location:** `crates/storage-service/src/db.rs` (`INSERT_POSTS_BULK_SQL`, `insert_scored_batch`), `crates/storage-service/src/main.rs` (`persist_scored_batch`), `tests/test_retention_integration.py`
- **Implemented:** Replaced the 100-statement per-batch `INSERT` loop with a single `INSERT ... SELECT * FROM UNNEST(...) ON CONFLICT (platform, id, symbol) DO NOTHING` query (preserving single-row isolation on permanent SQLSTATE errors) and a single cumulative `BasicAckOptions { multiple: true }` ACK per batch.

### 1.5 ~~Micro-batching & ORT thread tuning in `preprocessing-service` / `sentiment-service`~~ *(Completed)*
- **Location:** `crates/social-sentiment-core/src/config.rs` (`OrtThreadConfig`), `crates/preprocessing-service/src/{topic_model.rs,main.rs}`, `crates/sentiment-service/src/sentiment_model.rs`, `docker-compose.yml`
- **Implemented:**
  - Added `OrtThreadConfig::from_env()` (`ORT_INTRA_THREADS`, default `clamp(cpus, 1, 2)`; `ORT_INTER_THREADS`, default `1`) and wired it into both `TopicModel` and `SentimentModel` ONNX Runtime sessions.
  - Added `TopicModel::predict_batch` (precomputing NLI hypotheses once, short-circuiting spam before ONNX, and running all non-spam posts in one batched forward pass) and micro-batching in `preprocessing-service` (`PREPROCESSING_BATCH_SIZE`, default `8`, `500ms` timeout).

### 1.6 ~~Enable HTTP compression in Caddy~~ *(Completed)*
- **Location:** `Caddyfile`
- **Implemented:** Enabled `encode zstd gzip` in `Caddyfile`.

---

## 2. Subtle Correctness & UX Gaps

### 2.1 ~~28-day `buzz_z` baseline in `leaderboard_handler` is truncated to 14 days~~ *(Completed)*
- **Location:** `crates/api-service/src/main.rs` (`LEADERBOARD_SQL`), `tests/test_retention_integration.py`
- **Implemented:** Updated `LEADERBOARD_SQL` to union cold `hourly_sentiment_facts` (+ legacy `hourly_sentiment_agg` fallback when absent from canonical facts) with hot `posts` across the full 28-day baseline window.

### 2.2 ~~`dashboard_handler` KPIs ignore cold-tier facts on `>14d` windows~~ *(Completed)*
- **Location:** `crates/api-service/src/main.rs` (`SENTIMENT_STATS_SQL`, `TOPIC_STATS_SQL`, `fetch_sentiment_stats`, `fetch_topic_stats`), `tests/test_retention_integration.py`
- **Implemented:** Shared cold+hot tier union helpers across `dashboard_handler`, `sentiment_stats_handler`, and `topic_stats_handler`, including `NULLIF(topic_label, 'Unassigned')` so cold-tier unassigned counts merge cleanly with hot-tier `NULL` labels.

### 2.3 ~~Frontend WebSocket reconnect & tab-visibility behavior~~ *(Completed)*
- **Location:** `ui-service/src/components/hooks/polling.mjs`, `ui-service/src/components/hooks/polling.d.mts`, `ui-service/src/components/hooks/polling.test.mjs`, `ui-service/src/components/hooks/useDashboardData.ts`
- **Implemented:**
  - Added `startReconnectingWebSocket` with bounded exponential backoff (`1s` → `30s` max, reset on open, clean teardown on unmount) and switched `useDashboardData.ts` to filter via `symbolRef`/`platformRef` so changing filters does not reconnect the WebSocket.
  - Made `startCompletionScheduledPolling` visibility-aware (pausing scheduled polls while `document.visibilityState === 'hidden'` and refreshing immediately when returning to `'visible'`).

---

## 3. Build, DB Schema & Ops Hardening

### 3.1 ~~Avoid busting Docker Rust build cache on non-Rust edits~~ *(Completed)*
- **Location:** `Dockerfile.rust`, `.dockerignore`
- **Implemented:** Scoped `rust-source` to `COPY Cargo.toml Cargo.lock ./` and `COPY crates ./crates`, and excluded `docs/` in `.dockerignore` so edits to UI/docs/scripts/compose do not invalidate Rust builder layers.

### 3.2 ~~Restart policies, log rotation & healthchecks in `docker-compose.yml`~~ *(Completed)*
- **Location:** `docker-compose.yml`
- **Implemented:** Added shared `x-service-defaults` (`restart: unless-stopped` + `json-file` log rotation `10m × 3`) across all long-running services, added zero-dependency container `healthcheck`s for `api-service` and `ui-service`, and removed dev-only `WATCHPACK_POLLING=true` from `ui-service`.

### 3.3 ~~Minor PostgreSQL index cleanup~~ *(Completed)*
- **Location:** `schema-migrate/0008_index_optimization.sql`, `schema-migrate/schema_migrations.py`, `tests/test_global_context_schema.py`, `tests/test_retention_integration.py`
- **Implemented:** Added migration `0008_index_optimization` dropping redundant single-column prefix indexes (`posts_symbol_idx`, `stock_quotes_symbol_idx`) and creating composite `posts_platform_timestamp_idx ON posts (platform, timestamp DESC)`.

---

## Suggested Implementation Bundles (Highest ROI First)

1. ~~**API + DB efficiency & correctness bundle:** SQL-side aggregation in `correlation_handler` + `tokio::try_join!` concurrency + short TTL cache + fixing the 28-day `leaderboard_handler` / `>14d` `dashboard_handler` cold-tier union + `encode zstd gzip` in `Caddyfile`.~~ *(Completed)*
2. ~~**Pipeline efficiency bundle:** Precompiled regexes in `symbols.rs` + bulk `UNNEST` insert & multi-ACK in `storage-service` + configurable ORT thread counts (and optional `preprocessing-service` micro-batching).~~ *(Completed)*
3. ~~**Ops & frontend polish bundle:** Scoped `COPY` in `Dockerfile.rust`, `docker-compose.yml` restart/log-rotation/healthchecks, WS reconnect + visibility-aware polling in `useDashboardData.ts`, and migration `0008_index_optimization`.~~ *(Completed)*
