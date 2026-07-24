-- Provider-neutral global market context. Instruments use stable internal keys;
-- provider symbols are replaceable aliases rather than database identities.

CREATE TABLE global_instruments (
    instrument_key     TEXT PRIMARY KEY,
    display_name       TEXT NOT NULL,
    asset_class        TEXT NOT NULL CHECK (
        asset_class IN ('index', 'fx', 'commodity', 'us_equity')
    ),
    currency           TEXT NOT NULL,
    exchange           TEXT,
    timezone           TEXT NOT NULL,
    provider_aliases   JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (
        jsonb_typeof(provider_aliases) = 'object'
    ),
    session_metadata   JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (
        jsonb_typeof(session_metadata) = 'object'
    ),
    quote_convention   TEXT,
    is_active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX global_instruments_active_class_idx
    ON global_instruments (asset_class, instrument_key)
    WHERE is_active;

CREATE TABLE global_market_bars (
    instrument_key     TEXT NOT NULL
        REFERENCES global_instruments(instrument_key) ON DELETE CASCADE,
    interval           TEXT NOT NULL CHECK (interval IN ('1h', '1d')),
    starts_at          TIMESTAMPTZ NOT NULL,
    ends_at            TIMESTAMPTZ NOT NULL,
    session_date       DATE NOT NULL,
    open_price         DOUBLE PRECISION NOT NULL,
    high_price         DOUBLE PRECISION NOT NULL,
    low_price          DOUBLE PRECISION NOT NULL,
    close_price        DOUBLE PRECISION NOT NULL,
    volume             DOUBLE PRECISION,
    provider           TEXT NOT NULL,
    fetched_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (instrument_key, interval, starts_at),
    CHECK (ends_at > starts_at),
    CHECK (
        open_price > 0 AND high_price > 0
        AND low_price > 0 AND close_price > 0
    ),
    CHECK (low_price <= high_price),
    CHECK (low_price <= LEAST(open_price, close_price)),
    CHECK (high_price >= GREATEST(open_price, close_price)),
    CHECK (volume IS NULL OR volume >= 0)
);

CREATE INDEX global_market_bars_instrument_interval_end_idx
    ON global_market_bars (instrument_key, interval, ends_at DESC);
CREATE INDEX global_market_bars_retention_idx
    ON global_market_bars (interval, ends_at);

CREATE TABLE stock_factor_exposures (
    symbol             TEXT NOT NULL
        REFERENCES tracked_symbols(symbol) ON DELETE CASCADE,
    instrument_key     TEXT NOT NULL
        REFERENCES global_instruments(instrument_key) ON DELETE RESTRICT,
    reason             TEXT NOT NULL,
    display_order      INTEGER NOT NULL DEFAULT 0,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, instrument_key),
    CHECK (display_order >= 0)
);

CREATE INDEX stock_factor_exposures_instrument_idx
    ON stock_factor_exposures (instrument_key, symbol);

CREATE TABLE global_event_rules (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol             TEXT NOT NULL
        REFERENCES tracked_symbols(symbol) ON DELETE CASCADE,
    name               TEXT NOT NULL,
    countries          JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (
        jsonb_typeof(countries) = 'array'
    ),
    themes             JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (
        jsonb_typeof(themes) = 'array'
    ),
    query_terms        JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (
        jsonb_typeof(query_terms) = 'array'
    ),
    is_active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (symbol, name),
    CHECK (
        jsonb_array_length(countries)
        + jsonb_array_length(themes)
        + jsonb_array_length(query_terms) > 0
    )
);

CREATE INDEX global_event_rules_active_symbol_idx
    ON global_event_rules (symbol, id)
    WHERE is_active;

CREATE TABLE global_event_signals (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider           TEXT NOT NULL,
    provider_event_id  TEXT,
    canonical_url      TEXT,
    title              TEXT NOT NULL,
    summary            TEXT,
    source_name        TEXT,
    occurred_at        TIMESTAMPTZ NOT NULL,
    countries          JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (
        jsonb_typeof(countries) = 'array'
    ),
    themes             JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (
        jsonb_typeof(themes) = 'array'
    ),
    ingested_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        provider_event_id IS NOT NULL
        OR NULLIF(canonical_url, '') IS NOT NULL
    )
);

CREATE UNIQUE INDEX global_event_signals_provider_id_uidx
    ON global_event_signals (provider, provider_event_id)
    WHERE provider_event_id IS NOT NULL;
CREATE UNIQUE INDEX global_event_signals_provider_url_uidx
    ON global_event_signals (provider, canonical_url)
    WHERE NULLIF(canonical_url, '') IS NOT NULL;
CREATE INDEX global_event_signals_occurred_at_idx
    ON global_event_signals (occurred_at DESC);

