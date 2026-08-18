# Rust backend migration

Python remains the production default and behavioral oracle until the gates in
this document pass. PostgreSQL migrations, RabbitMQ queue names and payloads,
environment variables, the public `/api` contract, and the Next.js frontend
remain shared across runtimes.

## Runtime inventory

The backend has three queue workers, one API, and seven ingestion producers:

1. Bluesky (publishes `RawPost`)
2. StockTwits (publishes `RawPost`)
3. Reddit (publishes `RawPost`, optional profile)
4. Finnhub news (publishes `RawPost`)
5. Alpaca news (publishes `RawPost`)
6. market data (writes normalized quotes, metrics, instruments, and bars)
7. global events (writes normalized event signals and links)

The Rust preprocessing, sentiment, and storage workers are available in
`docker-compose.rust.yml`. The Rust API candidate is a side-by-side service in
the `rust-api` profile and never replaces the Python API implicitly.

## Current implementation status

- [x] Shared Rust schemas, configuration, observability, shutdown handling,
  confirmed mandatory publishing, reconnect loops, and DLQ naming.
- [x] Rust preprocessing, sentiment, and scored-post storage workers.
- [x] SQLx 0.8 migration, removing the SQLx 0.7 future-Rust warning.
- [x] Model parity gate (exact labels, maximum probability delta `0.04`).
- [x] Rust public dashboard, posts, sentiment, topic, source, leaderboard,
  market, metrics, correlation, health, metrics, and WebSocket routes.
- [x] Rust tracked-symbol admin CRUD with fail-closed `X-API-Key` auth.
- [x] Atomic global exposure and event-rule replacement routes.
- [x] Global-context read calculations for 30/90 sessions, lag selection,
  beta, strength, event reactions, and freshness.
- [x] Side-by-side API contract harness with timestamp normalization and
  `1e-6` absolute numeric tolerance.
- [ ] Live RabbitMQ/PostgreSQL fault and replay qualification for each worker.
- [ ] Provider adapters and fixture suites for all seven Rust producers.
- [ ] Per-service 1,000-message shadow comparison and 24-hour observation.
- [ ] Rust API browser smoke, load, and 48-hour observation gates.
- [ ] Final full-stack 48-hour soak and default-runtime switch.

Unchecked items are release gates, not optional follow-up work. In particular,
the Rust overlay does not prove a service is ready for production promotion.

## Local gates

Run both worker configurations:

```bash
docker compose config --quiet
docker compose -f docker-compose.yml -f docker-compose.rust.yml config --quiet
```

Start the Python API and side-by-side Rust candidate against the same database:

```bash
docker compose -f docker-compose.yml -f docker-compose.rust.yml \
  --profile rust-api up -d --build api-service rust-api-service
python scripts/verify_api_contract.py \
  --python-url http://127.0.0.1:8000 \
  --rust-url http://127.0.0.1:8001
```

The contract gate compares every read-only application route and the OpenAPI
route/method inventory. Admin mutation, rollback, database-outage, and
WebSocket cases belong in the PostgreSQL/RabbitMQ integration suite because
they intentionally change state.

## Promotion and rollback gates

Workers are promoted one at a time in this order: preprocessing, sentiment,
storage, social/news producers, market producer, global-events producer. Each
worker must pass malformed, duplicate, retry, dead-letter, reconnect,
publisher-confirm, unroutable-message, transient dependency, and shutdown
requeue tests before a 1,000-message shadow and 24-hour observation.

The API is promoted only after route parity, admin authorization and rollback,
global-context calculations, browser smoke tests, and representative load
tests pass. A p95 latency regression above 10% blocks promotion. After a
48-hour API observation and a final 48-hour full-stack soak, Compose may be
changed to make Rust the default.

At every stage the default Compose file is the one-command Python rollback.
Message loss, unexplained DLQ growth, row-count divergence, stale sources,
contract mismatch, or failed rollback blocks the release. Python runtime
implementations remain for one release after final cutover; schema migration
and archive-export Python utilities are retained operationally.

