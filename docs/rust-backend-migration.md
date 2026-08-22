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
- [x] Isolated live RabbitMQ/PostgreSQL fault and replay qualification harness.
- [x] Native Rust candidates for Bluesky, StockTwits, Reddit, Finnhub, Alpaca,
  market data, and global events, without Python subprocesses or an embedded
  interpreter.
- [x] Recorded provider fixtures shared by the Python oracle and Rust adapter
  suites, including success, empty, malformed, 403/429, and 5xx cases.
- [x] Independently selectable non-root producer targets in the Rust Compose
  overlay; base Compose remains the Python default and rollback.
- [x] Isolated producer capture comparison and timed observation tooling.
- [ ] Execute and sign off the replay/observation promotion gates for each worker.
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

## Worker qualification harness

`docker-compose.worker-qualification.yml` is an opt-in, isolated topology. The
pytest driver assigns every case unique queue names, random host ports, and a
unique Compose project, then removes its containers and volumes. It never uses
the production queue names or database volume. The default Compose file is not
modified and continues to select Python.

Run the deterministic CI subset for the current promotion stage:

```bash
python scripts/qualify_worker.py preprocessing --mode smoke --build
```

Run destructive RabbitMQ restart, unroutable output, publisher rejection,
PostgreSQL outage, and in-flight `SIGTERM` cases separately:

```bash
python scripts/qualify_worker.py preprocessing --mode faults
```

The recorded replay and observation gates are deliberately manual. The replay
defaults to the required 1,000 messages; observation defaults to 24 hours:

```bash
python scripts/qualify_worker.py preprocessing --mode replay
python scripts/qualify_worker.py preprocessing --mode observe
```

For a complete promotion candidate run, including smoke, faults, 1,000-message
Python/Rust comparison, and 24-hour observation:

```bash
python scripts/qualify_worker.py preprocessing --mode promotion
```

Repeat in order for `sentiment`, then `storage`. Storage promotion additionally
runs duplicate-state, transient PostgreSQL outage, and atomic retention tests.
Use `--observe-hours` or `--replay-count` only for development; release evidence
must use at least 24 hours and 1,000 messages.

The harness checks input/output/DLQ counts, persistent restart recovery,
acknowledgement through zero-ready/zero-unacknowledged convergence, compatible
DLQ headers, normalized payloads, exact labels, probability delta `<= 0.04`,
idempotent database rows, duplicate accounting, and Rust-to-Python replacement.
Failed assertions include the candidate worker's recent container logs.

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

The tested rollback command for the active stage is:

```bash
docker compose -f docker-compose.yml up -d --build preprocessing-service
```

Substitute `sentiment-service` or `storage-service` for later stages. Roll back
immediately when message counts diverge, DLQs grow unexpectedly, freshness
declines, or error rates regress; do not advance the next worker until the
current worker has completed its observation window.

## Producer candidate gates

Recorded provider fixtures are under `tests/fixtures/providers`. Request
metadata is sanitized and every provider records success, empty, malformed,
rate-limit, and error responses beside the expected normalized output. Both
runtimes consume this tree:

```bash
python scripts/verify_producer_parity.py --fixtures-only
pytest -q tests/test_producer_fixture_oracles.py
cargo test --locked -p social-news-producer -p market-producer \
  -p global-events-producer
```

Capture Python and Rust candidates only from isolated shadow queues/tables,
then require identical normalized identity keys, values, and row counts. The
harness strips generated ingestion timestamps but no provider or event time:

```bash
python scripts/qualify_producer.py bluesky --mode shadow \
  --python-jsonl artifacts/bluesky-python.jsonl \
  --rust-jsonl artifacts/bluesky-rust.jsonl \
  --replay-count 1000
```

Market/global-event table captures use their actual primary-key columns via
`--key-fields`, for example
`instrument_key,interval,starts_at` or `event_id,symbol,rule_id`.

The timed gate is intentionally separate and defaults to 24 hours. It samples
both metrics endpoints into a reviewable JSON artifact; pair it with isolated
RabbitMQ DLQ snapshots and the JSONL row/payload comparison above:

```bash
python scripts/qualify_producer.py bluesky --mode observe \
  --python-metrics-url http://127.0.0.1:18001/metrics \
  --rust-metrics-url http://127.0.0.1:28001/metrics \
  --output artifacts/bluesky-observation.json
```

Promote producers one at a time in this exact order: Bluesky → StockTwits →
Reddit → Finnhub → Alpaca → market → global events. Apply the Rust overlay and
name only the current service:

```bash
docker compose -f docker-compose.yml -f docker-compose.rust.yml \
  up -d --build bluesky-producer
```

Rollback remains one command from base Compose:

```bash
docker compose -f docker-compose.yml up -d --build bluesky-producer
```

Substitute the current producer service name. A publisher-confirm failure,
unroutable mandatory publish, reconnect failure, non-clean SIGTERM, provider or
database recovery failure, metric regression, duplicate/cursor divergence,
unexpected DLQ growth, or any capture mismatch blocks promotion.
