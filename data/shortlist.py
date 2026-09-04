"""
Census shortlist (liquid option underlyings) loaded from CSV.

The CSV is the option-OI universe: CORE (dailies) then EXTENDED (7–12 expiry
tier). Ingest pulls OHLCV + option bars for every OCC-safe root. Live trading
and the backtest book apply skill filters (no leveraged / crypto / vol-ETP
warehouses) and, for live chain snapshots, a bucket cap so a cycle does not
pull two hundred indicative chains.
"""
from __future__ import annotations

import csv
import os
import re

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
DEFAULT_PATH = os.path.join(REPO, 'data', 'shortlist.csv')

# OCC roots in this file are 1–6 letters. Dotted class-B tickers (BRK.B) cannot
# be probed with occ.build(); they still get underlying bars.
OCC_ROOT = re.compile(r'^[A-Z]{1,6}$')
BANNED_FACTORS = frozenset({'crypto_proxy', 'leveraged', 'vol_etp'})


def load(path=None):
    path = path or DEFAULT_PATH
    with open(path, newline='') as f:
        rows = []
        for row in csv.DictReader(f):
            row['symbol'] = (row.get('symbol') or '').strip().upper()
            row['bucket'] = (row.get('bucket') or '').strip().upper()
            row['kind'] = (row.get('kind') or '').strip().lower()
            row['factor'] = (row.get('factor') or '').strip().lower()
            em = (row.get('event_mode') or '').strip().lower()
            row['event_mode'] = em in ('true', '1', 'yes')
            if row['symbol']:
                rows.append(row)
        return rows


def occ_safe(symbol):
    return bool(OCC_ROOT.match((symbol or '').upper()))


def symbols(rows=None, path=None, buckets=None, kinds=None,
            exclude_factors=None, exclude_event_mode=False, occ_only=False):
    """Filtered ticker list, preserving CSV order."""
    rows = rows if rows is not None else load(path)
    buckets = {b.upper() for b in buckets} if buckets else None
    kinds = {k.lower() for k in kinds} if kinds else None
    banned = BANNED_FACTORS if exclude_factors is None else {f.lower() for f in exclude_factors}
    out = []
    for r in rows:
        s = r['symbol']
        if buckets and r['bucket'] not in buckets:
            continue
        if kinds and r['kind'] not in kinds:
            continue
        if r['factor'] in banned:
            continue
        if exclude_event_mode and r['event_mode']:
            continue
        if occ_only and not occ_safe(s):
            continue
        if s not in out:
            out.append(s)
    return out


def all_symbols(path=None, occ_only=False):
    """Every name in the file (ingest). No skill filter."""
    return symbols(path=path, buckets=None, kinds=None, exclude_factors=(),
                   exclude_event_mode=False, occ_only=occ_only)


def tradable(path=None, buckets=None, kinds=None, exclude_event_mode=True):
    """Skill-safe names for the live/backtest book."""
    return symbols(path=path, buckets=buckets, kinds=kinds,
                   exclude_factors=BANNED_FACTORS,
                   exclude_event_mode=exclude_event_mode, occ_only=True)


if __name__ == '__main__':
    rows = load()
    assert len(rows) >= 100, len(rows)
    core = tradable(buckets=['CORE'], exclude_event_mode=False)
    assert core == ['SPY', 'QQQ', 'IWM', 'GLD', 'XLF', 'SMH'], core
    all_occ = all_symbols(occ_only=True)
    assert 'SPY' in all_occ and 'TQQQ' in all_occ
    assert 'BRK.B' not in all_occ
    no_lev = tradable(buckets=['CORE', 'EXTENDED'], kinds=['etf'], exclude_event_mode=False)
    assert 'TQQQ' not in no_lev and 'VXX' not in no_lev and 'IBIT' not in no_lev
    assert 'XBI' in no_lev and 'XLV' in no_lev
    print('shortlist.py self-check OK', len(rows), 'rows', len(all_occ), 'OCC roots',
          'CORE', core)
