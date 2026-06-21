-- Historical option chain snapshot schema for DuckDB.
-- Run once to initialise backtest/historic_data/options.duckdb.

CREATE SEQUENCE IF NOT EXISTS option_chain_id_seq START 1;

CREATE TABLE IF NOT EXISTS option_chain (
    id            BIGINT    DEFAULT nextval('option_chain_id_seq') PRIMARY KEY,
    ts            TIMESTAMP NOT NULL,
    instrument    VARCHAR   NOT NULL,
    asset         VARCHAR(3) NOT NULL,
    spot          DOUBLE    NOT NULL,
    mark_price    DOUBLE    NOT NULL,
    mark_iv       DOUBLE    NOT NULL,
    bid           DOUBLE    NOT NULL,
    ask           DOUBLE    NOT NULL,
    open_interest DOUBLE    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_option_chain_ts       ON option_chain (ts);
CREATE INDEX IF NOT EXISTS idx_option_chain_asset_ts ON option_chain (asset, ts);
CREATE INDEX IF NOT EXISTS idx_option_chain_instr_ts ON option_chain (instrument, ts);

CREATE TABLE IF NOT EXISTS collection_runs (
    id         INTEGER PRIMARY KEY,
    started_at TIMESTAMP NOT NULL,
    ended_at   TIMESTAMP,
    asset      VARCHAR(3),
    rows_added INTEGER,
    status     VARCHAR(16)   -- 'ok', 'error', 'partial'
);

CREATE SEQUENCE IF NOT EXISTS collection_runs_id_seq START 1;
