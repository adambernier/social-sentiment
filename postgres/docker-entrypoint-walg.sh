#!/bin/sh
set -eu

if [ "$#" -eq 0 ]; then
    set -- postgres
fi

case "${WALG_ENABLED:-false}" in
    1|true|TRUE|yes|YES|on|ON)
        if [ "$1" = "postgres" ]; then
            : "${WALG_S3_PREFIX:?WALG_S3_PREFIX is required when WAL-G is enabled}"
            : "${AWS_ENDPOINT:?AWS_ENDPOINT is required when WAL-G is enabled}"
            : "${AWS_ACCESS_KEY_ID:?AWS_ACCESS_KEY_ID is required when WAL-G is enabled}"
            : "${AWS_SECRET_ACCESS_KEY:?AWS_SECRET_ACCESS_KEY is required when WAL-G is enabled}"
            : "${WALG_LIBSODIUM_KEY:?WALG_LIBSODIUM_KEY is required when WAL-G is enabled}"
            set -- "$@" \
                -c wal_level=replica \
                -c archive_mode=on \
                -c "archive_timeout=${WALG_ARCHIVE_TIMEOUT:-60s}" \
                -c 'archive_command=wal-g wal-push "%p"'
        fi
        ;;
esac

exec /usr/local/bin/docker-entrypoint.sh "$@"
