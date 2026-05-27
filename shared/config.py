import os


def get_env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def get_env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


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
