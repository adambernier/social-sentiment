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


CREATE TABLE IF NOT EXISTS tracked_symbols (
    symbol            TEXT PRIMARY KEY,
    keywords          JSONB NOT NULL DEFAULT '[]'::jsonb,
    future            TEXT,
    sector            TEXT,
    require_uppercase BOOLEAN NOT NULL DEFAULT FALSE,
    block_phrases     JSONB NOT NULL DEFAULT '[]'::jsonb,
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS stock_quotes (
    id          SERIAL PRIMARY KEY,
    symbol      TEXT NOT NULL,
    timestamp   TIMESTAMPTZ NOT NULL,
    price       FLOAT NOT NULL,
    volume      BIGINT NOT NULL,
    market_session TEXT NOT NULL DEFAULT 'closed',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (symbol, timestamp)
);

CREATE INDEX IF NOT EXISTS stock_quotes_symbol_idx ON stock_quotes (symbol);
CREATE INDEX IF NOT EXISTS stock_quotes_timestamp_idx ON stock_quotes (timestamp);
CREATE INDEX IF NOT EXISTS stock_quotes_symbol_timestamp_idx ON stock_quotes (symbol, timestamp DESC);
CREATE INDEX IF NOT EXISTS stock_quotes_symbol_session_timestamp_idx ON stock_quotes (symbol, market_session, timestamp DESC);

-- Clean up duplicate quotes if they exist before adding the constraint to an existing table
DELETE FROM stock_quotes a
USING stock_quotes b
WHERE a.id > b.id
  AND a.symbol = b.symbol
  AND a.timestamp = b.timestamp;

-- Add unique constraint if it doesn't already exist for older installations
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'stock_quotes_symbol_timestamp_key'
           OR conname = 'unique_symbol_timestamp'
    ) THEN
        ALTER TABLE stock_quotes ADD CONSTRAINT unique_symbol_timestamp UNIQUE (symbol, timestamp);
    END IF;
END $$;

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
            'text', left(NEW.text, 1000),
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


-- Hourly aggregation table for cold-tier data retention.
-- Raw posts older than the retention window are rolled up here before pruning.
CREATE TABLE IF NOT EXISTS hourly_sentiment_agg (
    symbol              TEXT NOT NULL,
    bucket_hour         TIMESTAMPTZ NOT NULL,
    positive_count      INTEGER NOT NULL DEFAULT 0,
    neutral_count       INTEGER NOT NULL DEFAULT 0,
    negative_count      INTEGER NOT NULL DEFAULT 0,
    positive_weighted   FLOAT NOT NULL DEFAULT 0,
    negative_weighted   FLOAT NOT NULL DEFAULT 0,
    neutral_weighted    FLOAT NOT NULL DEFAULT 0,
    total_weighted      FLOAT NOT NULL DEFAULT 0,
    sentiment_index     FLOAT NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, bucket_hour)
);
