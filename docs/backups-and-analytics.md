# Backups and analytical retention

This deployment protects two different assets:

- WAL-G base backups plus continuous WAL provide PostgreSQL point-in-time
  recovery (PITR).
- Verified Parquet objects preserve portable analytical facts, as-of fact
  snapshots, market facts, and an explicitly permitted cleaned-text sample.

Neither tier replaces the other. PostgreSQL recovery is the operational path;
Parquet is the long-lived analytical path.

## Production defaults

| Control | Default |
| --- | --- |
| Cleaned posts in PostgreSQL | 14 days |
| Minute-level stock quotes | 90 days |
| Sentiment fact grain | hour × symbol × platform × topic/model identities × aggregate version |
| Full base backup | daily |
| WAL archive timeout | 60 seconds |
| Full backups retained | 35 daily backups (at least the 30-day target) |
| Restore objective | exercise a restore that completes within two hours |
| Probability sample | deterministic 1% |
| Challenge sample | engagement ≥100 or absolute signal ≥0.8 |
| Text archive sources | none until a platform is explicitly allowlisted |
| Parquet export | previous closed UTC month, monthly |

The operational target is roughly a one-minute RPO during write activity and a
two-hour RTO. These are objectives, not guarantees, until production backup
size and quarterly restore-drill measurements confirm them.

## DigitalOcean Spaces setup

Use private origin endpoints; do not enable the Spaces CDN or put CloudFront in
front of either prefix. Backups are write-once/recovery traffic, so edge caching
does not improve the normal path and creates another access surface.

Create dedicated backup and analytics Spaces, or at minimum isolated prefixes:

```text
s3://sentiment-backups/postgres/production
s3://sentiment-analytics/analytics/production
```

Enable S3 Versioning through the Spaces API. Versioning is disabled by default:

```bash
aws s3api put-bucket-versioning \
  --bucket sentiment-backups \
  --endpoint https://sfo3.digitaloceanspaces.com \
  --versioning-configuration Status=Enabled

aws s3api get-bucket-versioning \
  --bucket sentiment-backups \
  --endpoint https://sfo3.digitaloceanspaces.com
```

Repeat for the analytics Space. Configure lifecycle cleanup for incomplete
multipart uploads, but do not expire current backup versions independently of
WAL-G retention.

Spaces has no bucket-level encryption setting. This implementation therefore
does not depend on an implicit provider setting:

- WAL-G encrypts before upload with a 32-byte libsodium key.
- The Parquet exporter uses AES-256 SSE-C with a separate 32-byte key.
- HTTPS protects both paths in transit.

Generate and escrow the keys separately from the Droplet:

```bash
openssl rand -base64 32  # WALG_LIBSODIUM_KEY
openssl rand -base64 32  # ANALYTICS_SSE_C_KEY_B64
```

Losing either key makes its objects unrecoverable. Keep an offline or
independently controlled copy and include key recovery in every drill.

Create separate limited-access Spaces keys for:

- PostgreSQL/WAL-G runtime uploads.
- Backup retention maintenance.
- Restore drills.
- Analytical exports.

DigitalOcean's limited-key permission bundles may combine write and delete.
Separate keys still improve rotation and exposure boundaries, while object
versioning protects against ordinary overwrite/delete mistakes. Do not put the
maintenance or restore credentials in the long-running PostgreSQL container.

## Activate WAL-G

Copy `.env.example` to `.env`, set the production endpoint/prefix/keys, set
`PIPELINE_GIT_COMMIT` to the deployed commit SHA, and leave `WALG_ENABLED=false`
for the first image rollout.

Build and migrate first:

```bash
docker compose build postgres schema-migrate archive-service
docker compose up -d postgres
docker compose run --rm schema-migrate
```

Then set `WALG_ENABLED=true` and recreate PostgreSQL. Startup fails closed when
the storage prefix, upload credentials, or encryption key is absent.

```bash
docker compose up -d --force-recreate postgres
scripts/postgres_backup.sh
scripts/postgres_backup_health.sh
```

Verify `archive_mode`, the one-minute timeout, and successful archive activity:

```bash
docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "SHOW archive_mode" \
  -c "SHOW archive_timeout" \
  -c "SELECT * FROM pg_stat_archiver"
```

Install the supplied timers after adjusting `/opt/social-sentiment` if the
deployment path differs:

