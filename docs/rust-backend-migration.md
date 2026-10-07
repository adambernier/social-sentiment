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
- [x] Provider session record/replay and shadow-queue capture tooling for the
  search-feed producers.
- [x] Gate evidence records naming the source revision, container images, and
  host that each qualification run exercised.
- [x] Sustained-load worker gate comparing Python and Rust p95 latency and
  storage drain time under the same publish rate.
- [x] Worker replay parity gate: 1,000-message Python/Rust comparison for
  preprocessing, sentiment, and storage.
- [x] Worker observation gate: 24-hour stability runs for preprocessing and
  sentiment.
- [ ] Worker observation gate: 24-hour stability run for storage.
- [ ] Producer shadow parity gate: 1,000-record Python/Rust comparison for each
  of the seven producers.
- [ ] Producer observation gate: 24-hour stability run for each of the seven
  producers.
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

The recorded replay, sustained-load, and observation gates are deliberately
manual. The replay defaults to the required 1,000 messages; the load gate
defaults to 2,000 messages per runtime at 50 messages per second; observation
defaults to 24 hours:

```bash
python scripts/qualify_worker.py preprocessing --mode replay
python scripts/qualify_worker.py preprocessing --mode load
python scripts/qualify_worker.py preprocessing --mode observe
```

For a complete promotion candidate run, including smoke, faults, 1,000-message
Python/Rust comparison, sustained load, and 24-hour observation:

```bash
python scripts/qualify_worker.py preprocessing --mode promotion
```

Repeat in order for `sentiment`, then `storage`. Storage promotion additionally
runs duplicate-state, transient PostgreSQL outage, and atomic retention tests.
Use `--observe-hours`, `--replay-count`, `--load-count`, or `--load-rate` only
for development; release evidence must use at least 24 hours and 1,000 messages.

The harness checks input/output/DLQ counts, persistent restart recovery,
acknowledgement through zero-ready/zero-unacknowledged convergence, compatible
DLQ headers, normalized payloads, exact labels, probability delta `<= 0.04`,
idempotent database rows, duplicate accounting, and Rust-to-Python replacement.
Failed assertions include the candidate worker's recent container logs.

The 1,000-message replay gate is the payload-parity gate: it drives identical
messages through both runtimes and requires identical output keys and normalized
payloads, exact sentiment labels, and a probability delta `<= 0.04`. Storage has
no output queue, so its replay compares persisted rows and duplicate accounting
between the runtimes instead.

The sustained-load gate is the throughput gate. It publishes the same paced load
at the same rate through each runtime and compares end-to-end p95 latency for
preprocessing and sentiment, and total drain time for storage, which has no
output queue to time against. Rust may not regress past `--load-tolerance`
(10% by default), and separately may not exceed an absolute floor
(`QUALIFICATION_LOAD_LATENCY_FLOOR_MS`, 5 ms by default;
`QUALIFICATION_LOAD_DURATION_FLOOR_SECONDS`, 2 s for storage), so a comparison
of sub-millisecond numbers cannot fail on noise. Each runtime run also asserts
every message was handled, the input queue drained to zero ready and zero
unacknowledged, the dead-letter queue stayed empty, and, for storage, exact row
count and zero duplicate accounting. Choose a `--load-rate` at or below the
production rate: a worker that keeps up with a rate it can sustain passes, and
one that cannot grows a backlog and fails on latency or drain time.

The 24-hour observation gate is not a liveness-only check. Throughout the
window it asserts the candidate container is running and that both the input
queue and its `.dead-letter` queue hold zero ready and zero unacknowledged
messages, and it publishes a uniquely identified probe on a separate cadence
and requires that record to reach the output queue or the `posts` table. The
window closes on a final verified probe rather than on a clock check, so a
worker that stops consuming mid-window fails the gate instead of passing it as
an idle container behind an empty queue. Storage probes carry the current
timestamp rather than the recorded fixture time, because the storage worker
rolls up and prunes posts older than `POST_RETENTION_DAYS` (one day in the
qualification topology) 60 seconds after startup and every 24 hours after that,
and would otherwise archive the probes out of `posts` mid-window. Storage also
asserts exact row count and zero duplicate accounting across all probes.

Every gate writes a JSON evidence record to `artifacts/` naming the candidate it
exercised: `worker-replay-<worker>.json`, `worker-load-<worker>.json`, and
`worker-observation-<worker>.json`. Each record carries the source commit and
whether the working tree was dirty, the Compose project that ran the gate, the
image reference and image ID of the runtime containers that actually ran, the
host and platform, the measured result, and a `status` of `passed` or `failed`.
A gate removes its record when it starts and writes one when it finishes either
way, so a missing artifact means the run never completed rather than that it
failed quietly, and a retained record with `status: failed` carries the error.
Retain the records with the promotion record; a record with a missing revision
or an unexpected image ID is not evidence that the current candidate passed.

The project name belongs in that evidence because it is what separates a fresh
build from a reused one: a run given `QUALIFICATION_PROJECT_NAME` reports that
name instead of a random one, and its images are evidence only while the service
source is unchanged since they were built.

