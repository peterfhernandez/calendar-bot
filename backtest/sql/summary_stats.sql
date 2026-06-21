-- High-level coverage summary: row counts, date range, assets present.

SELECT
    asset,
    COUNT(*)                                       AS total_rows,
    COUNT(DISTINCT ts)                             AS total_snapshots,
    MIN(ts)                                        AS earliest_ts,
    MAX(ts)                                        AS latest_ts,
    ROUND(
        epoch(MAX(ts)) - epoch(MIN(ts))
    ) / 86400.0                                    AS span_days,
    COUNT(DISTINCT instrument)                     AS distinct_instruments
FROM  option_chain
GROUP BY asset
ORDER BY asset;
