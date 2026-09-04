"""DuckDB access + point-in-time tables added on top of alpaca_data.duckdb.

Everything written by this package carries `captured_at` (UTC) so that any later
backtest can do backward-only as-of joins (framework invariant 3.1).
"""
from __future__ import annotations
import duckdb, yaml, os, datetime as dt
from pathlib import Path

PIT_DDL = """
CREATE TABLE IF NOT EXISTS pit_stock_bars_daily (
    symbol VARCHAR, t DATE, o DOUBLE, h DOUBLE, l DOUBLE, c DOUBLE, v BIGINT, vw DOUBLE, n BIGINT,
    captured_at TIMESTAMP, PRIMARY KEY (symbol, t));
CREATE TABLE IF NOT EXISTS pit_chain_snapshots (
    captured_at TIMESTAMP, underlying VARCHAR, option_symbol VARCHAR, expiration DATE, cp VARCHAR, strike DOUBLE,
    bid DOUBLE, ask DOUBLE, bid_size BIGINT, ask_size BIGINT, quote_t TIMESTAMP,
    last DOUBLE, last_t TIMESTAMP, iv DOUBLE, delta DOUBLE, gamma DOUBLE, theta DOUBLE, vega DOUBLE,
    open_interest BIGINT, spot DOUBLE, feed VARCHAR);
CREATE TABLE IF NOT EXISTS pit_vix_daily (
    t DATE PRIMARY KEY, vix DOUBLE, vix9d DOUBLE, vix3m DOUBLE, vix6m DOUBLE, captured_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS pit_events (
    event_date DATE, event VARCHAR, vintage_at TIMESTAMP, source VARCHAR);
CREATE TABLE IF NOT EXISTS pit_dividends_forward (
    symbol VARCHAR, ex_date DATE, amount DOUBLE, vintage_at TIMESTAMP, source VARCHAR);
CREATE TABLE IF NOT EXISTS preselect_runs (
    run_at TIMESTAMP, as_of DATE, config_version VARCHAR, layer VARCHAR, symbol VARCHAR,
    passed BOOLEAN, reason VARCHAR, metrics JSON);
CREATE TABLE IF NOT EXISTS decision_ledger (
    decision_id VARCHAR, decided_at TIMESTAMP, as_of DATE, config_version VARCHAR, regime VARCHAR,
    underlying VARCHAR, structure VARCHAR, legs JSON, credit_exec DOUBLE, credit_mid DOUBLE, width DOUBLE,
    max_loss DOUBLE, margin DOUBLE, ev_exec DOUBLE, ev_pess DOUBLE, edge_on_margin DOUBLE, edge_cost_ratio DOUBLE,
    rt_cost DOUBLE, iv_atm DOUBLE, rv_f DOUBLE, k DOUBLE, qty INTEGER, decision VARCHAR, reason VARCHAR,
    predicted_dist JSON);
CREATE TABLE IF NOT EXISTS positions (
    position_id VARCHAR PRIMARY KEY, decision_id VARCHAR, opened_at TIMESTAMP, underlying VARCHAR, structure VARCHAR,
    legs JSON, qty INTEGER, credit_fill DOUBLE, width DOUBLE, expiration DATE, rv_f_entry DOUBLE, iv_entry DOUBLE,
    status VARCHAR, closed_at TIMESTAMP, close_fill DOUBLE, exit_reason VARCHAR, pnl DOUBLE);
CREATE TABLE IF NOT EXISTS kill_ledger (
    ts TIMESTAMP, hypothesis VARCHAR, reason VARCHAR, cost DOUBLE, lesson VARCHAR);
"""


def load_config(path: str | os.PathLike = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def connect(cfg: dict, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(cfg["db_path"]), read_only=read_only)
    if not read_only:
        con.execute(PIT_DDL)
    return con


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def trading_days(con, start: dt.date, end: dt.date) -> list[dt.date]:
    rows = con.execute("SELECT date FROM calendar WHERE date BETWEEN ? AND ? ORDER BY date", [start, end]).fetchall()
    return [r[0] for r in rows]


def sessions_between(con, a: dt.date, b: dt.date) -> int:
    """Number of trading sessions strictly after a, up to and including b."""
    return con.execute("SELECT count(*) FROM calendar WHERE date > ? AND date <= ?", [a, b]).fetchone()[0]


def daily_bars(con, symbol: str, min_rows: int = 0):
    """Bars from pit_stock_bars_daily, falling back to the original stock_bars_daily sample table."""
    rows = con.execute(
        "SELECT t, o, h, l, c, v FROM pit_stock_bars_daily WHERE symbol=? ORDER BY t", [symbol]).fetchall()
    if len(rows) < max(min_rows, 1):
        rows = con.execute(
            "SELECT CAST(t AS DATE), o, h, l, c, v FROM stock_bars_daily WHERE key=? ORDER BY t", [symbol]).fetchall()
    return rows
