"""
Read-side interface over the DuckDB market-data store.

The engine touches data only through this class, which keeps the simulation loop
free of SQL and makes it straightforward to swap in a fixture store for tests.

Everything is loaded into memory once per run and served from dicts. The whole
SPY option-bar table for 2.5 years is on the order of a few hundred thousand rows,
which fits comfortably; doing per-day SQL inside the day loop was measurably slower
and gained nothing.

Note that DuckDB takes an exclusive lock on the database file, so a backtest cannot
run while `data/ingest.py` is writing. Open read-only where possible.
"""
import bisect
import datetime as dt
import math
import os
import statistics

import duckdb

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
DEFAULT_DB = os.path.join(REPO_ROOT, 'data', 'market.duckdb')


def _as_date(value):
    if isinstance(value, dt.datetime):
        return value.date()
    return value


class Store:
    def __init__(self, db_path=DEFAULT_DB, underlying='SPY', underlyings=None, read_only=True):
        if not os.path.exists(db_path):
            raise FileNotFoundError(
                f"{db_path} does not exist. Run `python3 data/ingest.py` first.")
        self.db_path = db_path
        if underlyings is None:
            underlyings = [underlying]
        elif isinstance(underlyings, str):
            underlyings = [s.strip().upper() for s in underlyings.split(',') if s.strip()]
        self.underlyings = [u.upper() for u in underlyings]
        self.underlying = self.underlyings[0]
        self.con = duckdb.connect(db_path, read_only=read_only)

        self._underlying = {}   # (symbol, date) -> close
        self._ohlc = {}         # (symbol, date) -> (o, h, l, c, v)
        self._opt_close = {}    # (occ, date)    -> close
        self._chain = {}        # (underlying, expiry, date) -> [row, ...]
        self._expiries_by = {}  # underlying -> sorted unique expiries
        self._expiries = []     # union, for coverage
        self._sessions = []     # sorted trading days
        self._rv_cache = {}
        self._ohlc_cache = {}
        # Deterministic functions of the data, shared across every configuration run
        # against this store in one process: the Black-Scholes inversion of a chain and
        # the HAR/OHLCV feature block. A parameter grid re-derives both thousands of
        # times otherwise, and neither depends on any parameter being varied.
        self._greeks_cache = {}
        self._feature_cache = {}
        self._load()

    # -------------------------------------------------------------------- load

    def _load(self):
        for row in self.con.execute(
            "SELECT symbol, ts, open, high, low, close, volume "
            "FROM underlying_bars WHERE timeframe='1Day'"
        ).fetchall():
            sym, ts, o, h, l, close, vol = row
            d = _as_date(ts)
            self._underlying[(sym, d)] = close
            self._ohlc[(sym, d)] = (o, h, l, close, vol or 0)

        if self.underlyings == ['*']:
            rows = self.con.execute(
                "SELECT occ, underlying, expiry, strike, opt_right, ts, close, volume "
                "FROM option_bars WHERE timeframe='1Day' "
                "ORDER BY underlying, expiry, ts, strike",
            ).fetchall()
        else:
            placeholders = ','.join('?' * len(self.underlyings))
            rows = self.con.execute(
                "SELECT occ, underlying, expiry, strike, opt_right, ts, close, volume "
                f"FROM option_bars WHERE underlying IN ({placeholders}) AND timeframe='1Day' "
                "ORDER BY underlying, expiry, ts, strike",
                self.underlyings,
            ).fetchall()

        expiries = set()
        by = {}
        for occ, und, expiry, strike, right, ts, close, volume in rows:
            d = _as_date(ts)
            e = _as_date(expiry)
            self._opt_close[(occ, d)] = close
            self._chain.setdefault((und, e, d), []).append({
                'occ': occ, 'strike': strike, 'opt_right': right,
                'expiry': e, 'close': close, 'volume': volume,
            })
            expiries.add(e)
            by.setdefault(und, set()).add(e)
        self._expiries = sorted(expiries)
        self._expiries_by = {u: sorted(es) for u, es in by.items()}
        if self.underlyings == ['*']:
            self.underlyings = sorted(by) or self.underlyings
            if self.underlyings:
                self.underlying = self.underlyings[0]

        self._sessions = [
            _as_date(r[0]) for r in
            self.con.execute("SELECT date FROM calendar ORDER BY date").fetchall()
        ]

    # ------------------------------------------------------------------ access

    def trading_days(self, start, end):
        """
        Real NYSE sessions in range, from the ingested calendar.

        Bisected rather than scanned. The engine asks this once per CONTRACT to put the
        Black-Scholes inversion on trading time -- 630k calls in a six-name run -- and a
        linear scan over ~650 sessions made it the single most expensive line in the
        profile (22s of a 130s run) for an answer that is a slice of a sorted list.
        """
        lo = bisect.bisect_left(self._sessions, start)
        hi = bisect.bisect_right(self._sessions, end)
        return self._sessions[lo:hi]

    def underlying_close(self, symbol, date):
        return self._underlying.get((symbol, date))

    def option_close(self, occ, date):
        return self._opt_close.get((occ, date))

    def expiries_after(self, underlying, date):
        return [e for e in self._expiries_by.get(underlying, self._expiries) if e >= date]

    def chain_for(self, underlying, expiry, date):
        """Every contract for one expiry that printed on `date`."""
        return self._chain.get((underlying, expiry, date), [])

    def chains_in_window(self, underlying, date, min_dte, max_dte):
        """All contracts whose expiry DTE is in [min_dte, max_dte] on `date`."""
        rows = []
        for exp in self.expiries_after(underlying, date):
            dte = (exp - date).days
            if min_dte <= dte <= max_dte:
                for r in self.chain_for(underlying, exp, date):
                    row = dict(r)
                    row['dte'] = dte
                    rows.append(row)
        return rows

    def realized_vol(self, symbol, date, window=21):
        """
        Annualized close-to-close realized volatility over the `window` sessions
        STRICTLY BEFORE `date`.

        Strictly-before matters: including `date` itself would leak the very move the
        strategy is about to trade against, which is the classic way a vol filter
        backtests well and fails live.
        """
        key = (symbol, date, window)
        if key in self._rv_cache:
            return self._rv_cache[key]

        prior = [d for d in self._sessions if d < date][-window:]
        closes = [self._underlying.get((symbol, d)) for d in prior]
        closes = [c for c in closes if c]
        if len(closes) < 3:
            self._rv_cache[key] = None
            return None
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
        if len(rets) < 2:
            self._rv_cache[key] = None
            return None
        vol = statistics.stdev(rets) * math.sqrt(252)
        self._rv_cache[key] = vol
        return vol

    def closes_before(self, symbol, date, n=21):
        """Underlying closes for the n sessions strictly before `date`."""
        prior = [d for d in self._sessions if d < date][-n:]
        return [c for c in (self._underlying.get((symbol, d)) for d in prior) if c]

    def ohlc_before(self, symbol, date, n=400):
        """OHLCV arrays for the n sessions strictly before `date`. None if too thin."""
        cached = self._ohlc_cache.get((symbol, date, n))
        if cached is not None:
            return cached if cached != 'MISS' else None
        prior = self._sessions[:bisect.bisect_left(self._sessions, date)][-n:]
        rows = [self._ohlc.get((symbol, d)) for d in prior]
        rows = [r for r in rows if r and r[3]]
        if len(rows) < 90:
            self._ohlc_cache[(symbol, date, n)] = 'MISS'
            return None
        o, h, l, c, v = zip(*[(r[0] or r[3], r[1] or r[3], r[2] or r[3], r[3], r[4] or 0)
                              for r in rows])
        out = (list(o), list(h), list(l), list(c), list(v))
        self._ohlc_cache[(symbol, date, n)] = out
        return out

    # ------------------------------------------------------------------- meta

    def coverage(self):
        """Row counts and date ranges, for the run's data fingerprint."""
        out = {}
        for table, tscol in (('underlying_bars', 'ts'), ('option_bars', 'ts'),
                             ('news', 'ts'), ('calendar', 'date')):
            n = self.con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            lo = hi = None
            if n:
                lo, hi = self.con.execute(
                    f"SELECT min({tscol}), max({tscol}) FROM {table}").fetchone()
            out[table] = {'rows': n, 'first': str(lo)[:10] if lo else None,
                          'last': str(hi)[:10] if hi else None}
        if self.underlyings == ['*']:
            out['option_contracts'] = self.con.execute(
                "SELECT count(DISTINCT occ) FROM option_bars").fetchone()[0]
        else:
            placeholders = ','.join('?' * len(self.underlyings))
            out['option_contracts'] = self.con.execute(
                f"SELECT count(DISTINCT occ) FROM option_bars WHERE underlying IN ({placeholders})",
                self.underlyings).fetchone()[0]
        out['option_expiries'] = len(self._expiries)
        out['option_underlyings'] = list(self.underlyings)
        return out

    def fingerprint(self, start, end):
        """
        Cheap equivalence check, per the vendored skill's data_fingerprint.json.

        close_sum over the window is enough to detect a changed dataset without
        hashing megabytes of bars.
        """
        if self.underlyings == ['*']:
            row = self.con.execute(
                "SELECT count(*), sum(close), min(ts), max(ts) FROM option_bars "
                "WHERE timeframe='1Day' AND CAST(ts AS DATE) BETWEEN ? AND ?",
                [start, end],
            ).fetchone()
            und = self.con.execute(
                "SELECT count(*), sum(close) FROM underlying_bars "
                "WHERE timeframe='1Day' AND CAST(ts AS DATE) BETWEEN ? AND ?",
                [start, end],
            ).fetchone()
            und_name = '*'
        else:
            placeholders = ','.join('?' * len(self.underlyings))
            row = self.con.execute(
                "SELECT count(*), sum(close), min(ts), max(ts) FROM option_bars "
                f"WHERE underlying IN ({placeholders}) AND timeframe='1Day' "
                "AND CAST(ts AS DATE) BETWEEN ? AND ?",
                list(self.underlyings) + [start, end],
            ).fetchone()
            und = self.con.execute(
                "SELECT count(*), sum(close) FROM underlying_bars "
                f"WHERE symbol IN ({placeholders}) AND timeframe='1Day' "
                "AND CAST(ts AS DATE) BETWEEN ? AND ?",
                list(self.underlyings) + [start, end],
            ).fetchone()
            und_name = ','.join(self.underlyings)
        return {
            'provider': 'alpaca',
            'access_method': 'alpaca_cli',
            'underlying': und_name,
            'underlyings': list(self.underlyings),
            'feed': 'sip (underlying) / indicative (options)',
            'adjustment': 'split (underlying) -- NOT dividend-adjusted, so it matches option strikes',
            'timeframe': '1Day',
            'window': {'start': str(start), 'end': str(end)},
            'option_bars_in_window': row[0],
            'option_close_sum': round(row[1], 4) if row[1] else 0.0,
            'option_first_ts': str(row[2])[:10] if row[2] else None,
            'option_last_ts': str(row[3])[:10] if row[3] else None,
            'underlying_bars_in_window': und[0],
            'underlying_close_sum': round(und[1], 4) if und[1] else 0.0,
        }

    def close(self):
        self.con.close()


