#!/bin/sh
set -eu

: "${WALG_S3_PREFIX:?WALG_S3_PREFIX is required}"
: "${AWS_ENDPOINT:?AWS_ENDPOINT is required}"
: "${AWS_ACCESS_KEY_ID:?AWS_ACCESS_KEY_ID is required}"
: "${AWS_SECRET_ACCESS_KEY:?AWS_SECRET_ACCESS_KEY is required}"

restore_data=/var/lib/postgresql/restore-drill
restore_port=${RESTORE_DRILL_PORT:-55433}
target_time=${RESTORE_TARGET_TIME:-$(date -u -d '5 minutes ago' '+%Y-%m-%d %H:%M:%S+00')}

install -d -m 0700 -o postgres -g postgres "${restore_data}"

gosu postgres wal-g backup-fetch "${restore_data}" LATEST
touch "${restore_data}/recovery.signal"
chown postgres:postgres "${restore_data}/recovery.signal"

{
    printf "restore_command = 'wal-g wal-fetch \"%%f\" \"%%p\"'\n"
    printf "recovery_target_time = '%s'\n" "${target_time}"
    printf "recovery_target_action = 'promote'\n"
    printf "recovery_target_timeline = 'latest'\n"
} >> "${restore_data}/postgresql.auto.conf"

gosu postgres pg_ctl \
    --pgdata "${restore_data}" \
    --options "-p ${restore_port} -c listen_addresses=''" \
    --wait \
    start

gosu postgres psql \
    --host /var/run/postgresql \
    --port "${restore_port}" \
    --dbname "${POSTGRES_DB:-sentiment}" \
    --set ON_ERROR_STOP=1 \
    --command "SELECT version(), pg_is_in_recovery();" \
    --command "SELECT COUNT(*) AS applied_migrations FROM schema_migrations;" \
    --command "SELECT COUNT(*) AS canonical_facts FROM hourly_sentiment_facts;"

gosu postgres pg_ctl --pgdata "${restore_data}" --mode fast --wait stop
echo "Restore drill reached ${target_time} and passed database checks."
