"""Geopolitical event adapter contract and GDELT pilot implementation."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

import httpx

GDELT_DOC_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
_UNSAFE_QUERY_CHARACTER = re.compile(r"[^\w .,'&+:/()-]", re.UNICODE)


@dataclass(frozen=True)
class EventRule:
    id: int
    symbol: str
    name: str
    countries: tuple[str, ...]
    themes: tuple[str, ...]
    query_terms: tuple[str, ...]


@dataclass(frozen=True)
class NormalizedEventSignal:
    provider: str
    provider_event_id: str
    canonical_url: str
    title: str
    summary: str | None
    source_name: str | None
    occurred_at: datetime
    countries: tuple[str, ...]
    themes: tuple[str, ...]


class EventProviderRateLimited(RuntimeError):
    def __init__(self, retry_after_seconds: int | None = None):
        super().__init__("event provider rate limited")
        self.retry_after_seconds = retry_after_seconds


class GlobalEventAdapter(Protocol):
    provider_name: str

    async def fetch(
        self,
        rule: EventRule,
        *,
        since: datetime,
        max_results: int,
    ) -> list[NormalizedEventSignal]:
        """Fetch event signals selected by one explicit curated rule."""


def _query_atom(value: str) -> str:
    cleaned = _UNSAFE_QUERY_CHARACTER.sub(" ", value).strip()
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        raise ValueError("event query values cannot be empty")
    return f'"{cleaned}"'


def build_gdelt_query(rule: EventRule) -> str:
    """Build a deterministic query; no model-based matching is involved."""
    groups: list[str] = []
    if rule.query_terms:
        groups.append(
            "(" + " OR ".join(_query_atom(term) for term in rule.query_terms) + ")"
        )
    if rule.countries:
        groups.append(
            "(" + " OR ".join(_query_atom(country) for country in rule.countries) + ")"
        )
    if rule.themes:
        groups.append(
            "("
            + " OR ".join(f"theme:{_query_atom(theme)}" for theme in rule.themes)
            + ")"
        )
    if not groups:
        raise ValueError("event rule must contain a country, theme, or term")
    return " AND ".join(groups)


def _parse_gdelt_timestamp(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    for pattern in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _retry_after_seconds(response: httpx.Response) -> int | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(int(raw), 0)
    except ValueError:
        return None


class GdeltEventAdapter:
    provider_name = "gdelt"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def fetch(
        self,
        rule: EventRule,
        *,
        since: datetime,
        max_results: int,
    ) -> list[NormalizedEventSignal]:
        response = await self.client.get(
            GDELT_DOC_ENDPOINT,
            params={
                "query": build_gdelt_query(rule),
                "mode": "artlist",
                "format": "json",
                "sort": "datedesc",
                "maxrecords": max(1, min(max_results, 250)),
                "startdatetime": since.astimezone(timezone.utc).strftime(
                    "%Y%m%d%H%M%S"
                ),
            },
        )
        if response.status_code == 429:
            raise EventProviderRateLimited(_retry_after_seconds(response))
        response.raise_for_status()
        payload = response.json()
        output: list[NormalizedEventSignal] = []
        seen_urls: set[str] = set()
        for article in payload.get("articles", []):
            url = str(article.get("url") or "").strip()
            title = str(article.get("title") or "").strip()
            if not url or not title or url in seen_urls:
                continue
            seen_urls.add(url)
            output.append(
                NormalizedEventSignal(
                    provider=self.provider_name,
                    provider_event_id=hashlib.sha256(url.encode("utf-8")).hexdigest(),
                    canonical_url=url,
                    title=title[:1000],
                    summary=None,
                    source_name=(str(article.get("domain")).strip()[:255] or None),
                    occurred_at=_parse_gdelt_timestamp(article.get("seendate")),
                    countries=rule.countries,
                    themes=rule.themes,
                )
            )
        return output