class DictStore:
    """In-memory store for tests -- same surface, no database."""

    def __init__(self, sessions, underlying_closes, option_closes, chains, expiries):
        self._sessions = sorted(sessions)
        self._underlying = underlying_closes
        self._opt_close = option_closes
        self._chain = chains
        self._expiries = sorted(expiries)
        self._ohlc = {}

    def trading_days(self, start, end):
        return [d for d in self._sessions if start <= d <= end]

    def underlying_close(self, symbol, date):
        return self._underlying.get((symbol, date))

    def option_close(self, occ, date):
        return self._opt_close.get((occ, date))

    def expiries_after(self, underlying, date):
        found = sorted({e for (u, e, _d) in self._chain if u == underlying and e >= date})
        if found:
            return found
        return [e for e in self._expiries if e >= date]

    def chain_for(self, underlying, expiry, date):
        return self._chain.get((underlying, expiry, date), [])

    def chains_in_window(self, underlying, date, min_dte, max_dte):
        rows = []
        for exp in self.expiries_after(underlying, date):
            dte = (exp - date).days
            if min_dte <= dte <= max_dte:
                for r in self.chain_for(underlying, exp, date):
                    row = dict(r)
                    row['dte'] = dte
                    rows.append(row)
        return rows

    def realized_vol(self, symbol, date, window=21):
        """Same contract as Store.realized_vol -- strictly-prior sessions only."""
        prior = [d for d in self._sessions if d < date][-window:]
        closes = [self._underlying.get((symbol, d)) for d in prior]
        closes = [c for c in closes if c]
        if len(closes) < 3:
            return None
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
        if len(rets) < 2:
            return None
        return statistics.stdev(rets) * math.sqrt(252)

    def closes_before(self, symbol, date, n=21):
        prior = [d for d in self._sessions if d < date][-n:]
        return [c for c in (self._underlying.get((symbol, d)) for d in prior) if c]

    def ohlc_before(self, symbol, date, n=400):
        return None
