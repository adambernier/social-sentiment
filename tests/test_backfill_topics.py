import sys
from unittest.mock import MagicMock

import backfill_topics


def test_backfill_updates_duplicate_source_ids_by_internal_primary_key(
    monkeypatch,
):
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [
        (101, "shared-id", "first tracked symbol"),
        (202, "shared-id", "second tracked symbol"),
    ]
    topic_model = MagicMock()
    topic_model.predict.side_effect = [
        (1, "Earnings & Guidance"),
        (2, "Fed & Macro"),
    ]
    monkeypatch.setattr(backfill_topics.psycopg, "connect", lambda _dsn: conn)
    monkeypatch.setattr(backfill_topics, "TopicModel", lambda: topic_model)
    monkeypatch.setattr(sys, "argv", ["backfill_topics.py"])

    backfill_topics.main()

    select_sql = " ".join(cursor.execute.call_args.args[0].lower().split())
    assert "select post_pk, id, text from posts" in select_sql

    update_sql, updates = cursor.executemany.call_args.args
    normalized_update_sql = " ".join(update_sql.lower().split())
    assert "where post_pk = %s" in normalized_update_sql
    assert "where id = %s" not in normalized_update_sql
    assert updates == [
        ("Earnings & Guidance", 1, 101),
        ("Fed & Macro", 2, 202),
    ]
    conn.commit.assert_called_once_with()
