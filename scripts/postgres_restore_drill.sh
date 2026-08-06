#!/bin/sh
set -eu

: "${SPACES_WALG_RESTORE_ACCESS_KEY:?restore access key is required}"
: "${SPACES_WALG_RESTORE_SECRET_KEY:?restore secret key is required}"

compose_command=${COMPOSE_COMMAND:-docker compose}

${compose_command} --profile restore run --rm --no-deps \
    -e AWS_ACCESS_KEY_ID="${SPACES_WALG_RESTORE_ACCESS_KEY}" \
    -e AWS_SECRET_ACCESS_KEY="${SPACES_WALG_RESTORE_SECRET_KEY}" \
    postgres-restore-drill
