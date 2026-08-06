#!/bin/sh
set -eu

compose_command=${COMPOSE_COMMAND:-docker compose}
max_archive_age_seconds=${WALG_MAX_ARCHIVE_AGE_SECONDS:-300}

${compose_command} exec -T postgres psql \
    --username "${POSTGRES_USER:-postgres}" \
    --dbname "${POSTGRES_DB:-sentiment}" \
    --set ON_ERROR_STOP=1 \
    --command "SELECT archived_count, failed_count, last_archived_time, last_failed_time, last_failed_wal FROM pg_stat_archiver;" \
    --command "SELECT CASE WHEN last_archived_time >= NOW() - make_interval(secs => ${max_archive_age_seconds}) THEN 'ok' ELSE 'stale' END AS archive_freshness FROM pg_stat_archiver;"

${compose_command} exec -T -u postgres postgres wal-g backup-list --pretty --detail
${compose_command} exec -T -u postgres postgres wal-g wal-show
