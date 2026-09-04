#!/usr/bin/env python3
"""
Rebuild `data/market.duckdb` from the committed Parquet export.

This is the half of the round-trip that makes "reproducible without re-downloading"
true rather than merely claimed. `data/ingest.py export` in the development repo
writes `data/export/*.parquet`; nothing read them back, so a fresh clone had the
files and no way to turn them into the store the backtest opens. This does that,
offline, in a few seconds.

What ships, and what that means for what you can reproduce:

  option_bars      SPY, QQQ, IWM, GLD, XLF, SMH -- 1,661,101 daily bars,
                   2024-01-22 .. 2026-08-26, one Parquet per underlying
  underlying_bars  the same six names
  calendar         NYSE sessions over the same window
  news             1,952 Benzinga articles, the live agent's veto tape
  ingest_log       provenance: which window of which endpoint was fetched when

The six names are the ones with option history. Account C's rulebook lists twelve;
USO, SLV, TLT, IBIT, UNG and XLV have no option bars in this export because they
have none in the source store either. A backtest of C's book therefore covers half
its universe, and `backtest/run.py` says which names it dropped rather than
silently thinning the book. That limitation is real and is described in the README.

Usage:
    python3 data/restore.py                 # -> data/market.duckdb
    python3 data/restore.py --db /tmp/x.duckdb --force
"""
import argparse
import glob
import os
import sys

import duckdb

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
EXPORT_DIR = os.path.join(REPO_ROOT, 'data', 'export')
SCHEMA = os.path.join(REPO_ROOT, 'data', 'schema.sql')
DEFAULT_DB = os.path.join(REPO_ROOT, 'data', 'market.duckdb')

# Tables restored from a single Parquet file. `option_bars` is handled separately
# because it is split per underlying to keep every file well inside GitHub's
# large-file warning.
SINGLE = ['underlying_bars', 'calendar', 'news', 'ingest_log']


def restore(db_path=DEFAULT_DB, export_dir=EXPORT_DIR, force=False):
    if os.path.exists(db_path):
        if not force:
            print(f"{db_path} already exists. Pass --force to rebuild it.", file=sys.stderr)
            return 1
        os.remove(db_path)
        for suffix in ('.wal',):
            if os.path.exists(db_path + suffix):
                os.remove(db_path + suffix)

    if not os.path.isdir(export_dir):
        print(f"no export directory at {export_dir}", file=sys.stderr)
        return 2

    con = duckdb.connect(db_path)
    # The schema carries the primary keys and column types; loading straight from
    # Parquet with CREATE TABLE AS would silently drop both.
    with open(SCHEMA) as fh:
        con.execute(fh.read())

    total = 0
    parts = sorted(glob.glob(os.path.join(export_dir, 'option_bars_*.parquet')))
    if not parts:
        print(f"no option_bars_*.parquet in {export_dir}", file=sys.stderr)
        return 2
    for part in parts:
        name = os.path.basename(part)[len('option_bars_'):-len('.parquet')]
        con.execute(f"INSERT INTO option_bars SELECT * FROM read_parquet('{part}')")
        n = con.execute(f"SELECT count(*) FROM option_bars WHERE underlying = '{name}'").fetchone()[0]
        print(f"  option_bars   {name:<5} {n:>9,}")
        total += n

    for table in SINGLE:
        path = os.path.join(export_dir, f'{table}.parquet')
        if not os.path.exists(path):
            print(f"  {table:<15} (absent from the export, skipped)")
            continue
        con.execute(f"INSERT INTO {table} SELECT * FROM read_parquet('{path}')")
        n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        print(f"  {table:<21} {n:>9,}")
        total += n

    names = [r[0] for r in con.execute(
        "SELECT DISTINCT underlying FROM option_bars ORDER BY 1").fetchall()]
    span = con.execute("SELECT min(ts)::DATE, max(ts)::DATE FROM option_bars").fetchone()
    con.close()
    print(f"\n{db_path}: {total:,} rows, {len(names)} underlyings "
          f"({', '.join(names)}), {span[0]} .. {span[1]}")
    print("Run `make backtest` next.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--export-dir', default=EXPORT_DIR)
    ap.add_argument('--force', action='store_true', help='rebuild even if the store exists')
    a = ap.parse_args()
    return restore(a.db, a.export_dir, a.force)


if __name__ == '__main__':
    raise SystemExit(main())
