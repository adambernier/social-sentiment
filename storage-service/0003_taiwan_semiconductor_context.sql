-- Add the targeted Taiwan semiconductor factor without changing the stable
-- identity or history of the broader Taiwan Weighted index. Daily history is
-- sourced from Taiwan Index Plus because Yahoo currently exposes only the
-- latest row for IX0143.TW.

INSERT INTO global_instruments (
    instrument_key, display_name, asset_class, currency, exchange, timezone,
    provider_aliases, session_metadata, quote_convention
)
VALUES (
    'index:taiwan-semiconductor',
    'Taiwan Semiconductor',
    'index',
    'TWD',
    'TWSE/TPEx',
    'Asia/Taipei',
    '{"taiwan_index":"IX0143","yahoo":"IX0143.TW"}',
    '{"open":"09:00","close":"13:30","weekdays":[1,2,3,4,5]}',
    NULL
)
ON CONFLICT (instrument_key) DO NOTHING;

WITH replaced_exposure AS (
    DELETE FROM stock_factor_exposures
    WHERE symbol = 'NVDA'
      AND instrument_key = 'index:taiwan-weighted'
    RETURNING symbol, display_order
)
INSERT INTO stock_factor_exposures (
    symbol, instrument_key, reason, display_order
)
SELECT
    symbol,
    'index:taiwan-semiconductor',
    'Taiwan semiconductor manufacturing and supply-chain context',
    display_order
FROM replaced_exposure
ON CONFLICT (symbol, instrument_key) DO UPDATE SET
    reason = EXCLUDED.reason,
    display_order = EXCLUDED.display_order,
    updated_at = NOW();
