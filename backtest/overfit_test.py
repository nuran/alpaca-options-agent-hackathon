"""
What the search itself cost: deflated Sharpe and probability of backtest overfitting.

A grid returns its own best cell by construction. If 376 configurations are run and the
best one is reported, its Sharpe is the maximum of 376 draws, and the maximum of 376
draws from a distribution centred on zero is not zero. Two corrections exist for this
and the repository already implements both in `stats.py`; neither had been applied to
the configuration that was actually selected.

  DEFLATED SHARPE (Bailey & Lopez de Prado). Discounts the observed Sharpe by the
  benchmark E[max SR] that N trials of the observed cross-trial variance would produce
  under a true Sharpe of zero. DSR is then the probability that the true Sharpe is
  positive. Below ~0.95 the result is not distinguishable from the best of N noise
  draws.

  PBO / CSCV. Split the sample into s contiguous blocks; over every way of choosing half
  the blocks as in-sample, pick the in-sample winner and record where it lands out of
  sample. PBO is the share of splits where the in-sample winner falls into the bottom
  half out of sample. It answers the question the grid cannot: if I select on one half,
  how often does that selection disappoint on the other?

Scope, stated because it decides how to read the output: the parameter selection
happened on the SIX-name book (exits, quality, tenor, load, champion grids). That is the
search space, and it is what gets deflated here. The sixteen-name result is a holdout
confirmation of an already-frozen configuration, not a selection, so it is not subject
to this deflation -- but its Sharpe is reported against the same benchmark anyway,
because a reader is entitled to the conservative number.

Usage:
    python3 backtest/overfit_test.py [--out runs/_grid/overfit.json] [--blocks 8]
"""
import datetime as dt
import glob
import json
import os
import statistics as st
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, 'data'))
sys.path.insert(0, os.path.join(REPO, 'agent'))

import fills
import gridlab
import metrics
import run as RUN
import stats as STATS
from engine import Engine
from store import Store

# The grid whose cells produced the final delta / gate / stop choice. PBO is run over
# these because they are mutually comparable: same book, same window, same cost model,
# one cell per parameter combination.
SELECTION_GRID = 'champion'


def trial_sharpes():
    """Every annualised Sharpe this repository's grids produced, with provenance."""
    by_grid, all_sr = {}, []
    for path in sorted(glob.glob(os.path.join(REPO, 'runs', '_grid', '*.jsonl'))):
        name = os.path.basename(path)[:-6]
        srs = []
        for line in open(path):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if 'error' in r or 'sharpe' not in r:
                continue
            srs.append(float(r['sharpe']))
        if srs:
            by_grid[name] = srs
            all_sr.extend(srs)
    return by_grid, all_sr


def daily_returns_for(cell, names, friction, store, start, end):
    cfg = RUN.build_config(gridlab._opts(cell), friction, list(names))
    res = Engine(store, cfg).run(start, end)
    curve = res['equity_curve']
    eq = [r.get('equity_mtm', r['equity']) for r in curve]
    return metrics.daily_returns(eq), [r['date'] for r in curve]