The replay and load records also carry a `margin`, the room the candidate had
under the limit it had to hold: the metric, the value observed, the limit, and
the headroom between them. Replay reports the largest probability delta it
measured against the `0.04` tolerance, or, for the workers compared by exact
payload equality, a delta of zero against a limit that admits no deviation at
all. Load reports the candidate's p95 latency, or storage drain time, against
whichever of the tolerance and the absolute floor was binding, since either one
passes the gate. A zero headroom therefore means the candidate passed exactly,
not that it passed comfortably. The observation gate has no margin: it passes on
liveness and probe evidence rather than against a threshold.

Cadence, thresholds, and output paths are overridable for development with
`QUALIFICATION_OBSERVE_PROBE_INTERVAL` (seconds, default 300),
`QUALIFICATION_OBSERVE_PROBE_TIMEOUT` (seconds, default 120),
`QUALIFICATION_LOAD_RATE` (messages per second, default 50),
`QUALIFICATION_LOAD_P95_TOLERANCE` (default 0.10),
`QUALIFICATION_OBSERVE_ARTIFACT`, `QUALIFICATION_REPLAY_ARTIFACT`, and
`QUALIFICATION_LOAD_ARTIFACT` (output paths).

`QUALIFICATION_PROJECT_NAME` runs a gate against an existing Compose project
instead of a fresh one, so a host that already holds that project's images does
not rebuild them. Reuse is only sound while the service source and
`Dockerfile.rust` are unchanged since those images were built — confirm with
`git log --since=<image date> -- crates/ preprocessing-service/ sentiment-service/
storage-service/ Dockerfile.rust` before citing a reused image as evidence.

## Promotion and rollback gates

Workers are promoted one at a time in this order: preprocessing, sentiment,
storage, social/news producers, market producer, global-events producer. Each
worker must pass malformed, duplicate, retry, dead-letter, reconnect,
publisher-confirm, unroutable-message, transient dependency, and shutdown
requeue tests before a 1,000-message shadow, a sustained load at or below the
production rate, and a 24-hour observation.

The API is promoted only after route parity, admin authorization and rollback,
global-context calculations, browser smoke tests, and representative load
tests pass. A p95 latency regression above 10% blocks promotion, as it does for
the worker load gate. After a 48-hour API observation and a final 48-hour
full-stack soak, Compose may be changed to make Rust the default.

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

### Search-feed shadow captures

The search-feed producers (Bluesky, StockTwits, Reddit) poll mutable,
cursor-driven feeds: engagement counters drift between fetches and the two
runtimes' poll windows race, so two independent live runs cannot produce the
identical record set the shadow comparator requires. Record one provider
session and replay it to each runtime, then drain each runtime's isolated
output queue:

1. Record a session, taking the Python reference capture in the same pass.
   `provider_replay.py record` forwards every provider request to the live
   upstream and appends the exchange to the session:

   ```bash
   python scripts/provider_replay.py record --provider bluesky \
     --session artifacts/bluesky-session.jsonl --port 8611
   BLUESKY_API_BASE=http://127.0.0.1:8611 \
   QUEUE_RAW_POSTS=shadow.bluesky.python python bluesky-producer/main.py
   python scripts/capture_shadow_queue.py --queue shadow.bluesky.python \
     --count 1000 --output artifacts/bluesky-python.jsonl
   ```

2. Replay the session to the Rust candidate with a fresh replay server (each
   server serves the session from the beginning):

   ```bash
   python scripts/provider_replay.py replay \
     --session artifacts/bluesky-session.jsonl --port 8611
   BLUESKY_API_BASE=http://127.0.0.1:8611 \
   QUEUE_RAW_POSTS=shadow.bluesky.rust \
     cargo run --locked --release -p social-news-producer --bin producer-bluesky
   python scripts/capture_shadow_queue.py --queue shadow.bluesky.rust \
     --count 1000 --output artifacts/bluesky-rust.jsonl
   ```

3. Compare as for any capture:

   ```bash
   python scripts/qualify_producer.py bluesky --mode shadow \
     --python-jsonl artifacts/bluesky-python.jsonl \
     --rust-jsonl artifacts/bluesky-rust.jsonl --replay-count 1000
   ```

`BLUESKY_API_BASE`, `STOCKTWITS_API_BASE`, and `REDDIT_FEED_URL` are the
overrides for the three search-feed producers; they default to the production
endpoints, so an unset environment behaves exactly as before. Once a session's
recorded exchanges are exhausted the replay server answers with a
provider-shaped empty payload; a request for a key that was never recorded
fails loudly. Run the two runtimes for one provider sequentially or on
separate hosts: they bind the same per-provider metrics port, and whichever
loses the bind only logs the failure. Sessions and captures belong under
`artifacts/` (gitignored) with the promotion record; add `--host 0.0.0.0` and
a host-reachable address (for example `host.docker.internal`) when the
runtimes run in containers.

`tests/test_capture_shadow_queue_integration.py` exercises the capture tool
end to end against an isolated broker when `SHADOW_CAPTURE_BROKER_URL` is
set.

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