```bash
sudo install -m 0644 ops/systemd/social-sentiment-backup.* /etc/systemd/system/
sudo install -m 0644 ops/systemd/social-sentiment-backup-retention.* /etc/systemd/system/
sudo install -m 0644 ops/systemd/social-sentiment-analytics-archive.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now \
  social-sentiment-backup.timer \
  social-sentiment-backup-retention.timer \
  social-sentiment-analytics-archive.timer
```

The service files read secrets from root-controlled files under
`/etc/social-sentiment/`. Set their mode to `0600` and never commit them.

## Restore drill

Run at least quarterly and after any PostgreSQL, WAL-G, encryption, or storage
configuration change:

```bash
scripts/postgres_restore_drill.sh
```

By default the isolated container restores the latest base backup and replays
to five minutes before the drill. To test an incident timestamp:

```bash
RESTORE_TARGET_TIME='2026-08-05 12:00:00+00' \
  scripts/postgres_restore_drill.sh
```

The drill starts PostgreSQL on an isolated port, checks the server/recovery
state, applied migrations, and canonical fact count, then stops it. Record the
elapsed time and alert if it exceeds two hours. A successful `backup-push` is
not sufficient evidence of recoverability.

## Analytical data contract

`hourly_sentiment_facts` stores additive counts, class-probability sums and
squares, continuous signal sums and squares, engagement/weight components, and
event/ingest/score/finalization times. Nonlinear indexes and variances are
derived only after the requested rows are summed.

Topic and sentiment identities remain separate. Human model aliases are stored
beside immutable artifact SHA-256 values and registered in
`model_artifacts`/`model_artifact_aliases`. Topic `0` is real, `-1` is the
General/Outlier class, and `-2` is unassigned. `agg_version` is part of the fact
identity.

Every retention run appends the resulting changed facts to
`hourly_sentiment_fact_snapshots` with a run ID and `as_of` time. Backtests that
claim historical availability must use these snapshots or a versioned row in
`analytical_signal_snapshots`, not a later revision of the canonical fact.

The legacy `hourly_sentiment_agg` table remains valid only for unfiltered
history. Platform/topic-filtered API requests never use it and return coverage
metadata that exposes their shorter dimension-preserving history.

## Retention and sampling

The daily retention transaction performs four operations atomically:

1. Select rows strictly older than the hour-aligned 14-day boundary.
2. Stage permitted probability/challenge samples.
3. Delete exactly those rows.
4. Upsert additive canonical and legacy facts and append as-of snapshots.

A failed partition or fact write rolls back the raw-post deletion. Monthly
partitions are prepared for every expiring event month plus the next three
months.

`RAW_ARCHIVE_PLATFORMS` is empty by default. Add a source only after its terms,
privacy expectations, and intended retention have been reviewed. The archive
contains cleaned text—not original producer payloads. Engagement is the value
observed at ingestion time and is not refreshed later.

The probability and challenge cohorts are deliberately separate. Only the 1%
probability cohort carries an inclusion probability suitable for prevalence
estimation. The enriched challenge cohort is for model evaluation and must not
be treated as representative.

## Parquet archive

Run the prior closed month manually with:

```bash
scripts/archive_analytical.sh
```

Export one month or dataset explicitly:

```bash
scripts/archive_analytical.sh --month 2026-07 \
  --dataset hourly_sentiment_facts
```

The exporter writes explicit Arrow schemas, Zstandard-compressed Parquet,
content-addressed object keys, SHA-256 metadata, object version IDs, row/time
ranges, and a verified `archive_manifests` row. Canonical/as-of sentiment facts,
derived signal snapshots, quality facts, model registries, hourly/daily market
facts, and permitted sample rows are separate datasets. Sample staging rows are
marked archived only after the uploaded object passes size and
checksum-metadata checks. Re-running after late data creates another
content-addressed revision.

Do not detach/drop PostgreSQL fact partitions yet. The API currently reads
PostgreSQL rather than Parquet, so verified objects are protection and portable
analytical history, not an online query substitute.

## Monitoring and alerts

Alert on:

- `pg_stat_archiver.failed_count` increasing or `last_failed_wal` becoming set.
- Archive freshness beyond five minutes during active writes.
- No successful daily base backup within 36 hours.
- WAL-G `wal-show` gaps or integrity errors.
- PostgreSQL volume pressure.
- Failed systemd backup, retention, archive, or restore-drill units.
- Parquet uploads without a verified manifest, and pending sample staging that
  remains unarchived after the monthly export.

Use `scripts/postgres_backup_health.sh` for the WAL/base-backup checks and keep
the resulting logs outside the database being protected.
