-- Show each instrument and the date span for which data exists.

SELECT
    asset,
    instrument,
    MIN(ts)             AS first_seen,
    MAX(ts)             AS last_seen,
    COUNT(DISTINCT ts)  AS snapshot_count
FROM  option_chain
GROUP BY asset, instrument
ORDER BY asset, instrument;
