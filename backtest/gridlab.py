"""
Grid lab: run many engine configurations against one loaded store and report both
equity bases.

Two questions get confused whenever a backtest's return goes up, and this exists to
keep them apart:

    return rose because more capital was deployed   -> loading, not edge
    return rose per unit of risk taken              -> edge

So every row reports total return AND Sharpe AND the average fraction of NAV actually
at risk. A configuration whose return scaled with `avg_risk_pct` while Sharpe stayed
flat has discovered leverage, which needs no research to find.

Equity is marked to market. A realised-cash curve cannot show a drawdown that has not
been closed yet, and at the concurrency levels this grid explores that is most of the
drawdown.

Grids are declared in GRIDS. Results append to a JSONL keyed by config, so a run is
resumable and two passes never redo the same cell.

Usage:
    python3 backtest/gridlab.py --grid load --out runs/_grid/load.jsonl [--jobs 2]
"""
import datetime as dt
import itertools
import json
import multiprocessing as mp
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, 'data'))
sys.path.insert(0, os.path.join(REPO, 'agent'))

import fills
import metrics
import run as RUN
from engine import Engine
from store import Store

NAMES = ['SPY', 'QQQ', 'IWM', 'GLD', 'XLF', 'SMH']

BASE = {'min_dte': 4, 'max_dte': 7, 'har_horizon': 5, 'equity_basis': 'mtm',
        'drawdown_halt': 0.99, 'max_contracts': 400, 'structure': 'vertical',
        'side': 'put', 'risk_pct': 0.02, 'concurrent': 3, 'max_per_name': 1,
        'max_portfolio_risk': 1.0, 'iv_rv': 1.2, 'delta': 0.20,
        'take_profit': 0.50, 'stop_loss': 2.0, 'width': 5.0,
        'wing_sigmas': 'none', 'min_credit_ratio': 0.15,
        'min_iv_har_ratio': 'none', 'max_overnight_share': 'none',
        'vol_target': 'none', 'friction': 'measured'}

