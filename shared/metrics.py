import logging

from prometheus_client import Counter, Gauge, start_http_server

logger = logging.getLogger("metrics")

POSTS_INGESTED_TOTAL = Counter(
    'posts_ingested_total', 
    'Total number of posts/messages ingested', 
    ['platform', 'symbol']
)

RATE_LIMITS_HIT_TOTAL = Counter(
    'rate_limits_hit_total', 
    'Total number of API rate limits encountered', 
    ['platform']
)


def initialize_rate_limit_metrics(*platforms: str) -> None:
    """Expose an explicit zero before a provider encounters its first limit."""
    for platform in platforms:
        RATE_LIMITS_HIT_TOTAL.labels(platform=platform).inc(0)

MESSAGES_PROCESSED_TOTAL = Counter(
    'messages_processed_total',
    'Total messages processed by internal services',
    ['service']
)

GLOBAL_PROVIDER_REQUESTS_TOTAL = Counter(
    "global_context_provider_requests_total",
    "Global-context provider requests by type and result.",
    ["provider", "data_type", "status"],
)

GLOBAL_LAST_SUCCESS_TIMESTAMP = Gauge(
    "global_context_last_success_timestamp_seconds",
    "Unix timestamp of the latest successful global-context ingestion.",
    ["provider", "data_type"],
)

GLOBAL_BACKFILL_PROGRESS = Gauge(
    "global_context_backfill_progress_ratio",
    "Per-instrument global-context backfill completion ratio.",
    ["provider", "instrument_key"],
)

GLOBAL_RULE_MATCHES_TOTAL = Counter(
    "global_context_rule_matches_total",
    "Events linked by an explicit global-context rule.",
    ["provider", "symbol"],
)

GLOBAL_RELATIONSHIP_SAMPLE_SUFFICIENT = Gauge(
    "global_context_relationship_sample_sufficient",
    "Whether a factor relationship has the minimum required sample.",
    ["symbol", "instrument_key", "horizon_sessions"],
)

def start_metrics_server(port: int):
    try:
        start_http_server(port)
        logger.info(f"Prometheus metrics server started on port {port}")
    except Exception as e:  # noqa: BLE001 - metrics failure must not stop a service
        logger.error(f"Failed to start Prometheus metrics server on port {port}: {e}")
