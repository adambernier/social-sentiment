import hashlib
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from schema_migrations import (
    Migration,
    MigrationChecksumMismatch,
    SCHEMA_APPLY_RETRIES,
    apply_migrations,
)


@pytest.fixture
def migration_database():
    with patch("schema_migrations.psycopg.connect") as mock_connect:
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn

        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_conn.cursor.return_value.__exit__.return_value = False
        mock_conn.transaction.return_value.__exit__.return_value = False
        mock_cursor.fetchall.return_value = []

        yield mock_connect, mock_conn, mock_cursor


@pytest.fixture
def migration(tmp_path):
    path = tmp_path / "0001_test.sql"
    path.write_text("-- test migration")
    return Migration("0001_test", path)


def test_applies_and_records_pending_migration(migration_database, migration):
    mock_connect, mock_conn, mock_cursor = migration_database

    assert apply_migrations("mock_dsn", migrations=(migration,)) == [
        "0001_test"
    ]

    mock_connect.assert_called_once_with("mock_dsn", autocommit=True)
    mock_conn.transaction.assert_called_once_with()
    mock_conn.close.assert_called_once_with()

    executed = [call.args[0] for call in mock_cursor.execute.call_args_list]
    assert "-- test migration" in executed
    assert any("INSERT INTO schema_migrations" in sql for sql in executed)
    assert any("pg_advisory_lock" in sql for sql in executed)
    assert any("pg_advisory_unlock" in sql for sql in executed)


def test_skips_migration_with_matching_record(migration_database, migration):
    _, mock_conn, mock_cursor = migration_database
    checksum = hashlib.sha256(b"-- test migration").hexdigest()
    mock_cursor.fetchall.return_value = [("0001_test", checksum)]

    assert apply_migrations("mock_dsn", migrations=(migration,)) == []

    mock_conn.transaction.assert_not_called()
    executed = [call.args[0] for call in mock_cursor.execute.call_args_list]
    assert "-- test migration" not in executed


def test_rejects_edit_to_applied_migration(migration_database, migration):
    _, mock_conn, mock_cursor = migration_database
    mock_cursor.fetchall.return_value = [("0001_test", "old-checksum")]

    with pytest.raises(MigrationChecksumMismatch, match="0001_test has changed"):
        apply_migrations("mock_dsn", migrations=(migration,))

    mock_conn.transaction.assert_not_called()


def _deadlock_on_migration(mock_cursor, fail_times):
    state = {"migration_calls": 0}

    def side_effect(statement, *args, **kwargs):
        if statement == "-- test migration":
            state["migration_calls"] += 1
            if state["migration_calls"] <= fail_times:
                raise psycopg.errors.DeadlockDetected("deadlock detected")
        return None

    mock_cursor.execute.side_effect = side_effect
    return state


def test_retries_lock_contention(migration_database, migration, monkeypatch):
    mock_connect, mock_conn, mock_cursor = migration_database
    monkeypatch.setattr("schema_migrations.time.sleep", lambda _seconds: None)
    state = _deadlock_on_migration(mock_cursor, fail_times=2)

    assert apply_migrations("mock_dsn", migrations=(migration,)) == [
        "0001_test"
    ]

    assert state["migration_calls"] == 3
    assert mock_connect.call_count == 3
    assert mock_conn.close.call_count == 3


def test_raises_after_bounded_lock_retries(
    migration_database,
    migration,
    monkeypatch,
):
    mock_connect, _, mock_cursor = migration_database
    monkeypatch.setattr("schema_migrations.time.sleep", lambda _seconds: None)
    state = _deadlock_on_migration(mock_cursor, fail_times=999)

    with pytest.raises(psycopg.errors.DeadlockDetected):
        apply_migrations("mock_dsn", migrations=(migration,))

    assert state["migration_calls"] == SCHEMA_APPLY_RETRIES
    assert mock_connect.call_count == SCHEMA_APPLY_RETRIES


def test_rejects_duplicate_migration_versions(tmp_path):
    first = tmp_path / "first.sql"
    second = tmp_path / "second.sql"
    first.write_text("SELECT 1")
    second.write_text("SELECT 2")

    with pytest.raises(ValueError, match="Duplicate migration version"):
        apply_migrations(
            "mock_dsn",
            migrations=(
                Migration("0001_duplicate", first),
                Migration("0001_duplicate", second),
            ),
        )
