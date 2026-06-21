-- Identify consecutive timestamp gaps that exceed a threshold.
-- Edit :min_gap_minutes and :asset as needed.
-- Gaps are computed per asset so a gap in BTC data doesn't mask ETH continuity.

WITH ranked AS (
    SELECT
        asset,
        ts,
        LAG(ts) OVER (PARTITION BY asset ORDER BY ts) AS prev_ts
    FROM (
        SELECT DISTINCT asset, ts FROM option_chain
    ) t
)
SELECT
    asset,
    prev_ts                                              AS gap_start,
    ts                                                   AS gap_end,
    ROUND((epoch(ts) - epoch(prev_ts)) / 60.0, 1)       AS gap_minutes
FROM ranked
WHERE prev_ts IS NOT NULL
  AND (epoch(ts) - epoch(prev_ts)) / 60.0 > 30   -- change 30 to :min_gap_minutes
ORDER BY gap_minutes DESC;
