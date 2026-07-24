"""Validated, versioned desired state for global-context instruments."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AssetClass = Literal["index", "fx", "commodity", "us_equity"]
QuoteConvention = Literal["local_currency_per_usd"]

DEFAULT_GLOBAL_INSTRUMENT_CATALOG = Path(__file__).with_name(
    "global_instruments.yaml"
)


class CatalogInstrument(BaseModel):
    """One provider-neutral instrument definition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_key: str = Field(
        min_length=3,
        pattern=r"^[a-z0-9]+:[a-z0-9][a-z0-9-]*$",
    )
    display_name: str = Field(min_length=1)
    asset_class: AssetClass
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    exchange: str | None = None
    timezone: str = Field(min_length=1)
    provider_aliases: dict[str, str]
    session_metadata: dict[str, object]
    quote_convention: QuoteConvention | None = None
    is_active: bool = True

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown IANA timezone: {value}") from exc
        return value

    @field_validator("provider_aliases")
    @classmethod
    def validate_provider_aliases(
        cls,
        value: dict[str, str],
    ) -> dict[str, str]:
        if not value:
            raise ValueError("provider_aliases must not be empty")
        if any(not provider.strip() or not alias.strip() for provider, alias in value.items()):
            raise ValueError("provider aliases must have non-empty keys and values")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> CatalogInstrument:
        expected_prefix = {
            "index": "index:",
            "fx": "fx:",
            "commodity": "commodity:",
            "us_equity": "us-stock:",
        }[self.asset_class]
        if not self.instrument_key.startswith(expected_prefix):
            raise ValueError(
                f"{self.asset_class} instrument keys must start with "
                f"{expected_prefix!r}"
            )
        if self.asset_class == "fx":
            if self.quote_convention != "local_currency_per_usd":
                raise ValueError(
                    "FX catalog entries must use local_currency_per_usd"
                )
        elif self.quote_convention is not None:
            raise ValueError("quote_convention is only supported for FX entries")
        return self


class GlobalInstrumentCatalog(BaseModel):
    """Catalog envelope with a format version and unique instrument keys."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    instruments: list[CatalogInstrument] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_keys(self) -> GlobalInstrumentCatalog:
        keys = [instrument.instrument_key for instrument in self.instruments]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(
                "Duplicate global instrument keys: " + ", ".join(duplicates)
            )
        return self


def load_global_instrument_catalog(
    path: Path = DEFAULT_GLOBAL_INSTRUMENT_CATALOG,
) -> GlobalInstrumentCatalog:
    """Load and validate the versioned YAML catalog."""
    with path.open(encoding="utf-8") as catalog_file:
        payload = yaml.safe_load(catalog_file)
    return GlobalInstrumentCatalog.model_validate(payload)
