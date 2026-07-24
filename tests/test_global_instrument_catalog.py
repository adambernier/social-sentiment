import importlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from shared.global_instrument_catalog import (
    CatalogInstrument,
    GlobalInstrumentCatalog,
    load_global_instrument_catalog,
)

catalog_sync = importlib.import_module(
    "market-producer.sync_global_instruments"
)


def test_catalog_contains_unique_provider_independent_universe():
    catalog = load_global_instrument_catalog()
    instruments = {
        instrument.instrument_key: instrument
        for instrument in catalog.instruments
    }

    assert catalog.version == 1
    assert len(instruments) == len(catalog.instruments) == 15
    assert instruments["index:taiwan-semiconductor"].provider_aliases == {
        "taiwan_index": "IX0143",
        "yahoo": "IX0143.TW",
    }
    assert sum(
        instrument.quote_convention == "local_currency_per_usd"
        for instrument in catalog.instruments
    ) == 5
    nvda_exposure = next(
        exp for exp in catalog.stock_exposures if exp.symbol == "NVDA"
    )
    assert [exp.instrument_key for exp in nvda_exposure.exposures] == [
        "index:taiwan-semiconductor",
        "index:nikkei-225",
    ]


def test_catalog_rejects_duplicate_stable_keys():
    instrument = CatalogInstrument(
        instrument_key="index:test",
        display_name="Test",
        asset_class="index",
        currency="USD",
        timezone="UTC",
        provider_aliases={"fixture": "TEST"},
        session_metadata={},
    )

    with pytest.raises(ValidationError, match="Duplicate global instrument keys"):
        GlobalInstrumentCatalog(
            version=1,
            instruments=[instrument, instrument],
        )


def test_catalog_rejects_unknown_exposure_keys():
    instrument = CatalogInstrument(
        instrument_key="index:test",
        display_name="Test",
        asset_class="index",
        currency="USD",
        timezone="UTC",
        provider_aliases={"fixture": "TEST"},
        session_metadata={},
    )

    with pytest.raises(ValidationError, match="references unknown instrument_key"):
        GlobalInstrumentCatalog(
            version=1,
            instruments=[instrument],
            stock_exposures=[
                {
                    "symbol": "NVDA",
                    "exposures": [
                        {
                            "instrument_key": "index:nonexistent",
                            "reason": "Test",
                            "display_order": 1,
                        }
                    ],
                }
            ],
        )


def test_catalog_loader_rejects_unknown_fields(tmp_path: Path):
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(
        """
version: 1
unexpected: true
instruments: []
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_global_instrument_catalog(catalog_path)


def test_sync_command_uses_validated_catalog_and_closes_connection(monkeypatch):
    database = MagicMock()
    database.sync_global_instrument_catalog.side_effect = len
    database.sync_stock_factor_exposures.return_value = 2
    monkeypatch.setattr(catalog_sync, "DB", lambda: database)

    assert catalog_sync.sync_global_instruments() == 15
    synced = database.sync_global_instrument_catalog.call_args.args[0]
    assert synced[4].instrument_key == "index:taiwan-semiconductor"
    database.sync_stock_factor_exposures.assert_called_once()
    database.conn.close.assert_called_once_with()

