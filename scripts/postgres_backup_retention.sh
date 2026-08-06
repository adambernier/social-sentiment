#!/bin/sh
set -eu

: "${SPACES_WALG_MAINTENANCE_ACCESS_KEY:?maintenance access key is required}"
: "${SPACES_WALG_MAINTENANCE_SECRET_KEY:?maintenance secret key is required}"

compose_command=${COMPOSE_COMMAND:-docker compose}
retain_full=${WALG_RETAIN_FULL_BACKUPS:-35}

${compose_command} exec -T -u postgres \
    -e AWS_ACCESS_KEY_ID="${SPACES_WALG_MAINTENANCE_ACCESS_KEY}" \
    -e AWS_SECRET_ACCESS_KEY="${SPACES_WALG_MAINTENANCE_SECRET_KEY}" \
    postgres wal-g delete retain FULL "${retain_full}" \
    --use-sentinel-time --confirm
