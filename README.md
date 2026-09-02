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
| `api-service`           | FastAPI backend exposing aggregated endpoints like `/stats/dashboard`    |
| `ui-service`            | Next.js (React) web frontend with real-time charts and scorecards        |

Tracked tickers/symbols are centralized in `shared/symbols.py`. Both transformer
models ship as quantized ONNX in `*/model_quant/`, so there is no large
model download on first boot.

## Architecture notes

- **Async ingest pipeline.** The consumers (`preprocessing`, `sentiment`,
  `storage`) use `aio-pika` with `connect_robust` and process messages in
  batches, acking on success and re-queuing (`nack(requeue=True)`) on failure.
- **Async connection pooling.** `api-service` and `storage-service` use a
  `psycopg_pool.AsyncConnectionPool` for Postgres access.
- **Graceful shutdown.** Every service routes its entrypoint through
  `shared/runtime.py`, which traps `SIGINT`/`SIGTERM` so in-flight work
  finishes (and unacked messages re-queue) before exit — important for clean
  container stops.
- **Real-time updates.** A Postgres `LISTEN/NOTIFY` trigger on new posts is
  relayed to the UI over the `/stats/stream` WebSocket.
- **[Phased Rust migration](docs/rust-backend-migration.md).** Python remains the default implementation. The
  Rust backend candidates are organized in the `crates/` workspace (`social-sentiment-core`,
  `preprocessing-service`, `sentiment-service`, `storage-service`, `social-news-producer`,
  `market-producer`, `global-events-producer`, and `api-service`). The Rust worker overlay is opt-in,
  using confirmed RabbitMQ publishing, reconnect loops, graceful shutdown, and Prometheus endpoints.
  The Rust API runs as a side-by-side candidate while contract, load, and soak gates are completed.

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
   ./.venv/bin/python scripts/run_tests.sh
   ```

## Quick start (Docker Compose)

The whole stack — RabbitMQ, Postgres, and every service — is orchestrated by
`docker-compose.yml`:

```bash
docker compose up -d --build
docker compose ps
```

### Shared proxy for the DaySmart exporter

The same Caddy container also proxies the sibling `daysmart_schedule` app on a
dedicated Basic-Auth hostname. Create the cross-project network once, add
`DAYSMART_SITE_ADDRESS` and `DAYSMART_BASIC_AUTH_PASSWORD_HASH` from
`.env.example` to the root-only deployment `.env`, and start the exporter before
recreating Caddy:

```bash
docker network inspect shared-proxy >/dev/null 2>&1 || \
  docker network create shared-proxy
docker compose run --rm --no-deps caddy caddy hash-password
docker compose -f ../daysmart_schedule/compose.yaml up -d --build app
docker compose up -d --force-recreate caddy
```

The fixed Basic Auth username is `schedule`. The exporter has no published host
port; Caddy reaches the `daysmart-app` network alias over `shared-proxy`. Do not
put DaySmart account credentials in either project's environment file—users
submit them only in the export form.

To evaluate the Rust preprocessing, sentiment, and storage workers while
keeping the production-parity Python API, apply the explicit overlay:

```bash
docker compose -f docker-compose.yml -f docker-compose.rust.yml up -d --build
```

Model promotion is gated by a cross-runtime fixture check:

```bash
python scripts/verify_model_parity.py
```

Run the Rust API beside the Python oracle and compare their contracts:

```bash
docker compose -f docker-compose.yml -f docker-compose.rust.yml \
  --profile rust-api up -d --build api-service rust-api-service
python scripts/verify_api_contract.py \
  --python-url http://127.0.0.1:8000 \
  --rust-url http://127.0.0.1:8001
```

Compose runs `schema-migrate` once after PostgreSQL is healthy. Database-using
services start only after that migration exits successfully; ordinary service
startup never executes DDL.

The quantized ONNX models ship in the images, so first boot is fast; the
`hf_cache` named volume caches any tokenizer assets across restarts.

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

For iterating on a single service it's often nicer to run that service from
the venv while the rest stays in Compose. Stop just the service you want to
replace, then run it locally — `shared/config.py` reads everything from env
vars, with defaults that match `localhost`:

```bash
docker compose up -d rabbitmq postgres
docker compose run --rm schema-migrate
source .venv/bin/activate
python preprocessing-service/main.py    # for example
```

The migration runner records the checksum of each applied migration in
`schema_migrations`. Applied SQL files are immutable: add a new version to
`storage-service/schema_migrations.py` for later schema changes.

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

Each service installs from a fully hashed, transitively pinned `requirements.txt`
compiled by [`uv`](https://github.com/astral-sh/uv) from its `requirements.in`
(direct deps) plus the shared `constraints.txt` (cross-service version pins).
Images install with `pip install --require-hashes`, so builds are reproducible
and artifact-verified.

Regenerate after editing any `requirements.in` or `constraints.txt`:

```bash
scripts/lock.sh                 # recompile all services
scripts/lock.sh api-service     # or just one
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
migrations, sync the catalog, then run the paced two-year daily backfill:

