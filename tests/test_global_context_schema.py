from pathlib import Path

from schema_migrations import MIGRATIONS

MIGRATION_SQL = (
    Path(__file__).resolve().parents[1] / "storage-service" / "0002_global_context.sql"
).read_text()


def test_global_context_migration_is_registered_after_baseline():
    assert [migration.version for migration in MIGRATIONS] == [
        "0001_baseline",
        "0002_global_context",
    ]


def test_schema_uses_stable_keys_and_retention_indexes():
    normalized = " ".join(MIGRATION_SQL.lower().split())

    assert "instrument_key text primary key" in normalized
    assert "primary key (instrument_key, interval, starts_at)" in normalized
    assert "global_market_bars_retention_idx" in normalized
    assert "global_event_signals_occurred_at_idx" in normalized
    assert "references tracked_symbols(symbol) on delete cascade" in normalized
    assert "provider_aliases jsonb" in normalized
    assert "unique (symbol, name)" in normalized


def test_seed_contains_the_complete_initial_universe_and_fx_orientation():
    for key in (
        "index:nikkei-225",
        "index:hang-seng",
        "index:csi-300",
        "index:taiwan-weighted",
        "index:kospi",
        "index:nifty-50",
        "fx:usd-jpy",
        "fx:usd-cnh",
        "fx:usd-krw",
        "fx:usd-twd",
        "fx:usd-inr",
        "commodity:gold",
        "commodity:brent-crude",
        "commodity:copper",
    ):
        assert f"'{key}'" in MIGRATION_SQL

    assert MIGRATION_SQL.count("'local_currency_per_usd'") == 5
    assert "ON CONFLICT (instrument_key) DO NOTHING" in MIGRATION_SQL
