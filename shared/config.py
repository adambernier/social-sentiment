import os


def get_env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def get_env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


def get_env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except ValueError:
        return default


def get_env_csv(key: str, default: str = "") -> tuple[str, ...]:
    value = os.environ.get(key, default)
    return tuple(item.strip() for item in value.split(",") if item.strip())


def get_env_bool(key: str, default: bool = False) -> bool:
    value = os.environ.get(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# RabbitMQ Config
RABBIT_HOST = get_env("RABBITMQ_HOST", "localhost")
RABBIT_PORT = get_env_int("RABBITMQ_PORT", 5672)
RABBIT_USER = get_env("RABBITMQ_USER", "guest")
RABBIT_PASS = get_env("RABBITMQ_PASS", "guest")

# Queue Names
QUEUE_RAW_POSTS = get_env("QUEUE_RAW_POSTS", "raw-posts")
QUEUE_CLEAN_POSTS = get_env("QUEUE_CLEAN_POSTS", "clean-posts")
QUEUE_SCORED_POSTS = get_env("QUEUE_SCORED_POSTS", "scored-posts")
QUEUE_TOPIC_POSTS = get_env("QUEUE_TOPIC_POSTS", "topic-posts")

# Database Config
DATABASE_DSN = get_env(
    "DATABASE_DSN", "postgresql://postgres:sentiment@localhost:5432/sentiment"
)

# Retention and analytical archive defaults. Cleaned post text is only copied
# into long-lived archive staging for explicitly allowlisted source platforms.
POST_RETENTION_DAYS = max(1, get_env_int("POST_RETENTION_DAYS", 14))
QUOTE_RETENTION_DAYS = max(1, get_env_int("QUOTE_RETENTION_DAYS", 90))
RAW_ARCHIVE_PLATFORMS = get_env_csv("RAW_ARCHIVE_PLATFORMS")
RAW_ARCHIVE_SAMPLE_RATE = min(
    1.0,
    max(0.0, get_env_float("RAW_ARCHIVE_SAMPLE_RATE", 0.01)),
)
RAW_ARCHIVE_CHALLENGE_ENGAGEMENT = max(
    0,
    get_env_int("RAW_ARCHIVE_CHALLENGE_ENGAGEMENT", 100),
)
RAW_ARCHIVE_CHALLENGE_ABS_SIGNAL = min(
    1.0,
    max(0.0, get_env_float("RAW_ARCHIVE_CHALLENGE_ABS_SIGNAL", 0.8)),
)

# Global context is deliberately opt-in. The same switch gates provider work
# and API contracts; the browser panel also has a build-time flag.
GLOBAL_CONTEXT_ENABLED = get_env_bool("GLOBAL_CONTEXT_ENABLED", False)

# Reddit Config
REDDIT_USER_AGENT = get_env(
    "REDDIT_USER_AGENT", "social-sentiment-rss/0.1 (sentiment dashboard)"
)

# Finnhub Config (news producer) — free API key from https://finnhub.io
FINNHUB_API_KEY = get_env("FINNHUB_API_KEY", "")

# Market-wide volatility signal. Spot VIX index (^VIX) — has rich 1-minute data,
# and the 15/25 stress thresholds in shared.futures are spot-VIX levels. Defined
# here (a dependency-light module) so the producers and the API share one source
# of truth for the symbol stored in / queried from stock_quotes.
VIX_SYMBOL = get_env("VIX_SYMBOL", "^VIX")

# Alpaca Config (news producer)
ALPACA_API_KEY = get_env("ALPACA_API_KEY", "")
ALPACA_API_SECRET = get_env("ALPACA_API_SECRET", "")
ALPACA_URL = get_env("ALPACA_URL", "https://data.alpaca.markets/v1beta1/news")
