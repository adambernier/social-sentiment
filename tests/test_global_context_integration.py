import importlib
import os
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from schema_migrations import MIGRATIONS, apply_migrations

TEST_DATABASE_DSN = os.environ.get("TEST_DATABASE_DSN")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_DSN,
    reason="TEST_DATABASE_DSN is required for global-context integration tests",
)

events_adapter = importlib.import_module("global-events-producer.adapter")
events_main = importlib.import_module("global-events-producer.main")
context_api = importlib.import_module("api-service.global_context_api")
storage_db = importlib.import_module("db")


@pytest.fixture
def global_context_database():
    schema_name = f"global_context_{uuid.uuid4().hex}"
    with psycopg.connect(TEST_DATABASE_DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
    scoped_dsn = make_conninfo(
        TEST_DATABASE_DSN,
        options=f"-csearch_path={schema_name}",
    )
    apply_migrations(scoped_dsn)
    with psycopg.connect(scoped_dsn, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO tracked_symbols (symbol, keywords)
            VALUES ('NVDA', '[]'::jsonb)
            """
        )
    try:
        yield scoped_dsn
    finally:
        with psycopg.connect(TEST_DATABASE_DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name))
            )


def test_migration_seeds_provider_independent_universe_and_constraints(
    global_context_database,
):
    with psycopg.connect(global_context_database, autocommit=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM global_instruments").fetchone() == (
            15,
        )
        assert conn.execute(
            """
            SELECT COUNT(*) FROM global_instruments
            WHERE asset_class = 'fx'
              AND quote_convention = 'local_currency_per_usd'
            """
        ).fetchone() == (5,)
        aliases = conn.execute(
            """
            SELECT provider_aliases
            FROM global_instruments
            WHERE instrument_key = 'index:nikkei-225'
            """
        ).fetchone()[0]
        assert aliases == {"yahoo": "^N225"}
        taiwan_aliases = conn.execute(
            """
            SELECT provider_aliases
            FROM global_instruments
            WHERE instrument_key = 'index:taiwan-semiconductor'
            """
        ).fetchone()[0]
        assert taiwan_aliases == {
            "taiwan_index": "IX0143",
            "yahoo": "IX0143.TW",
        }

        starts_at = datetime(2026, 7, 23, tzinfo=timezone.utc)
        values = [
            "index:nikkei-225",
            "1d",
            starts_at,
            starts_at + timedelta(hours=6),
            starts_at.date(),
            100,
            102,
            99,
            101,
            1000,
            "fixture",
        ]
        conn.execute(
            """
            INSERT INTO global_market_bars (
                instrument_key, interval, starts_at, ends_at, session_date,
                open_price, high_price, low_price, close_price, volume,
                provider
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            values,
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                """
                INSERT INTO global_market_bars (
                    instrument_key, interval, starts_at, ends_at, session_date,
                    open_price, high_price, low_price, close_price, volume,
                    provider
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                values,
            )


def test_taiwan_semiconductor_migration_replaces_existing_nvda_exposure(
    global_context_database,
):
    with psycopg.connect(global_context_database, autocommit=True) as conn:
        conn.execute(
            """
            DELETE FROM schema_migrations
            WHERE version = '0003_taiwan_semiconductor_context'
            """
        )
        conn.execute(
            """
            DELETE FROM global_instruments
            WHERE instrument_key = 'index:taiwan-semiconductor'
            """
        )
        conn.execute(
            """
            INSERT INTO stock_factor_exposures (
                symbol, instrument_key, reason, display_order
            )
            VALUES ('NVDA', 'index:taiwan-weighted', 'Taiwan context', 2)
            """
        )

    assert apply_migrations(
        global_context_database,
        migrations=(MIGRATIONS[-1],),
    ) == ["0003_taiwan_semiconductor_context"]

    with psycopg.connect(global_context_database, autocommit=True) as conn:
        assert conn.execute(
            """
            SELECT instrument_key, reason, display_order
            FROM stock_factor_exposures
            WHERE symbol = 'NVDA'
            """
        ).fetchall() == [
            (
                "index:taiwan-semiconductor",
                "Taiwan semiconductor manufacturing and supply-chain context",
                2,
            )
        ]


def test_backfill_symbol_source_reads_only_active_tracked_symbols(
    global_context_database,
):
    with psycopg.connect(global_context_database, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO tracked_symbols (symbol, keywords, is_active)
            VALUES ('AAPL', '[]'::jsonb, false)
            """
        )
        database = storage_db.DB.__new__(storage_db.DB)
        database.conn = conn

        assert database.list_active_symbols() == ["NVDA"]


@pytest.mark.asyncio
async def test_event_links_are_deduplicated_and_capped_per_utc_day(
    global_context_database,
):
    with psycopg.connect(global_context_database, autocommit=True) as conn:
        rule_id = conn.execute(
            """
            INSERT INTO global_event_rules (
                symbol, name, countries, themes, query_terms
            )
            VALUES (
                'NVDA', 'Asia supply', '["Taiwan"]'::jsonb,
                '["SUPPLY_CHAIN"]'::jsonb, '["semiconductor"]'::jsonb
            )
            RETURNING id
            """
        ).fetchone()[0]
    rule = events_adapter.EventRule(
        id=rule_id,
        symbol="NVDA",
        name="Asia supply",
        countries=("Taiwan",),
        themes=("SUPPLY_CHAIN",),
        query_terms=("semiconductor",),
    )
    occurred_at = datetime(2026, 7, 23, 4, tzinfo=timezone.utc)
    events = [
        events_adapter.NormalizedEventSignal(
            provider="fixture",
            provider_event_id=f"event-{index}",
            canonical_url=f"https://example.com/{index}",
            title=f"Event {index}",
            summary=None,
            source_name="example.com",
            occurred_at=occurred_at + timedelta(hours=index),
            countries=("Taiwan",),
            themes=("SUPPLY_CHAIN",),
        )
        for index in range(2)
    ]

    async with await psycopg.AsyncConnection.connect(
        global_context_database,
        autocommit=True,
    ) as conn:
        assert (
            await events_main.store_rule_events(
                conn,
                rule,
                events,
                cap_per_day=1,
            )
            == 1
        )
        assert (
            await events_main.store_rule_events(
                conn,
                rule,
                events,
                cap_per_day=1,
            )
            == 0
        )
        assert (
            await events_main.store_rule_events(
                conn,
                rule,
                events,
                cap_per_day=2,
            )
            == 1
        )
        assert (
            await events_main.store_rule_events(
                conn,
                rule,
                events,
                cap_per_day=2,
            )
            == 0
        )
        url_duplicate = events_adapter.NormalizedEventSignal(
            provider="fixture",
            provider_event_id="replacement-provider-id",
            canonical_url=events[0].canonical_url,
            title="Updated event title",
            summary=None,
            source_name="example.com",
            occurred_at=events[0].occurred_at,
            countries=("Taiwan",),
            themes=("SUPPLY_CHAIN",),
        )
        assert (
            await events_main.store_rule_events(
                conn,
                rule,
                [url_duplicate],
                cap_per_day=3,
            )
            == 0
        )

    with psycopg.connect(global_context_database, autocommit=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM global_event_signals").fetchone() == (
            2,
        )
        assert conn.execute("SELECT COUNT(*) FROM stock_event_links").fetchone() == (2,)
        assert conn.execute(
            """
            SELECT title
            FROM global_event_signals
            WHERE canonical_url = 'https://example.com/0'
            """
        ).fetchone() == ("Updated event title",)


@pytest.mark.asyncio
async def test_api_query_aligns_seeded_daily_history(
    global_context_database,
):
    factor_key = "index:nikkei-225"
    target_key = "us-stock:NVDA"
    start = datetime(2026, 1, 2, 8, tzinfo=timezone.utc)
    with psycopg.connect(global_context_database, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO global_instruments (
                instrument_key, display_name, asset_class, currency, timezone,
                provider_aliases, session_metadata
            )
            VALUES (
                %s, 'NVDA', 'us_equity', 'USD', 'America/New_York',
                '{"yahoo":"NVDA"}'::jsonb,
                '{"open":"09:30","close":"16:00"}'::jsonb
            )
            """,
            [target_key],
        )
        conn.execute(
            """
            INSERT INTO stock_factor_exposures (
                symbol, instrument_key, reason, display_order
            )
            VALUES ('NVDA', %s, 'Asia session', 0)
            """,
            [factor_key],
        )
        factor_price = 100.0
        stock_price = 100.0
        rows = []
        returns = [0.01, -0.005, 0.013, -0.009, 0.004] * 6
        for index, daily_return in enumerate([0.0, *returns]):
            if index:
                factor_price *= 1 + daily_return
                stock_price *= 1 + daily_return
            for instrument_key, end, price in (
                (
                    factor_key,
                    start + timedelta(days=index),
                    factor_price,
                ),
                (
                    target_key,
                    start.replace(hour=21) + timedelta(days=index),
                    stock_price,
                ),
            ):
                rows.append(
                    (
                        instrument_key,
                        "1d",
                        end - timedelta(hours=6),
                        end,
                        end.date(),
                        price,
                        price,
                        price,
                        price,
                        1000,
                        "fixture",
                    )
                )
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO global_market_bars (
                    instrument_key, interval, starts_at, ends_at, session_date,
                    open_price, high_price, low_price, close_price, volume,
                    provider
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                rows,
            )

    async with await psycopg.AsyncConnection.connect(
        global_context_database,
        autocommit=True,
        row_factory=dict_row,
    ) as conn:
        response = await context_api.build_global_context(
            conn,
            symbol="NVDA",
            horizon_sessions=30,
        )

    assert response.configured is True
    assert response.factors[0].relationship.sample_count >= 20
    assert response.factors[0].relationship.correlation is not None
