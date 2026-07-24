"""Versioned, single-owner database schema migrations."""

import hashlib
import logging
import random
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import psycopg

sys.path.append(str(Path(__file__).resolve().parent.parent))

from shared.config import DATABASE_DSN

logger = logging.getLogger("schema-migrations")
SCHEMA_APPLY_RETRIES = 5
SCHEMA_LOCK_TIMEOUT_MS = 5000
SCHEMA_RETRY_BASE_DELAY = 1.0
SCHEMA_ADVISORY_LOCK_KEY = 4815162342

CREATE_MIGRATION_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version TEXT PRIMARY KEY,
        checksum TEXT NOT NULL,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
"""


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path


MIGRATIONS = (
    Migration("0001_baseline", Path(__file__).parent / "schema.sql"),
    Migration(
        "0002_global_context",
        Path(__file__).parent / "0002_global_context.sql",
    ),
    Migration(
        "0003_taiwan_semiconductor_context",
        Path(__file__).parent / "0003_taiwan_semiconductor_context.sql",
    ),
)


class MigrationChecksumMismatch(RuntimeError):
    """An applied migration no longer matches the committed SQL file."""


def _prepare_migrations(
    migrations: Iterable[Migration],
) -> list[tuple[Migration, str, str]]:
    prepared = []
    versions = set()

    for migration in migrations:
        if migration.version in versions:
            raise ValueError(f"Duplicate migration version: {migration.version}")
        versions.add(migration.version)

        migration_bytes = migration.path.read_bytes()
        migration_sql = migration_bytes.decode()
        checksum = hashlib.sha256(migration_bytes).hexdigest()
        prepared.append((migration, migration_sql, checksum))

    return prepared


def _apply_pending_migrations(
    conn: psycopg.Connection,
    prepared: list[tuple[Migration, str, str]],
) -> list[str]:
    applied_now = []

    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (SCHEMA_ADVISORY_LOCK_KEY,))
        try:
            cur.execute(f"SET lock_timeout = '{SCHEMA_LOCK_TIMEOUT_MS}ms'")
            cur.execute(CREATE_MIGRATION_TABLE_SQL)
            cur.execute("SELECT version, checksum FROM schema_migrations")
            applied = dict(cur.fetchall())

            for migration, migration_sql, checksum in prepared:
                recorded_checksum = applied.get(migration.version)
                if recorded_checksum is not None:
                    if recorded_checksum != checksum:
                        raise MigrationChecksumMismatch(
                            f"Applied migration {migration.version} has changed; "
                            "add a new migration instead of editing applied SQL"
                        )
                    continue

                # Applying the SQL and recording its version are one transaction,
                # so a partially applied migration is never marked successful.
                with conn.transaction():
                    cur.execute(migration_sql)
                    cur.execute(
                        """
                        INSERT INTO schema_migrations (version, checksum)
                        VALUES (%s, %s)
                        """,
                        (migration.version, checksum),
                    )
                applied_now.append(migration.version)
        finally:
            # Closing the session also releases this lock. Keep unlock best-effort
            # so it cannot hide the original migration error.
            try:
                cur.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    (SCHEMA_ADVISORY_LOCK_KEY,),
                )
            except psycopg.Error:
                logger.debug(
                    "Could not explicitly release schema advisory lock",
                    exc_info=True,
                )

    return applied_now


def apply_migrations(
    dsn: str = DATABASE_DSN,
    *,
    migrations: Iterable[Migration] = MIGRATIONS,
) -> list[str]:
    """Apply pending migrations, retrying bounded lock-contention failures."""
    prepared = _prepare_migrations(migrations)
    last_error: Exception | None = None

    for attempt in range(1, SCHEMA_APPLY_RETRIES + 1):
        conn = None
        try:
            conn = psycopg.connect(dsn, autocommit=True)
            return _apply_pending_migrations(conn, prepared)
        except (
            psycopg.errors.DeadlockDetected,
            psycopg.errors.LockNotAvailable,
        ) as exc:
            last_error = exc
            if attempt >= SCHEMA_APPLY_RETRIES:
                break

            delay = (
                SCHEMA_RETRY_BASE_DELAY * attempt
                + random.uniform(0, SCHEMA_RETRY_BASE_DELAY)
            )
            print(
                f"Schema migration blocked by lock contention "
                f"({type(exc).__name__}); attempt "
                f"{attempt}/{SCHEMA_APPLY_RETRIES}, retrying in {delay:.1f}s..."
            )
            time.sleep(delay)
        finally:
            if conn is not None:
                conn.close()

    print(
        f"Schema migration failed after {SCHEMA_APPLY_RETRIES} attempts: "
        f"{last_error}"
    )
    if last_error is None:  # Defensive: the loop only exits early after an error.
        raise RuntimeError("Schema migration failed without an exception")
    raise last_error
