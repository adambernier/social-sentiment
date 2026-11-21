# social-sentiment

A message-queue pipeline that ingests posts from social and news sources,
cleans and topic-classifies them, scores their sentiment with a transformer
model, stores the results in Postgres, and correlates that sentiment against
real-time market data to drive a stock-performance dashboard.

```text
[Bluesky | Reddit | Stocktwits | News]        [Market data (yfinance)]
        └────────────┬─────────────┘                     │
               (raw-posts)                                │
                     ▼                                     │
             [Preprocessing]  ─ zero-shot ONNX topics      │
                     ▼                                     │
              (clean-posts)                                │
                     ▼                                     │
              [Sentiment]  ─ FinTwitBERT (quantized ONNX)  │
                     ▼                                     │
             (scored-posts)                                │
                     ▼                                     ▼
               [Storage] ───────▶ [(Postgres)] ◀───────────┘
                                       ▼
                       [API Service]  ─ analytics + correlation
                                       ▼
                        [UI Service]  ─ Next.js dashboard
```

## Services

| Service                 | Role                                                                     |
| ----------------------- | ------------------------------------------------------------------------ |
| `bluesky-producer`      | Polls public Bluesky search API for keywords, publishes raw posts        |
| `reddit-producer`       | Polls Reddit API for symbol mentions, publishes raw posts                |
| `stocktwits-producer`   | Polls Stocktwits streaming/search API, publishes raw posts               |
| `finnhub-producer`      | Polls Finnhub company news per symbol, publishes raw posts               |
| `alpaca-producer`       | Polls Alpaca market news per symbol, publishes raw posts                 |
| `market-producer`       | Fetches live quotes plus separately paced global factor/reference bars   |
| `global-events-producer`| Polls explicit geopolitical rules through the provider-neutral event API |
| `preprocessing-service` | Cleans text, runs a quantized ONNX zero-shot topic model, drops short posts |
| `sentiment-service`     | Scores clean posts with a quantized ONNX FinTwitBERT model, in batches   |
| `storage-service`       | Consumes scored posts and performs batched, idempotent database writes   |
| `api-service`           | Axum (Rust) backend exposing aggregated endpoints like `/stats/dashboard` |
| `schema-migrate`        | Standalone Python one-shot runner applying immutable SQL migrations       |
| `archive-service`       | Standalone Python monthly Parquet exporter to S3/Spaces (`archive` profile)|
| `ui-service`            | Next.js (React) web frontend with real-time charts and scorecards        |

Both transformer models ship as quantized ONNX in `*/model_quant/`, so there is
no large model download on first boot.

## Architecture notes

- **Rust backend workspace (`crates/`).** All 11 runtime backend services —
  the 7 ingestion producers, 3 RabbitMQ workers (`preprocessing`, `sentiment`,
  `storage`), and the HTTP/WebSocket `api-service` — run as native Rust binaries
  built from `Dockerfile.rust` by default in `docker-compose.yml`.
