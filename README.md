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
| `news-producer`         | Polls Yahoo Finance RSS feeds per symbol, publishes raw posts            |
| `market-producer`       | Fetches live quotes (price/vol) and background relative-to-sector metrics|
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
- Hourly sentiment aggregates are kept in `hourly_sentiment_agg` (cold tier),
  while raw posts (hot tier) are pruned after a retention window. The API
  transparently overlays live posts on top of the aggregates.
- Run by the maintenance script below.

## Getting Started on a New Machine

1. **Clone the repository:**
   ```bash
   git clone https://github.com/adambernier/social-sentiment.git
   cd social-sentiment
   ```

2. **(Optional) Local Python Environment:**
   If you plan to run scripts or services locally outside of Docker, set up a virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   # Install requirements for whichever service you are working on, e.g.:
   pip install -r preprocessing-service/requirements.txt
   ```

## Quick start (Docker Compose)

The whole stack — RabbitMQ, Postgres, and every service — is orchestrated by
`docker-compose.yml`:

```bash
docker compose up -d --build
docker compose ps
```

The quantized ONNX models ship in the images, so first boot is fast; the
`hf_cache` named volume caches any tokenizer assets across restarts.

Useful endpoints once the stack is up:
- UI dashboard: http://localhost:3000
- API docs: http://localhost:8000/docs
- Consolidated dashboard stats: http://localhost:8000/stats/dashboard?symbol=INTC&hours=24
- Price–sentiment correlation: http://localhost:8000/stats/correlation?symbol=INTC&hours=24
- Latest posts: http://localhost:8000/posts
- 24h sentiment stats: http://localhost:8000/stats/sentiment
- 24h topic stats: http://localhost:8000/stats/topics
- Financial metrics: http://localhost:8000/stats/metrics?symbol=INTC
- Live updates (WebSocket): ws://localhost:8000/stats/stream
- RabbitMQ management UI: http://localhost:15672 (`guest` / `guest`)
- Postgres: `localhost:5432`, db `sentiment`, user `postgres`, password `sentiment`

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
source .venv/bin/activate
python preprocessing-service/main.py    # for example
```

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

## Data retention & maintenance

`storage-service/rollup.py` rolls old posts up into `hourly_sentiment_agg` and
prunes raw data. It is a standalone script (not wired into Compose), so run it
on demand or from cron:

```bash
# Preview what would change without writing anything
python storage-service/rollup.py --dry-run

# Roll up + prune posts older than 7 days, prune quotes older than 90 days
python storage-service/rollup.py --retention-days 7 --quote-retention-days 90
```

The rollup uses `INSERT ... ON CONFLICT DO UPDATE`, so it is idempotent and
safe to re-run.

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
curl -s http://localhost:8000/posts?limit=5 | jq
curl -s http://localhost:8000/stats/sentiment?hours=24 | jq
```

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

- Migrated the ingest consumers to **async `aio-pika`** with batched
  processing and explicit ack/re-queue.
- Added **async Postgres connection pooling** (`psycopg_pool`) in the API and
  storage services.
- Moved sentiment analytics and correlation into the API; added an
  **engagement-weighted sentiment trend** and **Pearson price–sentiment
  correlation** with lag sweep (`/stats/correlation`).
- Added **hourly sentiment rollup + retention pruning** (`storage-service/rollup.py`)
  with a two-tier hot/cold read path in the API.
- Added **graceful `SIGTERM`/`SIGINT` shutdown** across all services via
  `shared/runtime.py`.
- Correctness/perf fixes: UTC-consistent API time windows, bounded `hours`/`offset`
  query params, dead-WebSocket cleanup, numerically stable softmax, composite
  `stock_quotes` indexes, and a two-row layout for the dashboard stat cards.
