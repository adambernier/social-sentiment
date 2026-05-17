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
    scored_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS posts_symbol_idx ON posts (symbol);
CREATE INDEX IF NOT EXISTS posts_sentiment_idx ON posts (sentiment);
CREATE INDEX IF NOT EXISTS posts_platform_idx ON posts (platform);

CREATE TABLE IF NOT EXISTS stock_quotes (
    id          SERIAL PRIMARY KEY,
    symbol      TEXT NOT NULL,
    timestamp   TIMESTAMPTZ NOT NULL,
    price       FLOAT NOT NULL,
    volume      BIGINT NOT NULL,
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
