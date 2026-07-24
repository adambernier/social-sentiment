"""Idempotently synchronize the versioned global-instrument catalog."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))
sys.path.append(str(ROOT / "storage-service"))

try:
    from storage_service.db import DB
except ModuleNotFoundError:  # Repository layout uses ``storage-service``.
    from db import DB

from shared.global_instrument_catalog import (
    DEFAULT_GLOBAL_INSTRUMENT_CATALOG,
    load_global_instrument_catalog,
)


def sync_global_instruments(
    catalog_path: Path = DEFAULT_GLOBAL_INSTRUMENT_CATALOG,
) -> int:
    """Validate the catalog and atomically upsert all listed instruments and stock exposures."""
    catalog = load_global_instrument_catalog(catalog_path)
    database = DB()
    try:
        instruments_count = database.sync_global_instrument_catalog(catalog.instruments)
        database.sync_stock_factor_exposures(catalog.stock_exposures)
        return instruments_count
    finally:
        if database.conn is not None:
            database.conn.close()



def main() -> None:
    parser = argparse.ArgumentParser(
        description="Synchronize provider-neutral global instrument metadata",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=DEFAULT_GLOBAL_INSTRUMENT_CATALOG,
        help="Path to the versioned YAML catalog",
    )
    args = parser.parse_args()
    synced = sync_global_instruments(args.catalog)
    print(f"Synced {synced} global instruments from {args.catalog}")


if __name__ == "__main__":
    main()
