import logging
from prometheus_client import start_http_server, Counter

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

MESSAGES_PROCESSED_TOTAL = Counter(
    'messages_processed_total',
    'Total messages processed by internal services',
    ['service']
)

def start_metrics_server(port: int):
    try:
        start_http_server(port)
        logger.info(f"Prometheus metrics server started on port {port}")
    except Exception as e:
        logger.error(f"Failed to start Prometheus metrics server on port {port}: {e}")
