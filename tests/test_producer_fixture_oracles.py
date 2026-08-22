"""Python behavioral oracle for fixtures consumed by the Rust adapter tests."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

FIXTURES = Path(__file__).parent / "fixtures" / "providers"


def load(provider: str) -> dict:
    return json.loads((FIXTURES / provider / "cases.json").read_text(encoding="utf-8"))


def iso_timestamp(value: str, fallback: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return fallback
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_url(raw: str) -> str:
    split = urlsplit(raw)
    host = (split.hostname or "").lower()
    port = split.port
    if port is not None and not (
        (split.scheme.lower() == "https" and port == 443)
        or (split.scheme.lower() == "http" and port == 80)
    ):
        host = f"{host}:{port}"
    query = urlencode(sorted(parse_qsl(split.query, keep_blank_values=True)))
    return urlunsplit((split.scheme.lower(), host, split.path, query, ""))


def test_recorded_fixture_tree_has_all_sanitized_outcomes():
    providers = {path.parent.name for path in FIXTURES.glob("*/cases.json")}
    assert providers == {
        "alpaca",
        "bluesky",
        "finnhub",
        "gdelt",
        "reddit",
        "stocktwits",
        "taiwan-index",
        "yahoo",
    }
    for provider in providers:
        fixture = load(provider)
        assert set(fixture["responses"]) == {
            "success",
            "empty",
            "malformed",
            "rate_limit",
            "error",
        }
        assert fixture["responses"]["rate_limit"]["status"] == 429
        request = json.dumps(fixture["request"])
        assert "<redacted>" in request or provider not in {"alpaca", "finnhub"}


def test_python_social_oracle_matches_rust_shared_expectations():
    now = "2026-08-22T12:00:00Z"

    fixture = load("bluesky")
    output = []
    for post in fixture["responses"]["success"]["body"]["posts"]:
        output.append(
            {
                "id": post["cid"],
                "symbol": "AAPL",
                "platform": "bluesky",
                "text": post["record"]["text"],
                "timestamp": iso_timestamp(post["record"]["createdAt"], now),
                "engagement": max(
                    1,
                    post.get("likeCount", 0)
                    + 2 * post.get("repostCount", 0)
                    + 3 * post.get("replyCount", 0),
                ),
            }
        )
    assert output == fixture["expected"]

    fixture = load("stocktwits")
    output = [
        {
            "id": f"st_{message['id']}",
            "symbol": "AAPL",
            "platform": "stocktwits",
            "text": message["body"],
            "timestamp": iso_timestamp(message["created_at"], now),
            "engagement": max(1, (message.get("likes") or {}).get("total", 0)),
        }
        for message in reversed(fixture["responses"]["success"]["body"]["messages"])
    ]
    assert output == fixture["expected"]

    fixture = load("reddit")
    output = []
    for child in fixture["responses"]["success"]["body"]["data"]["children"]:
        comment = child["data"]
        body = comment.get("body", "")
        if not body or not ("apple" in body.lower() or "aapl" in body.lower()):
            continue
        output.append(
            {
                "id": f"rd_{comment['id']}",
                "symbol": "AAPL",
                "platform": "reddit",
                "text": body.strip()[:2000],
                "timestamp": datetime.fromtimestamp(
                    comment["created_utc"], tz=timezone.utc
                ).isoformat().replace("+00:00", "Z"),
                "engagement": max(1, int(comment.get("score", 1))),
            }
        )
    assert output == fixture["expected"]

    for provider, prefix, engagement in (
        ("finnhub", "finnhub", 15),
        ("alpaca", "alpaca", 1),
    ):
        fixture = load(provider)
        articles = fixture["responses"]["success"]["body"]
        articles = articles if isinstance(articles, list) else articles["news"]
        output = []
        for article in articles:
            text = article["headline"].strip()
            if article.get("summary", "").strip():
                text += f". {article['summary'].strip()}"
            if not ("apple" in text.lower() or "aapl" in text.lower()):
                continue
            timestamp = (
                datetime.fromtimestamp(article["datetime"], tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
                if provider == "finnhub"
                else iso_timestamp(article.get("created_at"), now)
            )
            output.append(
                {
                    "id": f"{prefix}_{article['id']}",
                    "symbol": "AAPL",
                    "platform": provider,
                    "text": text,
                    "timestamp": timestamp,
                    "engagement": engagement,
                }
            )
        assert output == fixture["expected"]


def test_python_market_and_event_oracles_match_shared_expectations():
    fixture = load("yahoo")
    body = fixture["responses"]["success"]["body"]["chart"]["result"][0]
    quote = body["indicators"]["quote"][0]
    output = [
        {
            "timestamp": datetime.fromtimestamp(body["timestamp"][0], tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "open": quote["open"][0],
            "high": quote["high"][0],
            "low": quote["low"][0],
            "close": quote["close"][0],
            "volume": float(quote["volume"][0]),
        }
    ]
    assert output == fixture["expected"]

    fixture = load("taiwan-index")
    body = fixture["responses"]["success"]["body"]["data"]
    price_data = next(
        dataset["data"] for dataset in body["datasets"] if dataset["value_type"] == "price"
    )
    output = [
        {
            "session_date": date.replace("/", "-"),
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": None,
        }
        for date, price in zip(body["labels"], price_data, strict=True)
    ]
    assert output == fixture["expected"]

    fixture = load("gdelt")
    article = fixture["responses"]["success"]["body"]["articles"][0]
    canonical_url = normalize_url(article["url"])
    expected = fixture["expected"][0]
    assert canonical_url == expected["canonical_url"]
    assert hashlib.sha256(canonical_url.encode()).hexdigest()
    assert article["title"] == expected["title"]
    assert article["domain"] == expected["source_name"]
    assert datetime.strptime(article["seendate"], "%Y%m%dT%H%M%SZ").replace(
        tzinfo=timezone.utc
    ).isoformat().replace("+00:00", "Z") == expected["occurred_at"]