# Each grid is {option: [values]}; the cross product is run.
GRIDS = {
    # Does return scale with capital deployed, and does Sharpe survive it?
    'load': {
        'structure_side': ['vertical:put', 'condor:both'],
        'risk_pct': [0.02, 0.05, 0.10, 0.20],
        'concurrent': [6, 20],
        'max_per_name': [1, 3],
    },
    # Exits first: they change what the other knobs are optimising against.
    'exits': {
        'structure_side': ['vertical:put', 'condor:both'],
        'take_profit': [0.25, 0.35, 0.50, 0.65, 0.80],
        'stop_loss': [1.25, 1.5, 2.0, 3.0, 99.0],
    },
    # The vol gate and the strike. Anything found here has to survive OOT.
    'quality': {
        'structure_side': ['vertical:put', 'condor:both'],
        'iv_rv': ['none', 1.0, 1.2, 1.4],
        'delta': [0.10, 0.15, 0.20, 0.30],
    },
    'tenor': {
        'structure_side': ['vertical:put', 'condor:both'],
        'dte': ['1:3', '4:7', '8:15', '4:15'],
        'delta': [0.15, 0.20],
        'width': [2.0, 5.0, 10.0],
    },
    # Wing geometry. A fixed $5 wing is not one structure across tenors: at SPY's vol
    # it is 1.05 sigma at DTE 1 and 0.33 sigma at DTE 10. `wing_sigmas` holds the wing
    # constant in standard deviations instead, which is the comparison the DTE ladder
    # was never able to make.
    'geometry': {
        'structure_side': ['condor:both'],
        'wing_sigmas': ['none', 0.5, 0.75, 1.0, 1.5],
        'min_credit_ratio': [0.08, 0.15, 0.25],
    },
    # The two second gates the feature lab built and left switched off, swept against
    # the incumbent IV/RV gate on the whole six-name book rather than on SPY alone.
    'gates': {
        'structure_side': ['condor:both'],
        'iv_rv': [1.0, 1.2, 1.4],
        'min_iv_har_ratio': ['none', 1.0, 1.162268, 1.3],
        'max_overnight_share': ['none', 0.36, 0.55],
    },
    # The volatility overlay against the leverage ladder. The load grid says return
    # scales with risk per trade to ~10% and then goes negative; if that cliff is
    # variance drag rather than a worse edge, targeting volatility should move it.
    'voltarget': {
        'structure_side': ['condor:both'],
        'risk_pct': [0.05, 0.10, 0.20, 0.35],
        'vol_target': ['none', 0.10, 0.15, 0.25],
        'max_per_name': [3],
        'concurrent': [20],
    },
    # Where the exits and quality grids point: put credit verticals, struck closer to
    # the money, with a looser vol gate and a stop that is not there. Swept around that
    # point to check it is a plateau rather than one lucky cell.
    'champion': {
        'structure_side': ['vertical:put'],
        'delta': [0.20, 0.25, 0.30, 0.35, 0.40],
        'iv_rv': ['none', 0.9, 1.0, 1.1, 1.2],
        'stop_loss': [2.0, 3.0, 99.0],
        'concurrent': [20],
        'max_per_name': [3],
    },
    # The decisive grid. Selling 0.30-delta puts into a market that rose 56% carries a
    # beta of 1.27; the question is whether anything here earns a return at 3% friction
    # WITHOUT being the index in disguise. Beta and alpha are reported per cell.
    'beta': {
        'structure_side': ['vertical:put', 'vertical:both', 'condor:both'],
        'delta': [0.15, 0.20, 0.25, 0.30],
        'iv_rv': [1.0, 1.2],
        'stop_loss': [99.0],
        'friction': ['measured', 0.03],
        'concurrent': [20],
        'max_per_name': [3],
    },
    # Everything the earlier grids survived, put together and then levered: the
    # beta-neutral two-sided book, the stop removed, the gate at 1.0, tranches on,
    # and the volatility overlay that removed the leverage cliff. Priced at 3% as
    # well as at the measured floor, because 3% is this repo's stated honest cost.
    'final': {
        'structure_side': ['vertical:both', 'vertical:put', 'condor:both'],
        'delta': [0.20],
        'iv_rv': [1.0],
        'stop_loss': [99.0],
        'risk_pct': [0.02, 0.05, 0.10, 0.15],
        'vol_target': [0.25],
        'friction': ['measured', 0.03],
        'concurrent': [20],
        'max_per_name': [3],
    },
    'smoke': {
        'structure_side': ['vertical:put'],
        'risk_pct': [0.02, 0.10],
    },
}

_STORE = None
_WINDOW = None


def _init():
    global _STORE, _WINDOW
    _STORE = Store(os.path.join(REPO, 'data', 'market.duckdb'), underlyings=NAMES)
    cov = _STORE.coverage()
    _WINDOW = (dt.date.fromisoformat(cov['option_bars']['first']),
               dt.date.fromisoformat(cov['option_bars']['last']))


def _opts(cell, names=None):
    o = dict(BASE)
    o.update(cell)
    if 'structure_side' in o:
        st, sd = o.pop('structure_side').split(':')
        o['structure'], o['side'] = st, sd
    if 'dte' in o:
        lo, hi = o.pop('dte').split(':')
        o['min_dte'], o['max_dte'] = lo, hi
    return {k: str(v) for k, v in o.items()}