```bash
docker compose up -d postgres schema-migrate
docker compose run --rm market-producer \
  python market-producer/sync_global_instruments.py
GLOBAL_CONTEXT_ENABLED=true docker compose run --rm market-producer \
  python market-producer/global_backfill.py
```

Adding or changing instrument metadata does not require a schema migration.
After deploying the IX0143 adapter, sync and backfill the targeted instrument:

```bash
docker compose run --rm market-producer \
  python market-producer/sync_global_instruments.py

GLOBAL_CONTEXT_ENABLED=true docker compose run --rm market-producer \
  python market-producer/global_backfill.py \
  --instrument-key index:taiwan-semiconductor
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

## Smoke test (synthetic posts)

`test_publisher.py` publishes a handful of canned posts directly to
`raw-posts`, bypassing the Bluesky producer:

```bash
python test_publisher.py
```

Expected behavior:
- 7 `Published ...` lines from the publisher.
- Preprocessing logs each cleaned post; the `t3` (`"hi"`) post is dropped as
  too short.
- Sentiment logs a batch with a label and confidence per post.
- Storage logs `Inserting batch of N posts` and the affected row count;
  re-running the publisher inserts 0 new rows (deduplicated via `ON CONFLICT`).

Inspect the data:
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

### Continuous Integration (CI)

A GitHub Actions workflow is configured in `.github/workflows/tests.yml` to automatically execute the full test suite on every code push and pull request targeting the `main` branch. The tests run in an isolated environment using mocks, meaning they complete quickly without needing any Docker containers or database services running in CI.

### Worker & Producer Qualification Harnesses

For black-box qualification of the Rust pipeline workers (`preprocessing`, `sentiment`, `storage`) and ingestion producers against isolated Docker containers (`docker-compose.worker-qualification.yml`), use `scripts/qualify_worker.py` and `scripts/qualify_producer.py`:

```bash
# 1,000-message recorded replay parity gate
python scripts/qualify_worker.py preprocessing --mode replay --replay-count 1000 --build

# 24-hour observation stability gate
python scripts/qualify_worker.py preprocessing --mode observe --observe-hours 24

# Full worker promotion gate (smoke, faults, replay, observation)
python scripts/qualify_worker.py preprocessing --mode promotion

# Producer fixture parity gate
python scripts/qualify_producer.py bluesky --mode fixtures
```

See [docs/rust-backend-migration.md](docs/rust-backend-migration.md) for full worker promotion and release gate documentation.

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

- Created **phased Rust backend migration workspace** (`crates/`) covering core libraries (`social-sentiment-core`), processing workers (`preprocessing`, `sentiment`, `storage`), ingestion producers (`social-news`, `market`, `global-events`), and side-by-side Axum API candidate.
- Added **live Docker-backed worker qualification harness** (`scripts/qualify_worker.py`) with RabbitMQ/PostgreSQL fault injection, 1,000-message recorded replay parity gates, and 24-hour observation windows.
- Implemented **producer fixture parity and shadow capture verification harness** (`scripts/qualify_producer.py` and `scripts/verify_producer_parity.py`).
- Added **cross-runtime model parity gate** (`scripts/verify_model_parity.py`) and side-by-side **API contract validation harness** (`scripts/verify_api_contract.py`).
- Integrated **opt-in global context factor & event model** with Asian indices, FX pairs, geopolitical rules, and 30/90-session lag statistics (`GLOBAL_CONTEXT_ENABLED`).
- Migrated ingest consumers to **async `aio-pika`** with batched processing and explicit ack/re-queue.
- Added **async Postgres connection pooling** (`psycopg_pool`) in the API and storage services.
- Added **hourly sentiment rollup + retention pruning** (`storage-service/rollup.py`) with a two-tier hot/cold read path in the API.
- Added **graceful `SIGTERM`/`SIGINT` shutdown** across all services via `shared/runtime.py`.
