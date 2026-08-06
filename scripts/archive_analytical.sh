#!/bin/sh
set -eu

compose_command=${COMPOSE_COMMAND:-docker compose}
purge_days=${ANALYTICS_ARCHIVE_PURGE_DAYS:-7}

if [ "$#" -eq 0 ]; then
    set -- --purge-archived-staging-days "${purge_days}"
fi

${compose_command} --profile archive run --rm archive-service \
    python archive-service/export.py "$@"