def evaluate(cell, names=NAMES, sub_windows=True):
    start, end = _WINDOW
    cell = dict(cell)
    # `friction` is a grid axis like any other: MeasuredSpread is calibrated on two
    # calm sessions and is the FLOOR of the cost, so a configuration that only works
    # there has not been tested, it has been flattered.
    fr = cell.pop('friction', 'measured')
    friction = fills.MeasuredSpread() if str(fr) == 'measured' else float(fr)
    opts = _opts(cell)
    cfg = RUN.build_config(opts, friction, list(names))
    eng = Engine(_STORE, cfg)
    res = eng.run(start, end)
    curve = res['equity_curve']
    days = [r['date'] for r in curve]
    pnls = [t['pnl'] for t in res['trades']]
    mtm = [r.get('equity_mtm', r['equity']) for r in curve]
    cash = [r['equity'] for r in curve]
    m = metrics.summarize(mtm, pnls, trading_days=len(days))
    mc = metrics.summarize(cash, pnls, trading_days=len(days))
    row = {
        'trades': m['trades'], 'total_return': m['total_return'],
        'ann_return': m['annualized_return'], 'sharpe': m['sharpe'],
        'max_dd': m['max_drawdown'], 'win_rate': m['win_rate'],
        'profit_factor': m['profit_factor'],
        'cash_return': mc['total_return'], 'cash_sharpe': mc['sharpe'],
        'cash_max_dd': mc['max_drawdown'],
        'avg_open': sum(r['open_positions'] for r in curve) / max(len(curve), 1),
        'avg_risk_pct': (sum(r.get('risk_committed', 0.0) for r in curve)
                         / max(len(curve), 1) / cfg['initial_cash']),
        'halted': res['halted'],
    }
    # Beta and alpha live in metrics as a pure function of two series. Calling into
    # another module that imports THIS one gave a second copy of it whose `_STORE`
    # was None the moment gridlab ran as __main__ -- every cell then reported a
    # clean zero, which is the most dangerous shape a research failure can take.
    row.update(metrics.market_regression(
        mtm, [_STORE.underlying_close('SPY', d) for d in days]))
    if sub_windows:
        # Per calendar year, on the marked curve. A configuration that only works in
        # one of the three years is a regime, not a strategy, and this is the cheapest
        # place to see it.
        for yr in (2024, 2025, 2026):
            idx = [i for i, d in enumerate(days) if d.year == yr]
            if len(idx) > 20:
                seg = mtm[idx[0]:idx[-1] + 1]
                sm = metrics.summarize(seg, [], trading_days=len(seg))
                row[f'ret_{yr}'] = sm['total_return']
                row[f'sharpe_{yr}'] = sm['sharpe']
    return row


def _work(item):
    key, cell = item
    t0 = time.time()
    try:
        r = evaluate(cell)
    except Exception as e:
        r = {'error': f"{type(e).__name__}: {e}"}
    r.update(cell)
    r['key'] = key
    r['secs'] = round(time.time() - t0, 1)
    return r


def main():
    argv = sys.argv[1:]
    grid_name, out, jobs = 'smoke', None, 2
    for i, a in enumerate(argv):
        if a == '--grid':
            grid_name = argv[i + 1]
        elif a == '--out':
            out = argv[i + 1]
        elif a == '--jobs':
            jobs = int(argv[i + 1])
    g = GRIDS[grid_name]
    out = out or f'runs/_grid/{grid_name}.jsonl'
    path = out if os.path.isabs(out) else os.path.join(REPO, out)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    done = set()
    if os.path.exists(path):
        for line in open(path):
            try:
                done.add(json.loads(line)['key'])
            except Exception:
                pass

    keys = list(g)
    cells = []
    for values in itertools.product(*(g[k] for k in keys)):
        cell = dict(zip(keys, values))
        key = '|'.join(f"{k}={cell[k]}" for k in keys)
        if key not in done:
            cells.append((key, cell))
    print(f"grid {grid_name}: {len(cells)} to run ({len(done)} already done)", flush=True)

    with open(path, 'a') as f:
        if jobs > 1:
            ctx = mp.get_context('fork')
            _init()
            with ctx.Pool(jobs) as pool:
                for n, r in enumerate(pool.imap_unordered(_work, cells), 1):
                    f.write(json.dumps(r, default=str) + '\n'); f.flush()
                    print(f"[{n}/{len(cells)}] {r['key']:<60} "
                          f"ret {r.get('total_return', 0):>+8.2%} sh {r.get('sharpe', 0):>5.2f} "
                          f"dd {r.get('max_dd', 0):>+7.2%} n {r.get('trades', 0):>4} "
                          f"risk {r.get('avg_risk_pct', 0):>5.1%} {r.get('error', '')}", flush=True)
        else:
            _init()
            for n, item in enumerate(cells, 1):
                r = _work(item)
                f.write(json.dumps(r, default=str) + '\n'); f.flush()
                print(f"[{n}/{len(cells)}] {r['key']:<60} "
                      f"ret {r.get('total_return', 0):>+8.2%} sh {r.get('sharpe', 0):>5.2f} "
                      f"dd {r.get('max_dd', 0):>+7.2%} n {r.get('trades', 0):>4} "
                      f"risk {r.get('avg_risk_pct', 0):>5.1%} {r.get('error', '')}", flush=True)


if __name__ == '__main__':
    main()
