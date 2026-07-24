"""API contracts and queries for the opt-in global-context panel."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from shared.global_context import (
    DailyClose,
    calculate_relationship,
    next_close_move,
)
from shared.metrics import GLOBAL_RELATIONSHIP_SAMPLE_SUFFICIENT


class ExposureInput(BaseModel):
    instrument_key: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=1000)
    display_order: int = Field(default=0, ge=0)


class ExposureReplacement(BaseModel):
    exposures: list[ExposureInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def reject_duplicates(self):
        keys = [exposure.instrument_key for exposure in self.exposures]
        if len(keys) != len(set(keys)):
            raise ValueError("instrument_key values must be unique")
        return self


class EventRuleInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    countries: list[str] = Field(default_factory=list, max_length=30)
    themes: list[str] = Field(default_factory=list, max_length=30)
    query_terms: list[str] = Field(default_factory=list, max_length=30)
    is_active: bool = True

    @model_validator(mode="after")
    def validate_rule(self):
        if not (self.countries or self.themes or self.query_terms):
            raise ValueError(
                "each rule needs at least one country, theme, or query term"
            )
        for values in (self.countries, self.themes, self.query_terms):
            if any(not value.strip() for value in values):
                raise ValueError("rule match values cannot be blank")
            if len(values) != len(set(values)):
                raise ValueError("rule match values must be unique")
        return self


class EventRuleReplacement(BaseModel):
    rules: list[EventRuleInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def reject_duplicate_names(self):
        names = [rule.name for rule in self.rules]
        if len(names) != len(set(names)):
            raise ValueError("rule names must be unique")
        return self


class LagRelationshipResponse(BaseModel):
    lag_sessions: int
    correlation: float | None
    beta: float | None
    sample_count: int


class RelationshipResponse(BaseModel):
    correlation: float | None
    beta: float | None
    selected_lag: int | None
    sample_count: int
    strength: Literal["weak", "moderate", "strong"] | None
    lag_statistics: list[LagRelationshipResponse]


class GlobalFactorResponse(BaseModel):
    instrument_key: str
    display_name: str
    asset_class: Literal["index", "fx", "commodity"]
    currency: str
    exchange: str | None
    timezone: str
    quote_convention: str | None
    exposure_reason: str
    current_price: float | None
    current_move_pct: float | None
    current_as_of: datetime | None
    fetched_at: datetime | None
    provider: str | None
    relationship: RelationshipResponse


class GlobalEventResponse(BaseModel):
    id: int
    title: str
    summary: str | None
    canonical_url: str | None
    source_name: str | None
    occurred_at: datetime
    provider: str
    rule_names: list[str]
    match_reasons: list[dict]
    next_close_move_pct: float | None
    reaction_label: Literal["next-close move"] = "next-close move"


class GlobalFreshnessResponse(BaseModel):
    latest_factor_at: datetime | None
    latest_daily_at: datetime | None
    latest_event_at: datetime | None
    status: Literal["fresh", "stale", "empty"]


class GlobalContextResponse(BaseModel):
    symbol: str
    configured: bool
    horizon_sessions: Literal[30, 90]
    as_of: datetime
    currency_orientation: str
    disclaimer: str
    factors: list[GlobalFactorResponse]
    events: list[GlobalEventResponse]
    freshness: GlobalFreshnessResponse


FACTOR_SNAPSHOT_QUERY = """
    SELECT exposure.instrument_key, exposure.reason AS exposure_reason,
           exposure.display_order, instrument.display_name,
           instrument.asset_class, instrument.currency, instrument.exchange,
           instrument.timezone, instrument.quote_convention,
           latest.close_price AS current_price,
           latest.ends_at AS current_as_of,
           latest.fetched_at, latest.provider,
           reference.close_price AS reference_price
    FROM stock_factor_exposures AS exposure
    JOIN global_instruments AS instrument
      ON instrument.instrument_key = exposure.instrument_key
    LEFT JOIN LATERAL (
        SELECT bar.close_price, bar.ends_at, bar.fetched_at, bar.interval,
               bar.provider
        FROM global_market_bars AS bar
        WHERE bar.instrument_key = exposure.instrument_key
        ORDER BY bar.ends_at DESC,
                 CASE WHEN bar.interval = '1h' THEN 0 ELSE 1 END
        LIMIT 1
    ) AS latest ON TRUE
    LEFT JOIN LATERAL (
        SELECT bar.close_price
        FROM global_market_bars AS bar
        WHERE bar.instrument_key = exposure.instrument_key
          AND bar.interval = '1d'
          AND bar.ends_at < latest.ends_at
        ORDER BY bar.ends_at DESC
        LIMIT 1
    ) AS reference ON TRUE
    WHERE exposure.symbol = %s
      AND instrument.is_active
      AND instrument.asset_class <> 'us_equity'
    ORDER BY exposure.display_order, instrument.display_name
"""

DAILY_HISTORY_QUERY = """
    SELECT instrument_key, ends_at, close_price, fetched_at
    FROM global_market_bars
    WHERE interval = '1d'
      AND instrument_key = ANY(%s)
      AND ends_at >= NOW() - INTERVAL '2 years'
    ORDER BY instrument_key, ends_at
"""

EVENTS_QUERY = """
    SELECT signal.id, signal.title, signal.summary, signal.canonical_url,
           signal.source_name, signal.occurred_at, signal.provider,
           signal.ingested_at,
           jsonb_agg(rule.name ORDER BY rule.name) AS rule_names,
           jsonb_agg(link.match_reason ORDER BY rule.name) AS match_reasons
    FROM stock_event_links AS link
    JOIN global_event_signals AS signal ON signal.id = link.event_id
    JOIN global_event_rules AS rule ON rule.id = link.rule_id
    WHERE link.symbol = %s
      AND signal.occurred_at >= NOW() - INTERVAL '30 days'
    GROUP BY signal.id
    ORDER BY signal.occurred_at DESC
    LIMIT 30
