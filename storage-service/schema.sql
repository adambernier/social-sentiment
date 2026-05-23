CREATE TABLE IF NOT EXISTS posts (
    id          TEXT PRIMARY KEY,
    symbol      TEXT NOT NULL DEFAULT 'UNKNOWN',
    platform    TEXT NOT NULL,
    text        TEXT NOT NULL,
    timestamp   TIMESTAMPTZ NOT NULL,
    sentiment   TEXT NOT NULL,
    scores      JSONB NOT NULL,
    topic_id    INTEGER,
    topic_label TEXT,
    scored_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    engagement  INTEGER NOT NULL DEFAULT 1
);

-- Ensure engagement column exists if database table already created
ALTER TABLE posts ADD COLUMN IF NOT EXISTS engagement INTEGER NOT NULL DEFAULT 1;

CREATE INDEX IF NOT EXISTS posts_symbol_idx ON posts (symbol);
CREATE INDEX IF NOT EXISTS posts_sentiment_idx ON posts (sentiment);
CREATE INDEX IF NOT EXISTS posts_platform_idx ON posts (platform);
CREATE INDEX IF NOT EXISTS posts_symbol_timestamp_idx ON posts (symbol, timestamp DESC);


CREATE TABLE IF NOT EXISTS stock_quotes (
    id          SERIAL PRIMARY KEY,
    symbol      TEXT NOT NULL,
    timestamp   TIMESTAMPTZ NOT NULL,
    price       FLOAT NOT NULL,
    volume      BIGINT NOT NULL,
    market_session TEXT NOT NULL DEFAULT 'closed',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS stock_quotes_symbol_idx ON stock_quotes (symbol);
CREATE INDEX IF NOT EXISTS stock_quotes_timestamp_idx ON stock_quotes (timestamp);

CREATE TABLE IF NOT EXISTS stock_metrics (
    symbol                  TEXT PRIMARY KEY,
    pe_ratio                FLOAT,
    beta                    FLOAT,
    avg_return_1y           FLOAT,
    inflation_adj_return_1y FLOAT,
    pe_relative_sector      FLOAT,
    beta_relative_sector    FLOAT,
    return_relative_sector  FLOAT,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Trigger to notify on new post inserts
CREATE OR REPLACE FUNCTION notify_new_post()
RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify(
        'new_posts',
        json_build_object(
            'id', NEW.id,
            'symbol', NEW.symbol,
            'platform', NEW.platform,
            'text', NEW.text,
            'timestamp', NEW.timestamp,
            'sentiment', NEW.sentiment,
            'scores', NEW.scores,
            'topic_id', NEW.topic_id,
            'topic_label', NEW.topic_label,
            'scored_at', NEW.scored_at,
            'engagement', NEW.engagement
        )::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trigger_notify_new_post
AFTER INSERT ON posts
FOR EACH ROW
EXECUTE FUNCTION notify_new_post();
