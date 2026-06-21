-- Pull all option chain rows for a given asset and date range.
-- Replace the parameter placeholders before running interactively:
--   :asset  -> e.g. 'BTC'
--   :start  -> e.g. '2025-01-01 00:00:00'
--   :end    -> e.g. '2025-03-31 23:59:59'

SELECT
    epoch(ts)     AS timestamp,
    instrument,
    asset,
    spot,
    mark_price,
    mark_iv,
    bid,
    ask,
    open_interest
FROM  option_chain
WHERE asset = :asset
  AND ts BETWEEN TIMESTAMP :start AND TIMESTAMP :end
ORDER BY ts, instrument;
