# social-sentiment

A message-queue pipeline that ingests posts from Bluesky, cleans them, scores
their sentiment with a transformer model, stores the result in Postgres, and
integrates real-time financial metrics to provide a comprehensive stock
performance dashboard.

```text
[Social Media]
      │
      ▼
 (raw-posts)
      │
      ▼
[Preprocessing]
      │
      ▼
 (clean-posts)
      │
      ▼
[Sentiment]
      │
      ▼
 (scored-posts)
      │
      ▼
  [Storage]                [Market Data]
      │                          │
      └──────▶ [(Postgres)] ◀────┘
                    │
                    ▼
              [API Service]
                    │
                    ▼
               [UI Service]
```

## Services

| Service                 | Role                                                                  |
| ----------------------- | --------------------------------------------------------------------- |
| `bluesky-producer`      | Polls the public Bluesky search API for keywords, publishes raw posts |
| `market-producer`       | Fetches live price/volume and hourly relative-to-sector metrics       |
| `preprocessing-service` | Cleans text, drops too-short posts                                    |
| `sentiment-service`     | Scores posts with a RoBERTa sentiment model                           |
| `storage-service`       | Idempotent insert into Postgres (`posts.id` is the primary key)       |
| `api-service`           | FastAPI read API for posts, sentiment stats, and financial metrics    |

Keywords for the producers live in `bluesky-producer/keywords.json`.

## Features

### 📊 Relative Performance Scorecard
Inspired by hockey stat cards, the dashboard features a **Divergent Bar Chart** that shows how a stock is performing relative to its sector peers:
- **Valuation (P/E):** Relative to sector average (inverted: lower is better).
- **Risk (Beta):** Relative to sector average (inverted: lower volatility is better).
- **Returns (1Y):** Annual return vs. sector ETF performance.

### 🧠 Sentiment Engine
- Uses a **RoBERTa transformer model** fine-tuned for financial sentiment.
- Automatically calculates **Retail vs. Institutional Divergence** by comparing social media chatter against news headlines.

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

The whole stack — RabbitMQ, Postgres, and all five services — is orchestrated
by `docker-compose.yml`:

```bash
docker compose up -d --build
docker compose ps
```

First boot of `sentiment-service` downloads the RoBERTa model (~500MB) into the
`hf_cache` named volume; subsequent restarts reuse it.

Useful endpoints once the stack is up:
- API docs: http://localhost:8000/docs
- Latest posts: http://localhost:8000/posts
- 24h sentiment stats: http://localhost:8000/stats/sentiment
- Financial metrics: http://localhost:8000/stats/metrics?symbol=INTC
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
- Sentiment logs a label and confidence per post.
- Storage logs `inserted` for each — re-running the publisher logs
  `skipped (duplicate)` for the same IDs.

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

The `ui-service` provides a real-time Streamlit dashboard.

Terminal 5 — UI:
```bash
streamlit run ui-service/app.py
```

Once running, access the dashboard at: http://localhost:8501

## Troubleshooting

- **Service exits with `AMQPConnectionError`** — RabbitMQ isn't ready yet.
  Compose's healthcheck gates dependents, so this should be rare; if it
  happens, `docker compose logs rabbitmq` and wait for `Server startup
  complete`.
- **`psycopg.OperationalError: connection refused`** — Postgres isn't up, or
  you're running a service locally and pointing at the wrong host. Check
  `DATABASE_DSN`.
- **Queues piling up in the management UI** — the downstream consumer is down
  or stuck. `docker compose ps` to find the offender; `docker compose logs -f
  <service>` for details.
- **Want to start clean** — `docker compose down -v` drops `postgres_data` and
  `hf_cache`. Or, less destructively, purge queues from the management UI and
  `TRUNCATE posts;` in Postgres.
