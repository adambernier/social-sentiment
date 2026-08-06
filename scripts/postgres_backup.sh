#!/bin/sh
set -eu

compose_command=${COMPOSE_COMMAND:-docker compose}

${compose_command} exec -T -u postgres postgres \
    wal-g backup-push /var/lib/postgresql/data
${compose_command} exec -T -u postgres postgres wal-g backup-list --pretty --detail
${compose_command} exec -T -u postgres postgres wal-g wal-show
