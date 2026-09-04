-- DuckDB schema for the Alpaca options market-data store.
--
-- Everything here is regenerable with `make ingest`; the database file itself is
-- gitignored. data/export/*.parquet is what ships, so a reader can reproduce a
-- backtest without re-downloading ~2.5 years of option bars.
--
-- Alpaca's option history begins February 2024, so no table can be populated
-- earlier than that regardless of the requested start date.

-- Daily/intraday bars for the underlying (SPY, QQQ).
CREATE TABLE IF NOT EXISTS underlying_bars (
    symbol      VARCHAR   NOT NULL,
    ts          TIMESTAMP NOT NULL,
    timeframe   VARCHAR   NOT NULL,   -- '1Day', '1Hour', ...
    open        DOUBLE,
    high        DOUBLE,
    low         DOUBLE,
    close       DOUBLE,
    volume      BIGINT,
    trade_count BIGINT,
    vwap        DOUBLE,
    PRIMARY KEY (symbol, timeframe, ts)
);

-- Bars for individual option contracts. `underlying`, `expiry`, `strike` and
-- `right` are denormalised out of the OCC symbol so the backtest can filter on
-- them without parsing strings in the hot path.
CREATE TABLE IF NOT EXISTS option_bars (
    occ         VARCHAR   NOT NULL,
    underlying  VARCHAR   NOT NULL,
    expiry      DATE      NOT NULL,
    strike      DOUBLE    NOT NULL,
    opt_right   VARCHAR   NOT NULL,   -- 'C' | 'P'
    ts          TIMESTAMP NOT NULL,
    timeframe   VARCHAR   NOT NULL,
    open        DOUBLE,
    high        DOUBLE,
    low         DOUBLE,
    close       DOUBLE,
    volume      BIGINT,
    trade_count BIGINT,
    vwap        DOUBLE,
    PRIMARY KEY (occ, timeframe, ts)
);

-- Tick trades for option contracts. Optional -- only ingested for windows where
-- the backtest needs intraday fill realism, because the volume is large.
CREATE TABLE IF NOT EXISTS option_trades (
    occ        VARCHAR   NOT NULL,
    underlying VARCHAR   NOT NULL,
    expiry     DATE      NOT NULL,
    ts         TIMESTAMP NOT NULL,
    price      DOUBLE,
    size       BIGINT,
    exchange   VARCHAR,
    condition  VARCHAR
);

-- Benzinga news, free on the Basic plan. Feeds the live agent's context and any
-- news-conditioned backtest variant.
CREATE TABLE IF NOT EXISTS news (
    id         BIGINT  PRIMARY KEY,
    ts         TIMESTAMP NOT NULL,
    updated_at TIMESTAMP,
    headline   VARCHAR,
    summary    VARCHAR,
    author     VARCHAR,
    source     VARCHAR,
    url        VARCHAR,
    symbols    VARCHAR    -- comma-separated; DuckDB list types complicate Parquet round-trips
);

-- What has already been fetched, so re-running ingest is cheap and idempotent.
-- One row per (endpoint, symbol-ish key, timeframe, window).
CREATE TABLE IF NOT EXISTS ingest_log (
    endpoint    VARCHAR   NOT NULL,   -- 'underlying_bars' | 'option_bars' | 'option_trades' | 'news'
    key         VARCHAR   NOT NULL,   -- symbol, or underlying|expiry for an option chain slice
    timeframe   VARCHAR,
    start_date  DATE,
    end_date    DATE,
    rows_loaded BIGINT,
    fetched_at  TIMESTAMP NOT NULL,
    source      VARCHAR,              -- 'alpaca-cli 0.0.13'
    PRIMARY KEY (endpoint, key, timeframe, start_date, end_date)
);

-- Trading calendar, so the backtest never invents a session.
CREATE TABLE IF NOT EXISTS calendar (
    date        DATE PRIMARY KEY,
    open_time   VARCHAR,
    close_time  VARCHAR,
    settlement  DATE
);

CREATE INDEX IF NOT EXISTS idx_option_bars_lookup
    ON option_bars (underlying, expiry, ts);
CREATE INDEX IF NOT EXISTS idx_option_bars_strike
    ON option_bars (underlying, expiry, opt_right, strike);
CREATE INDEX IF NOT EXISTS idx_underlying_bars_ts
    ON underlying_bars (symbol, ts);
CREATE INDEX IF NOT EXISTS idx_news_ts
    ON news (ts);
