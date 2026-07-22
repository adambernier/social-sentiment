import importlib
import os
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row


api_main = importlib.import_module("api-service.main")

TEST_DATABASE_DSN = os.environ.get("TEST_DATABASE_DSN")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_DSN,
    reason="TEST_DATABASE_DSN is required for dashboard query tests",
)


@pytest.fixture
def dashboard_database():
    schema_name = f"dashboard_{uuid.uuid4().hex}"
    conn = psycopg.connect(
        TEST_DATABASE_DSN,
        autocommit=True,
        row_factory=dict_row,
    )
    conn.execute(
        sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name))
    )
    conn.execute(
        sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name))
    )
    conn.execute("""
        CREATE TABLE posts (
            id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            platform TEXT NOT NULL,
            text TEXT NOT NULL,
            timestamp TIMESTAMPTZ NOT NULL,
            sentiment TEXT NOT NULL,
            scores JSONB NOT NULL,
            topic_id INTEGER,
            topic_label TEXT,
            scored_at TIMESTAMPTZ NOT NULL,
            engagement INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE stock_quotes (
            symbol TEXT NOT NULL,
            timestamp TIMESTAMPTZ NOT NULL,
            price DOUBLE PRECISION NOT NULL,
            volume BIGINT NOT NULL,
            market_session TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX stock_quotes_symbol_timestamp_idx
            ON stock_quotes (symbol, timestamp DESC)
    """)
    conn.execute("""
        CREATE INDEX stock_quotes_symbol_session_timestamp_idx
            ON stock_quotes (symbol, market_session, timestamp DESC)
    """)
    conn.execute("""
        CREATE TABLE stock_metrics (
            symbol TEXT PRIMARY KEY,
            pe_ratio DOUBLE PRECISION,
            beta DOUBLE PRECISION,
            avg_return_1y DOUBLE PRECISION,
            inflation_adj_return_1y DOUBLE PRECISION,
            pe_relative_sector DOUBLE PRECISION,
            beta_relative_sector DOUBLE PRECISION,
            return_relative_sector DOUBLE PRECISION,
            updated_at TIMESTAMPTZ NOT NULL
        )
    """)

    try:
        yield conn
    finally:
        conn.close()
        with psycopg.connect(TEST_DATABASE_DSN, autocommit=True) as cleanup:
            cleanup.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(
                    sql.Identifier(schema_name)
                )
            )


def test_dashboard_queries_execute_and_preserve_latest_bucket_rows(
    dashboard_database,
):
    conn = dashboard_database
    now = datetime(2026, 7, 22, 16, 6, tzinfo=timezone.utc)
    scored_at = now - timedelta(minutes=1)
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO posts (
                id, symbol, platform, text, timestamp, sentiment, scores,
                topic_id, topic_label, scored_at, engagement
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    "reddit-post",
                    "NVDA",
                    "reddit",
                    "NVDA earnings",
                    now - timedelta(minutes=2),
                    "positive",
                    '{"positive": 0.9}',
                    1,
                    "Earnings & Guidance",
                    scored_at,
                    4,
                ),
                (
                    "bluesky-post",
                    "NVDA",
                    "bluesky",
                    "NVDA launch",
                    now - timedelta(minutes=3),
                    "neutral",
                    '{"neutral": 0.8}',
                    2,
                    "Products & Innovation",
                    scored_at,
                    2,
                ),
            ],
        )
        cur.executemany(
            """
            INSERT INTO stock_quotes (
                symbol, timestamp, price, volume, market_session
            )
            VALUES (%s, %s, %s, %s, %s)
            """,
            [
                ("NVDA", now - timedelta(hours=20), 100.0, 900, "regular"),
                ("NVDA", now.replace(minute=1), 108.0, 1000, "regular"),
                ("NVDA", now.replace(minute=4), 109.0, 1100, "regular"),
                ("NVDA", now, 110.0, 1200, "regular"),
                ("NQ=F", now - timedelta(hours=20), 200.0, 1900, "futures_open"),
                ("NQ=F", now, 205.0, 2000, "futures_open"),
                (api_main.VIX_SYMBOL, now - timedelta(hours=20), 17.0, 400, "regular"),
                (api_main.VIX_SYMBOL, now, 18.0, 500, "regular"),
            ],
        )
        cur.execute(
            """
            INSERT INTO stock_metrics (
                symbol, pe_ratio, beta, avg_return_1y,
                inflation_adj_return_1y, pe_relative_sector,
                beta_relative_sector, return_relative_sector, updated_at
            )
            VALUES ('NVDA', 25.0, 1.2, 0.3, 0.27, -0.1, -0.2, 0.4, %s)
            """,
            [now],
        )

    content_query, content_params = api_main._dashboard_content_query(
        "NVDA",
        now - timedelta(days=1),
        "reddit",
    )
    content = conn.execute(content_query, content_params).fetchone()
    assert [post["id"] for post in content["posts"]] == ["reddit-post"]
    assert content["sentiment_stats"] == [
        {"sentiment": "positive", "count": 1}
    ]
    assert content["topic_stats"] == [
        {"topic_label": "Earnings & Guidance", "count": 1}
    ]

    series = conn.execute(
        api_main.DASHBOARD_MARKET_SERIES_QUERY,
        [5, ["NVDA", "NQ=F"], now - timedelta(hours=2)],
    ).fetchall()
    nvda_series = [row for row in series if row["symbol"] == "NVDA"]
    assert [(row["timestamp"].minute, row["price"]) for row in nvda_series] == [
        (4, 109.0),
        (6, 110.0),
    ]

    snapshots = conn.execute(
        api_main.DASHBOARD_MARKET_SNAPSHOT_QUERY,
        [["NVDA", "NQ=F", api_main.VIX_SYMBOL]],
    ).fetchall()
    by_symbol = {row["symbol"]: row for row in snapshots}
    assert by_symbol["NVDA"]["latest_price"] == 110.0
    assert by_symbol["NVDA"]["reference_price"] == 100.0
    assert by_symbol["NVDA"]["metrics_symbol"] == "NVDA"
    assert by_symbol["NQ=F"]["latest_price"] == 205.0
    assert by_symbol["NQ=F"]["reference_price"] == 200.0
    assert by_symbol[api_main.VIX_SYMBOL]["latest_price"] == 18.0