"""


def _row_value(row: dict, key: str):
    return row.get(key)


def _current_move(row: dict) -> float | None:
    current = _row_value(row, "current_price")
    reference = _row_value(row, "reference_price")
    if current is None or reference is None or reference <= 0:
        return None
    return (current / reference - 1.0) * 100.0


def _freshness_status(
    latest_factor_at: datetime | None,
    as_of: datetime,
) -> Literal["fresh", "stale", "empty"]:
    if latest_factor_at is None:
        return "empty"
    if (as_of - latest_factor_at).total_seconds() > 72 * 3600:
        return "stale"
    return "fresh"


async def build_global_context(
    conn,
    *,
    symbol: str,
    horizon_sessions: Literal[30, 90],
) -> GlobalContextResponse:
    as_of = datetime.now(timezone.utc)
    async with conn.cursor() as cursor:
        await cursor.execute(FACTOR_SNAPSHOT_QUERY, [symbol])
        factor_rows = await cursor.fetchall()
        if not factor_rows:
            return GlobalContextResponse(
                symbol=symbol,
                configured=False,
                horizon_sessions=horizon_sessions,
                as_of=as_of,
                currency_orientation=(
                    "Asian FX is local-currency units per USD; "
                    "positive means local-currency weakness."
                ),
                disclaimer="Measured association, not causation or a forecast.",
                factors=[],
                events=[],
                freshness=GlobalFreshnessResponse(
                    latest_factor_at=None,
                    latest_daily_at=None,
                    latest_event_at=None,
                    status="empty",
                ),
            )

        factor_keys = [row["instrument_key"] for row in factor_rows]
        target_key = f"us-stock:{symbol}"
        await cursor.execute(
            DAILY_HISTORY_QUERY,
            [factor_keys + [target_key]],
        )
        history_rows = await cursor.fetchall()
        await cursor.execute(EVENTS_QUERY, [symbol])
        event_rows = await cursor.fetchall()

    history: dict[str, list[DailyClose]] = defaultdict(list)
    latest_daily_at = None
    for row in history_rows:
        history[row["instrument_key"]].append(
            DailyClose(
                ends_at=row["ends_at"],
                close_price=row["close_price"],
            )
        )
        fetched_at = row.get("fetched_at")
        if fetched_at is not None and (
            latest_daily_at is None or fetched_at > latest_daily_at
        ):
            latest_daily_at = fetched_at

    target_closes = history[target_key]
    factors: list[GlobalFactorResponse] = []
    latest_factor_at = None
    for row in factor_rows:
        relationship = calculate_relationship(
            history[row["instrument_key"]],
            target_closes,
            horizon_sessions=horizon_sessions,
        )
        sufficient = relationship.correlation is not None
        GLOBAL_RELATIONSHIP_SAMPLE_SUFFICIENT.labels(
            symbol=symbol,
            instrument_key=row["instrument_key"],
            horizon_sessions=str(horizon_sessions),
        ).set(1 if sufficient else 0)
        fetched_at = row.get("fetched_at")
        if fetched_at is not None and (
            latest_factor_at is None or fetched_at > latest_factor_at
        ):
            latest_factor_at = fetched_at
        factors.append(
            GlobalFactorResponse(
                instrument_key=row["instrument_key"],
                display_name=row["display_name"],
                asset_class=row["asset_class"],
                currency=row["currency"],
                exchange=row.get("exchange"),
                timezone=row["timezone"],
                quote_convention=row.get("quote_convention"),
                exposure_reason=row["exposure_reason"],
                current_price=row.get("current_price"),
                current_move_pct=_current_move(row),
                current_as_of=row.get("current_as_of"),
                fetched_at=fetched_at,
                provider=row.get("provider"),
                relationship=RelationshipResponse(
                    correlation=relationship.correlation,
                    beta=relationship.beta,
                    selected_lag=relationship.selected_lag,
                    sample_count=relationship.sample_count,
                    strength=relationship.strength,
                    lag_statistics=[
                        LagRelationshipResponse(
                            lag_sessions=lag.lag_sessions,
                            correlation=lag.correlation,
                            beta=lag.beta,
                            sample_count=lag.sample_count,
                        )
                        for lag in relationship.lag_statistics
                    ],
                ),
            )
        )

    events = [
        GlobalEventResponse(
            id=row["id"],
            title=row["title"],
            summary=row.get("summary"),
            canonical_url=row.get("canonical_url"),
            source_name=row.get("source_name"),
            occurred_at=row["occurred_at"],
            provider=row["provider"],
            rule_names=list(row["rule_names"]),
            match_reasons=list(row["match_reasons"]),
            next_close_move_pct=next_close_move(
                target_closes,
                row["occurred_at"],
            ),
        )
        for row in event_rows
    ]
    latest_event_at = max(
        (row["ingested_at"] for row in event_rows),
        default=None,
    )

    return GlobalContextResponse(
        symbol=symbol,
        configured=True,
        horizon_sessions=horizon_sessions,
        as_of=as_of,
        currency_orientation=(
            "Asian FX is local-currency units per USD; "
            "positive means local-currency weakness."
        ),
        disclaimer="Measured association, not causation or a forecast.",
        factors=factors,
        events=events,
        freshness=GlobalFreshnessResponse(
            latest_factor_at=latest_factor_at,
            latest_daily_at=latest_daily_at,
            latest_event_at=latest_event_at,
            status=_freshness_status(latest_factor_at, as_of),
        ),
    )