CREATE TABLE stock_event_links (
    event_id            BIGINT NOT NULL
        REFERENCES global_event_signals(id) ON DELETE CASCADE,
    symbol              TEXT NOT NULL
        REFERENCES tracked_symbols(symbol) ON DELETE CASCADE,
    rule_id             BIGINT NOT NULL
        REFERENCES global_event_rules(id) ON DELETE CASCADE,
    match_reason        JSONB NOT NULL CHECK (
        jsonb_typeof(match_reason) = 'object'
    ),
    linked_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (event_id, symbol, rule_id)
);

CREATE INDEX stock_event_links_symbol_linked_idx
    ON stock_event_links (symbol, linked_at DESC);
CREATE INDEX stock_event_links_rule_idx
    ON stock_event_links (rule_id, linked_at DESC);

INSERT INTO global_instruments (
    instrument_key, display_name, asset_class, currency, exchange, timezone,
    provider_aliases, session_metadata, quote_convention
)
VALUES
    (
        'index:nikkei-225', 'Nikkei 225', 'index', 'JPY', 'JPX',
        'Asia/Tokyo', '{"yahoo":"^N225"}',
        '{"open":"09:00","close":"15:30","weekdays":[1,2,3,4,5]}', NULL
    ),
    (
        'index:hang-seng', 'Hang Seng', 'index', 'HKD', 'HKEX',
        'Asia/Hong_Kong', '{"yahoo":"^HSI"}',
        '{"open":"09:30","close":"16:00","weekdays":[1,2,3,4,5]}', NULL
    ),
    (
        'index:csi-300', 'CSI 300', 'index', 'CNY', 'SSE/SZSE',
        'Asia/Shanghai', '{"yahoo":"000300.SS"}',
        '{"open":"09:30","close":"15:00","weekdays":[1,2,3,4,5]}', NULL
    ),
    (
        'index:taiwan-weighted', 'Taiwan Weighted', 'index', 'TWD', 'TWSE',
        'Asia/Taipei', '{"yahoo":"^TWII"}',
        '{"open":"09:00","close":"13:30","weekdays":[1,2,3,4,5]}', NULL
    ),
    (
        'index:kospi', 'KOSPI', 'index', 'KRW', 'KRX',
        'Asia/Seoul', '{"yahoo":"^KS11"}',
        '{"open":"09:00","close":"15:30","weekdays":[1,2,3,4,5]}', NULL
    ),
    (
        'index:nifty-50', 'Nifty 50', 'index', 'INR', 'NSE',
        'Asia/Kolkata', '{"yahoo":"^NSEI"}',
        '{"open":"09:15","close":"15:30","weekdays":[1,2,3,4,5]}', NULL
    ),
    (
        'fx:usd-jpy', 'USD/JPY', 'fx', 'JPY', NULL,
        'America/New_York', '{"yahoo":"JPY=X"}',
        '{"open":"00:00","close":"17:00","weekdays":[1,2,3,4,5]}',
        'local_currency_per_usd'
    ),
    (
        'fx:usd-cnh', 'USD/CNH', 'fx', 'CNH', NULL,
        'America/New_York', '{"yahoo":"CNH=X"}',
        '{"open":"00:00","close":"17:00","weekdays":[1,2,3,4,5]}',
        'local_currency_per_usd'
    ),
    (
        'fx:usd-krw', 'USD/KRW', 'fx', 'KRW', NULL,
        'America/New_York', '{"yahoo":"KRW=X"}',
        '{"open":"00:00","close":"17:00","weekdays":[1,2,3,4,5]}',
        'local_currency_per_usd'
    ),
    (
        'fx:usd-twd', 'USD/TWD', 'fx', 'TWD', NULL,
        'America/New_York', '{"yahoo":"TWD=X"}',
        '{"open":"00:00","close":"17:00","weekdays":[1,2,3,4,5]}',
        'local_currency_per_usd'
    ),
    (
        'fx:usd-inr', 'USD/INR', 'fx', 'INR', NULL,
        'America/New_York', '{"yahoo":"INR=X"}',
        '{"open":"00:00","close":"17:00","weekdays":[1,2,3,4,5]}',
        'local_currency_per_usd'
    ),
    (
        'commodity:gold', 'Gold', 'commodity', 'USD', 'COMEX',
        'America/New_York', '{"yahoo":"GC=F"}',
        '{"open":"18:00","close":"17:00","weekdays":[1,2,3,4,5]}', NULL
    ),
    (
        'commodity:brent-crude', 'Brent crude', 'commodity', 'USD', 'ICE',
        'Europe/London', '{"yahoo":"BZ=F"}',
        '{"open":"00:00","close":"22:00","weekdays":[1,2,3,4,5]}', NULL
    ),
    (
        'commodity:copper', 'Copper', 'commodity', 'USD', 'COMEX',
        'America/New_York', '{"yahoo":"HG=F"}',
        '{"open":"18:00","close":"17:00","weekdays":[1,2,3,4,5]}', NULL
    )
ON CONFLICT (instrument_key) DO NOTHING;