def main():
    out_path = os.path.join(REPO, 'runs', '_grid', 'overfit.json')
    blocks = 8
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == '--out':
            out_path = argv[i + 1]
        elif a == '--blocks':
            blocks = int(argv[i + 1])

    by_grid, all_sr = trial_sharpes()
    total = len(all_sr)
    print(f"search space actually run: {total} cells across {len(by_grid)} grids")
    for g, s in sorted(by_grid.items(), key=lambda kv: -len(kv[1])):
        print(f"  {g:<12}{len(s):>4} cells   Sharpe {min(s):+.2f} … {max(s):+.2f}")
    print(f"  cross-trial Sharpe sd = {st.pstdev(all_sr):.3f}\n")

    gridlab._init()
    store, (start, end) = gridlab._STORE, gridlab._WINDOW

    # --- the selected configuration, on the book it was selected on
    champion_6 = dict(structure_side='vertical:put', delta=0.20, iv_rv=1.0,
                      stop_loss=99.0, concurrent=20, max_per_name=3)
    rets6, _ = daily_returns_for(champion_6, gridlab.NAMES, fills.MeasuredSpread(),
                                 store, start, end)

    results = {'trials_total': total, 'trials_by_grid': {g: len(s) for g, s in by_grid.items()},
               'trial_sharpe_sd': st.pstdev(all_sr)}

    print("Deflated Sharpe -- champion on the six names it was selected on (measured cost)")
    for label, n_trials in (('champion grid only', len(by_grid.get(SELECTION_GRID, []))),
                            ('every cell run', total)):
        d = STATS.deflated_sharpe(rets6, n_trials, trial_sharpes=all_sr)
        results[f'dsr_{label.replace(" ", "_")}'] = d
        verdict = 'PASSES 0.95' if d['dsr'] >= 0.95 else 'FAILS the 0.95 bar'
        print(f"  N={n_trials:>4}  SR {d['sharpe']:.2f}  benchmark SR* {d['sr_star']:.2f}  "
              f"DSR {d['dsr']:.3f}   {verdict}")

    # --- and on the sixteen-name book, which was a holdout rather than a selection
    print("\nSame benchmark applied to the sixteen-name book at 3% (a holdout, not a selection)")
    store16 = Store(os.path.join(REPO, 'data', 'market.duckdb'),
                    underlyings=sorted(gridlab.NAMES + ['USO', 'IEF', 'UNG', 'TLT', 'ASHR',
                                                        'XLE', 'XLU', 'IBIT', 'XLV', 'SLV']))
    cov = store16.coverage()
    s16, e16 = (dt.date.fromisoformat(cov['option_bars']['first']),
                dt.date.fromisoformat(cov['option_bars']['last']))
    champion_16 = dict(champion_6, vol_target=0.25)
    rets16, _ = daily_returns_for(champion_16, store16.underlyings, 0.03, store16, s16, e16)
    d16 = STATS.deflated_sharpe(rets16, total, trial_sharpes=all_sr)
    results['dsr_book16'] = d16
    print(f"  N={total:>4}  SR {d16['sharpe']:.2f}  benchmark SR* {d16['sr_star']:.2f}  "
          f"DSR {d16['dsr']:.3f}   "
          f"{'PASSES 0.95' if d16['dsr'] >= 0.95 else 'FAILS the 0.95 bar'}")
    store16.close()

    # --- PBO over the selection grid
    print(f"\nPBO / CSCV over the '{SELECTION_GRID}' grid "
          f"({len(gridlab.GRIDS[SELECTION_GRID])} axes) -- re-running each cell for its return series")
    import itertools
    g = gridlab.GRIDS[SELECTION_GRID]
    keys = list(g)
    matrix = {}
    cells = list(itertools.product(*(g[k] for k in keys)))
    for n, values in enumerate(cells, 1):
        cell = dict(zip(keys, values))
        key = '|'.join(f"{k}={cell[k]}" for k in keys)
        try:
            r, _ = daily_returns_for(cell, gridlab.NAMES, fills.MeasuredSpread(),
                                     store, start, end)
            if any(x != 0 for x in r):
                matrix[key] = r
        except Exception as e:
            print(f"  [{n}/{len(cells)}] {key} -> {type(e).__name__}", flush=True)
            continue
        if n % 10 == 0 or n == len(cells):
            print(f"  [{n}/{len(cells)}] variants collected: {len(matrix)}", flush=True)

    pbo = STATS.pbo_cscv(matrix, s=blocks)
    results['pbo'] = pbo
    print(f"\n  PBO = {pbo['pbo']:.3f} over {pbo['splits']} splits, "
          f"{pbo['variants']} variants, {pbo['periods']} periods")
    print(f"  median logit {pbo.get('median_logit', float('nan')):+.3f}  "
          f"({'below 0.5 -- selection generalises more often than not' if pbo['pbo'] < 0.5 else 'at or above 0.5 -- the selection does not generalise'})")

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nwritten {out_path}")


if __name__ == '__main__':
    main()