- **Formally verified math kernel (`crates/social-sentiment-math`).** Pure,
  zero-dependency scoring, ONNX post-processing, simplex validation, FX/NAV
  math, and leakage-free time-series alignment live in `social-sentiment-math`
  and are formally verified with [Kani](https://model-checking.github.io/kani/).
  See [the formal verification strategy](docs/formal-verification-strategy.md).
- **Async ingest pipeline.** The Rust workers (`preprocessing`, `sentiment`,
  `storage`) use `lapin` with publisher confirms, reconnect loops, explicit
  ack/retry/dead-letter handling, and `sqlx` connection pooling for Postgres.
- **Graceful shutdown.** Every Rust service traps `SIGINT`/`SIGTERM` via
  `social-sentiment-core` so in-flight work finishes and unacked messages
  requeue cleanly before exit.
- **Real-time updates.** A Postgres `LISTEN/NOTIFY` trigger on new posts is
  relayed to the UI over the `/api/stats/stream` WebSocket.
- **Retained Python utilities.** `schema-migrate/` (one-shot DB migrations) and
  `archive-service/` (monthly encrypted Parquet export) remain standalone Python
  containers with hashed lockfiles. See [the Rust migration record](docs/rust-backend-migration.md).

## Features

### 📊 Relative Performance Scorecard
Inspired by hockey stat cards, the dashboard features a **Divergent Bar Chart** that shows how a stock is performing relative to its sector peers:
- **Valuation (P/E):** Relative to sector average (inverted: lower is better).
- **Risk (Beta):** Relative to sector average (inverted: lower volatility is better).
- **Returns (1Y):** Annual return vs. sector ETF performance.

### 🧠 Sentiment Engine
- Uses a **FinTwitBERT** model (quantized to ONNX) fine-tuned for financial sentiment.
- Automatically calculates **Retail vs. Institutional Divergence** by comparing social media chatter against news headlines.

### 🔗 Price–Sentiment Correlation
- An **engagement-weighted sentiment index** per hourly bucket, plus a simple moving average.
- A **Pearson correlation** between sentiment and price change, swept across ±5h
  lags to surface whether sentiment leads or lags price (`/stats/correlation`).

### 🗄️ Two-tier retention
- Dimension-preserving sufficient statistics are kept in
  `hourly_sentiment_facts`, while raw posts are pruned after 14 days. The older
  flattened table remains an explicitly unfiltered legacy tier.
- Post rollup and pruning are one atomic database operation; late posts add to
  existing hourly aggregates rather than replacing them.
- WAL-G PITR and verified Parquet archives protect operational and analytical
  state independently. See [the backup and analytics runbook](docs/backups-and-analytics.md).

### 🌏 Opt-in global context

- `GLOBAL_CONTEXT_ENABLED=false` by default gates market/event ingestion, the
  new API routes, and the client bundle.
- Stable keys such as `index:nikkei-225`, `fx:usd-jpy`, and
  `us-stock:NVDA` keep provider aliases out of database identity.
- The seeded universe includes six broad Asian indices, the targeted TIP
  Taiwan Semiconductor Total Market Index, five Asian USD FX pairs, gold,
  Brent crude, and copper. Asian FX is normalized as local-currency units per
  USD, so a positive move means local-currency weakness.
- `GET /api/stats/global-context?symbol=NVDA&horizon_sessions=30` returns
  current factor moves, 30/90-session lag statistics, recent event signals,
  next-close reactions, and source timestamps. The existing dashboard response
  is unchanged.
- Relationships pair each factor close with the first later U.S. close, test
  lags 0–2, and remain null below 20 observations or for zero-variance series.
  They are measured associations, not causal claims or forecasts.

## Getting Started on a New Machine

1. **Clone the repository:**
   ```bash
   git clone https://github.com/adambernier/social-sentiment.git
   cd social-sentiment
   ```

2. **(Optional) Local Python Environment:**
   If you plan to run scripts or services locally outside of Docker, set up a virtual environment (Python 3.11+ required):
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   # Or run commands directly using the venv executable:
   ./.venv/bin/pytest
   ```

## Quick start (Docker Compose)

The whole stack — RabbitMQ, Postgres, `schema-migrate`, the 11 Rust backend
services, UI, Prometheus, Grafana, and Caddy — is orchestrated by
`docker-compose.yml`:

```bash
docker compose up -d --build
docker compose ps
```

Compose runs `schema-migrate` once after PostgreSQL is healthy. Database-using
services start only after that migration exits successfully; ordinary service
startup never executes DDL.

The quantized ONNX models ship in the images (`preprocessing-service/model_quant`
and `sentiment-service/model_quant`), so first boot is fast.

Useful endpoints once the stack is up:
- UI dashboard: http://localhost:3000
- API docs: http://localhost:8000/docs
- Consolidated dashboard stats: http://localhost:8000/api/stats/dashboard?symbol=INTC&hours=24
- Price–sentiment correlation: http://localhost:8000/api/stats/correlation?symbol=INTC&hours=24
- Latest posts: http://localhost:8000/api/posts
- 24h sentiment stats: http://localhost:8000/api/stats/sentiment
- 24h topic stats: http://localhost:8000/api/stats/topics
- Financial metrics: http://localhost:8000/api/stats/metrics?symbol=INTC
- Live updates (WebSocket): ws://localhost:8000/api/stats/stream
- RabbitMQ management UI: http://localhost:15672 (default development user/password: `guest` / `guest`)
- Postgres: `localhost:5432` (default development db: `sentiment`, user: `postgres`, password: `sentiment`)

> [!WARNING]
> The default credentials specified in `docker-compose.yml` are strictly for convenient local development and testing. When deploying to staging or production environments, you **must** override these values. Copy the [.env.example](.env.example) template to `.env` in the root directory and supply secure password values. Docker Compose and the service config parsers will automatically detect and apply the values from your `.env` file.

Tear down:
```bash
docker compose down              # stop containers, keep data
docker compose down -v           # also drop postgres_data and hf_cache
```

## Local development (services on host, infra in Docker)

For iterating on a single Rust service, keep infra in Compose and run the crate
directly on the host (`social-sentiment-core` reads env vars with defaults
matching `localhost`):

```bash
docker compose up -d rabbitmq postgres
docker compose run --rm schema-migrate
cargo run --locked -p service-preprocessing    # for example
```

The migration runner records the checksum of each applied migration in
`schema_migrations`. Applied SQL files are immutable: add a new version to
`schema-migrate/schema_migrations.py` for later schema changes.

The UI is a Next.js app; iterate on it with `npm run dev` (it is already run
this way inside the `ui-service` container, with the source bind-mounted for
hot reload):

```bash
cd ui-service
npm install
npm run dev        # http://localhost:3000
```

Override defaults with env vars if needed (see `shared/config.py`):

| Variable             | Default                                                    |
| -------------------- | ---------------------------------------------------------- |
| `RABBITMQ_HOST`      | `localhost`                                                |
| `RABBITMQ_PORT`      | `5672`                                                     |
| `RABBITMQ_USER`      | `guest`                                                    |
| `RABBITMQ_PASS`      | `guest`                                                    |
| `QUEUE_RAW_POSTS`    | `raw-posts`                                                |
| `QUEUE_CLEAN_POSTS`  | `clean-posts`                                              |
| `QUEUE_SCORED_POSTS` | `scored-posts`                                             |
| `DATABASE_DSN`       | `postgresql://postgres:sentiment@localhost:5432/sentiment` |

## Dependency lockfiles

The retained Python containers (`schema-migrate` and `archive-service`) install
from fully hashed, transitively pinned `requirements.txt` files compiled by
[`uv`](https://github.com/astral-sh/uv) from `requirements.in` plus the shared
`constraints.txt`. Images install with `pip install --require-hashes`.

Regenerate after editing any `requirements.in` or `constraints.txt`:

```bash
scripts/lock.sh                    # recompile all retained Python services
scripts/lock.sh schema-migrate     # or just one
docker compose up -d --build
```

`scripts/lock.sh` runs `uv` inside a `python:3.11-slim` container (matching the
build image), so you need neither `uv` nor a changed runtime image on the host.
CI (`.github/workflows/lockfiles.yml`) runs `scripts/lock.sh --check` and fails
if a committed lock is stale.

## Data retention & maintenance

Database maintenance is fully automated. Inside the `storage-service`
container, a background scheduler runs once every 24 hours. It atomically
preserves canonical sentiment facts before pruning posts older than 14 days and
preserves hourly/daily market facts before pruning quotes older than 90 days.

Global hourly bars are retained for 180 days, daily bars for five years, and
event signals for one year. These limits are enforced even while the feature
is disabled.

You can also run database maintenance manually at any time using the `storage-service/rollup.py` script:

```bash
# Preview what would change without writing anything
python storage-service/rollup.py --dry-run

# Roll up + prune posts older than 14 days, quotes older than 90 days
python storage-service/rollup.py --retention-days 14 --quote-retention-days 90
```

Post rollup and pruning use one atomic `DELETE ... RETURNING` operation. A
failure rolls back all fact/sample/deletion changes, repeat runs are safe, and
late-arriving posts increment existing facts. Cleaned-text archival is opt-in
per source; the privacy-safe default retains aggregates but archives no text.

Production PostgreSQL uses the checksum-pinned WAL-G image in
`postgres/Dockerfile`. Daily base backups, retention, isolated restore drills,
monthly Parquet exports, credential separation, encryption, and activation are
documented in [Backups and analytical retention](docs/backups-and-analytics.md).

## Global-context rollout

Schema migrations own tables, constraints, and indexes only. Provider-neutral
instrument metadata lives in
`shared/global_instruments.yaml` and is applied with an additive, idempotent
catalog sync; entries omitted from the catalog are never deleted automatically.

For a new installation, keep the flag false while applying the structural
migrations, sync the YAML catalog, then run the paced daily backfill:

```bash
docker compose up -d postgres schema-migrate
python market-producer/sync_global_instruments.py
GLOBAL_CONTEXT_ENABLED=true docker compose run --rm market-producer \
  /app/producer-market backfill 2024-01-01T00:00:00Z 2026-01-01T00:00:00Z 1d
```

For equity hourly quote backfills, use the Rust `market-producer` CLI:

```bash
docker compose run --rm market-producer /app/producer-market backfill-quotes AAPL 1mo
```

The official Taiwan Index Plus feed supplies daily closing history for
`IX0143`; Yahoo's `IX0143.TW` alias remains available for live/hourly polling.

Curated mappings are replaced atomically through authenticated admin routes:

```bash
curl -X PUT http://localhost:8000/api/admin/global-context/NVDA/exposures \
  -H "X-API-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "exposures": [
      {
        "instrument_key": "index:taiwan-semiconductor",
        "reason": "Taiwan semiconductor manufacturing and supply-chain context",
        "display_order": 0
      },
      {
        "instrument_key": "fx:usd-twd",
        "reason": "Taiwan dollar orientation",
        "display_order": 1
      }
    ]
  }'

curl -X PUT http://localhost:8000/api/admin/global-context/NVDA/event-rules \
  -H "X-API-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"rules":[{"name":"Asia chip supply","countries":["Taiwan","South Korea"],"themes":["SUPPLY_CHAIN"],"query_terms":["semiconductor"]}]}'
```

Inspect bar/event timestamps and Prometheus metrics before rebuilding with
`GLOBAL_CONTEXT_ENABLED=true`. Relevant metrics include
`global_context_provider_requests_total`,
`global_context_last_success_timestamp_seconds`,
`global_context_backfill_progress_ratio`,
`global_context_rule_matches_total`, and
`global_context_relationship_sample_sufficient`.

## Inspecting Data

Inspect stored posts in Postgres:
```bash
docker compose exec postgres psql -U postgres -d sentiment \
  -c "SELECT id, platform, sentiment, scores FROM posts ORDER BY timestamp DESC LIMIT 10;"
```

Or via the API:
```bash
curl -s http://localhost:8000/api/posts?limit=5 | jq
curl -s http://localhost:8000/api/stats/sentiment?hours=24 | jq
```
## Running Tests

We use `pytest` for unit and integration testing. Tests are designed to run quickly on the host using the local virtual environment `.venv`.

To install testing dependencies:
```bash
source .venv/bin/activate
pip install pytest pytest-asyncio
```

To run all tests:
```bash
scripts/run_tests.sh
```

To run a subset of tests or pass extra arguments to `pytest`:
```bash
scripts/run_tests.sh tests/test_preprocess.py -v
```

### Auto-run tests on file changes (Watch Mode)

To monitor the codebase for any Python file changes and automatically re-run the test suite, run:
```bash
scripts/watch_tests.sh
```
This uses `watchdog` (pre-installed in the `.venv`) to listen to file modifications and trigger `pytest`.

### Rust Workspace & Formal Verification (Kani)

Run Rust formatting, linting, and unit/contract tests across the workspace:

```bash
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --workspace --locked
```

Run the Kani formal proof suite on `crates/social-sentiment-math` (43 harnesses covering softmax/NLI post-processing, simplex validation, FX/NAV math, `SectorRelativeScore` & opportunity scoring monotonicity/reachability, signal quality evaluation, analytical rollup monoids & SQL `CHECK` error budgets, symbol matching precedence, Pearson/beta & zero-lookahead alignment, market session partitioning & exponential backoff, finite-sample Hoeffding/Empirical Bernstein sentiment confidence bounds, and split-conformal/ACI/Horvitz–Thompson calibration):

```bash
# One-time local install
cargo install --locked kani-verifier --version 0.68.0
cargo kani setup

# Verify all proofs
cargo kani -p social-sentiment-math -Z stubbing --output-format terse
```

### Continuous Integration (CI)

GitHub Actions runs `.github/workflows/tests.yml` on every push and pull request (`test`, `frontend`, `rust`, and `worker-qualification-smoke`), plus a path-filtered `.github/workflows/kani.yml` workflow whenever `crates/social-sentiment-math/**`, `Cargo.toml`, or `Cargo.lock` change (or via manual dispatch):
- **`test`**: Python unit and Postgres-backed integration tests via `pytest`.
- **`frontend`**: Next.js tests, TypeScript type-check, lint, audit, and build.
- **`rust`**: `cargo fmt`, `cargo clippy --locked -D warnings`, `cargo test --workspace --locked`, provider fixture parity, and Compose validation.
- **`kani`** (`.github/workflows/kani.yml`): Formal verification of `crates/social-sentiment-math` using `kani-verifier 0.68.0`.
- **`worker-qualification-smoke`**: Live Docker smoke qualification for the Rust pipeline workers.

### Worker Qualification & Fixture Harnesses

For black-box qualification of the Rust pipeline workers (`preprocessing`, `sentiment`, `storage`) against isolated Docker containers (`docker-compose.worker-qualification.yml`), use `scripts/qualify_worker.py`:

```bash
# Deterministic live smoke cases (also run in CI)
python scripts/qualify_worker.py preprocessing --mode smoke --build

# Destructive fault-injection cases
python scripts/qualify_worker.py preprocessing --mode faults

# 24-hour observation stability gate
python scripts/qualify_worker.py preprocessing --mode observe --observe-hours 24

# Storage atomic retention gate
python scripts/qualify_worker.py storage --mode retention

# Provider JSON fixture contract check
python scripts/verify_producer_parity.py --fixtures-only
```

Observation gates write a JSON evidence record to `artifacts/` naming the source commit,
the container image, and the host it exercised alongside a `passed`/`failed` status.

See [docs/rust-backend-migration.md](docs/rust-backend-migration.md) for full worker qualification documentation.

## Dashboard

The `ui-service` is a real-time **Next.js** dashboard. With the stack running
it is served at http://localhost:3000 and talks to the API via the `API_URL`
environment variable (defaults to the in-network `http://api-service:8000`).

## Troubleshooting

- **Service exits with `AMQPConnectionError`** — RabbitMQ isn't ready yet.
  Compose's healthcheck gates dependents, so this should be rare; if it
  happens, `docker compose logs rabbitmq` and wait for `Server startup
  complete`.
- **`psycopg.OperationalError: connection refused`** — Postgres isn't up, or
  you're running a service locally and pointing at the wrong host. Check
  `DATABASE_DSN`.
- **`ModuleNotFoundError: No module named 'psycopg_pool'`** — a service that
  imports `storage_service.db` needs the pool extra; ensure its
  `requirements.txt` pins `psycopg[binary,pool]`, not just `psycopg[binary]`.
- **Queues piling up in the management UI** — the downstream consumer is down
  or stuck. `docker compose ps` to find the offender; `docker compose logs -f
  <service>` for details.
- **Want to start clean** — `docker compose down -v` drops `postgres_data` and
  `hf_cache`. Or, less destructively, purge queues from the management UI and
  `TRUNCATE posts;` in Postgres.

## Recent changes

- Extracted a zero-dependency **`crates/social-sentiment-math`** kernel formally verified with **Kani** (43 proof harnesses + path-filtered `.github/workflows/kani.yml`), completing all phases of `docs/formal-verification-strategy.md`: split-conformal/ACI/Horvitz–Thompson calibration (`conformal.rs`, `scripts/conformal_calibrate.py`, `TOPIC_CONFORMAL_Q_HAT`/`TOPIC_ENTAILMENT_THRESHOLD`), finite-sample 95% Hoeffding/Empirical Bernstein sentiment confidence intervals across `/api/stats/leaderboard`, `/api/stats/correlation`, and the Next.js dashboard (`TickerActivity` + `CorrelationChart`), compile-time `SectorRelativeScore` sign conventions + Postgres `0007_stock_metrics_constraints` finiteness checks, bounded `±2.5%` support bands, trailing local support in historical opportunity buckets, exact `score ↔ checklist` accounting, additive sentiment/OHLC rollup monoids with reconciled `5e-7` ingestion vs `1e-6` PostgreSQL `CHECK` tolerances, verified market-session partitioning & overflow-safe polling backoff, symbol-matching precedence invariants, overflow-safe topic NLI post-processing (Rust and `shared/topics.py`), FX `NaN`/overflow handling, conservative `neutral`-first sentiment argmax tie-breaking, and lazy synthetic NAV division; serialized shared BuildKit Cargo cache mounts in `Dockerfile.rust`.
- Promoted the **Rust backend workspace (`crates/`)** to the default runtime in `docker-compose.yml` for all 11 backend services, isolated `schema-migrate/` as a standalone migration container, and removed the superseded Python service implementations.
- Added **Rust quote backfill** (`producer-market backfill-quotes`) alongside global bar backfill in `crates/market-producer`.
- Retained `schema-migrate/` and `archive-service/` as standalone Python containers with hashed lockfiles, plus unported one-off maintenance CLIs (`market-producer/sync_global_instruments.py`, `storage-service/rollup.py`, `preprocessing-service/backfill_topics.py`).
- Integrated **opt-in global context factor & event model** with Asian indices, FX pairs, geopolitical rules, and 30/90-session lag statistics (`GLOBAL_CONTEXT_ENABLED`).
- Added **two-tier analytical retention** with atomic hourly sentiment & market fact rollups, WAL-G PITR backups, and monthly SSE-C encrypted Parquet archiving (`archive-service`).
