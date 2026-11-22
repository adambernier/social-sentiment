-- Remove redundant single-column prefix indexes that are already covered by
-- composite (symbol, timestamp DESC) indexes, and add a composite platform
-- time-range index for source health queries.

DROP INDEX IF EXISTS posts_symbol_idx;
DROP INDEX IF EXISTS stock_quotes_symbol_idx;

CREATE INDEX IF NOT EXISTS posts_platform_timestamp_idx
    ON posts (platform, timestamp DESC);
