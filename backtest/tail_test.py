"""
The tail the sample does not contain, bounded exactly rather than guessed.

The book carries a beta of 0.91 to SPY and was measured over 2024-2026, a window with
no bear market in it. The obvious objection -- "what does this do in 2022" -- cannot be
answered by backtesting, because the option history begins in February 2024.

It can, however, be answered by ARITHMETIC, and that is what this does. A short put
vertical has a closed-form worst case: once the underlying falls a full wing below the
short strike, the position is at its defined maximum loss and cannot lose more. So for
every session in the sample, take the book that was actually open that day, apply an
instantaneous move to every underlying, and value each position at its expiry payoff:

    loss per spread = min(width, max(0, K_short - S x (1 + shock))) - credit

No model, no volatility assumption, no fill assumption. The number is exact given the
positions, and the worst session in the sample is the honest answer to "how bad can one
gap be". It ignores the recovery a real position would often get from time remaining,
so it is a CEILING on the loss, which is the direction a risk number should err in.

Two other things, both from data that is in the sample:

  * The worst realised windows. The book's marked return over exactly the sessions that
    were worst for SPY -- 1, 5 and 20 days. Small comfort, but it is measurement rather
    than extrapolation.
  * Beta is not linear for a short-vol book. The regression beta of 0.91 is fitted on
    ordinary days. The gap table shows how far the real convexity runs ahead of it.

Usage:
    python3 backtest/tail_test.py runs/2026-08-30_book16_frozen-b16_1Day
"""
import csv
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, 'data'))
sys.path.insert(0, os.path.join(REPO, 'agent'))

import metrics
from store import Store

SHOCKS = [-0.03, -0.05, -0.10, -0.15, -0.20, -0.30]
CONTRACT = 100


def load_run(run_dir):
    trades = []
    for r in csv.DictReader(open(os.path.join(run_dir, 'trades.csv'))):
        if r.get('structure') not in ('vertical', '') and r.get('structure'):
            if r['structure'] != 'vertical':
                continue
        trades.append({
            'underlying': r['underlying'],
            'entry': dt.date.fromisoformat(r['entry_date'][:10]),
            'exit': dt.date.fromisoformat(r['exit_date'][:10]),
            'k_short': float(r['short_strike']), 'width': float(r['width']),
            'credit': float(r['credit'] or 0), 'qty': float(r['qty']),
            'max_loss': float(r['max_loss']),
        })
    equity = [{'date': dt.date.fromisoformat(r['date'][:10]),
               'mtm': float(r.get('equity_mtm') or r['equity'])}
              for r in csv.DictReader(open(os.path.join(run_dir, 'equity.csv')))]
    return trades, equity


def gap_loss(trades, equity, store, shocks=SHOCKS):
    """
    For each session, the book open that day valued at expiry payoff after an
    instantaneous move. Returns worst and median across sessions, per shock.
    """
    eq = {e['date']: e['mtm'] for e in equity}
    out = {}
    for shock in shocks:
        per_day = []
        for e in equity:
            d = e['date']
            open_now = [t for t in trades if t['entry'] <= d < t['exit']]
            if not open_now:
                continue
            loss = 0.0
            for t in open_now:
                spot = store.underlying_close(t['underlying'], d)
                if not spot:
                    continue
                shocked = spot * (1.0 + shock)
                intrinsic = min(t['width'], max(0.0, t['k_short'] - shocked))
                loss += (t['credit'] - intrinsic) * CONTRACT * t['qty']
            per_day.append((d, loss / eq[d] if eq[d] else 0.0, len(open_now)))
        if not per_day:
            continue
        per_day.sort(key=lambda x: x[1])
        worst = per_day[0]
        mid = per_day[len(per_day) // 2]
        out[f"{shock:.0%}"] = {
            'worst_pct_of_equity': worst[1], 'worst_date': worst[0].isoformat(),
            'worst_positions': worst[2],
            'median_pct_of_equity': mid[1],
            'p95_pct_of_equity': per_day[int(0.05 * len(per_day))][1],
            'sessions_with_book': len(per_day),
        }
    return out


def worst_windows(equity, store, symbol='SPY', lengths=(1, 5, 20), top=5):
    """The book's marked return over exactly the windows that were worst for SPY."""
    days = [e['date'] for e in equity]
    mtm = [e['mtm'] for e in equity]
    px = [store.underlying_close(symbol, d) for d in days]
    out = {}
    for L in lengths:
        rows = []
        for i in range(len(days) - L):
            if not (px[i] and px[i + L] and mtm[i]):
                continue
            rows.append({'from': days[i].isoformat(), 'to': days[i + L].isoformat(),
                         'spy': px[i + L] / px[i] - 1.0,
                         'book': mtm[i + L] / mtm[i] - 1.0})
        rows.sort(key=lambda r: r['spy'])
        out[f'{L}d'] = rows[:top]
    return out


def main():
    run_dir = (sys.argv[1] if len(sys.argv) > 1
               else os.path.join(REPO, 'runs', '2026-08-30_book16_frozen-b16_1Day'))
    if not os.path.isabs(run_dir):
        run_dir = os.path.join(REPO, run_dir)
    trades, equity = load_run(run_dir)
    names = sorted({t['underlying'] for t in trades})
    store = Store(os.path.join(REPO, 'data', 'market.duckdb'), underlyings=names)

    print(f"book: {os.path.basename(run_dir)}   {len(trades)} trades, {len(names)} names, "
          f"{len(equity)} sessions\n")

    print("Instantaneous gap, book valued at expiry payoff. Exact, no model.")
    print(f"  {'move':>6}{'worst session':>16}{'when':>13}{'positions':>11}{'5th pct':>10}{'median':>9}")
    gaps = gap_loss(trades, equity, store)
    for shock, g in gaps.items():
        print(f"  {shock:>6}{g['worst_pct_of_equity']:>+16.1%}{g['worst_date']:>13}"
              f"{g['worst_positions']:>11}{g['p95_pct_of_equity']:>+10.1%}"
              f"{g['median_pct_of_equity']:>+9.1%}")

    print("\nWorst realised windows for SPY, and what the book did in exactly those.")
    ww = worst_windows(equity, store)
    for L, rows in ww.items():
        print(f"  --- {L}")
        for r in rows:
            print(f"      {r['from']} -> {r['to']}   SPY {r['spy']:>+7.2%}   book {r['book']:>+7.2%}")

    out = {'run': os.path.basename(run_dir), 'gap': gaps, 'worst_windows': ww,
           'note': 'gap losses value every open spread at its expiry payoff after an '
                   'instantaneous move: a ceiling on the loss, since time remaining is '
                   'ignored'}
    path = os.path.join(run_dir, 'tail.json')
    with open(path, 'w') as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nwritten {os.path.relpath(path, REPO)}")


if __name__ == '__main__':
    main()
