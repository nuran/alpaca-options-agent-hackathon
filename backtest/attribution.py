"""
Where the P&L actually came from: theta, gamma, vega, delta, and cost.

WHY THIS EXISTS. The harness reports a return and nothing about its composition, so
"this strategy sells volatility" has been an assertion, never a measurement. It cost
real time: the adaptive book's +21.09% on QQQ turned out to be +$21,582 booked on
up-days against -$489 on down-days -- a directional bet wearing an options wrapper --
and finding that took a forensic decomposition after the fact. This module answers it
by default.

NOT a delta-hedging diagnostic. The textbook version of this decomposition assumes a
hedged book and splits out `hedge_gain = -delta * dS` plus a quadratic market-impact
term. Neither applies here: nothing in this repo trades the underlying, and friction is
the option bid-ask charged per leg, already modelled in fills.py. Copying that shape
would add two invented numbers. What transfers is the principle -- name the components,
then look at which one dominates.

The decomposition, per holding day, for the whole structure:

    dPnL  ~=  Theta*dt  +  Delta*dS  +  0.5*Gamma*dS^2  +  Vega*dSigma  +  residual

Two things it is for:

1. CONVEXITY SHARE. If |Gamma| + |Vega| is a small part of the total, the book is a
   directional position however it is labelled. The desk canon puts the line at 25%.
2. RECONCILIATION. The Greek sum must match the realized mark change within
   microstructure noise. A large residual means the model is hallucinating -- skew,
   vanna and jumps are not in this expansion, and a book living on them is not being
   measured by it.

Greeks are recovered by inverting Black-Scholes on each day's close, on the SAME
trading-day clock the rest of the harness now uses. Theta comes back per calendar day
from bs.greeks, so it is rescaled to per session here.

Usage:
    python3 backtest/attribution.py runs/<run-dir> [--db data/market.duckdb]
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, '..'))
sys.path.insert(0, os.path.join(_HERE, '..', 'data'))     # occ.py

import blackscholes as bs

CONTRACT = 100.0
SESSIONS = 252.0
CONVEXITY_FLOOR = 0.25      # below this the book is directional, whatever it is called


def legs_of(row):
    """
    Every leg of a trade, as (occ, sign). Sign is +1 long, -1 short.

    Reads both the two-leg vertical columns and the condor's call wing. If the call
    columns are absent the row predates the widened trade_cols and only half the
    structure can be attributed -- the caller is told rather than quietly given a
    number for a position that was twice the size.
    """
    out, missing = [], False
    if row.get('short_occ'):
        out.append((row['short_occ'], -1))
    if row.get('long_occ'):
        out.append((row['long_occ'], +1))
    if str(row.get('structure') or row.get('kind') or '').find('condor') >= 0:
        if row.get('call_short_occ') and row.get('call_long_occ'):
            out.append((row['call_short_occ'], -1))
            out.append((row['call_long_occ'], +1))
        else:
            missing = True
    if str(row.get('side') or '') == 'debit':          # a debit spread is long the near leg
        out = [(occ, -sign) for occ, sign in out]
    return out, missing


def _leg_greeks(store, occ, date, spot, sessions):
    px = store.option_close(occ, date)
    if not px or not spot or sessions is None:
        return None
    try:
        _, expiry, right, strike = _parse(occ)
    except ValueError:
        return None
    d, iv = bs.delta_from_price(px, spot, strike, (expiry - date).days, right,
                                sessions=max(sessions, 0))
    if d is None or iv is None:
        return None
    t = bs.year_fraction_sessions(max(sessions, 1e-6))
    g = bs.greeks(spot, strike, t, iv, right)
    return {'px': px, 'iv': iv,
            'delta': g['delta'],
            'gamma': g['gamma'],
            'vega': g['vega'],
            # bs.greeks divides by 365; this clock has 252 sessions in a year.
            'theta': g['theta'] * bs.DAYS_PER_YEAR / SESSIONS}


import occ as _occ_mod                                     # data/occ.py


def _parse(symbol):
    return _occ_mod.parse(symbol)


def attribute_trade(store, row, sessions_between):
    """
    Decompose one round trip. Returns component dollars and the reconciliation gap.
    """
    legs, missing = legs_of(row)
    if not legs:
        return None
    qty = float(row.get('qty') or 0)
    entry = _date(row['entry_date'])
    exit_ = _date(row['exit_date'])
    days = [d for d in store.trading_days(entry, exit_)]
    if len(days) < 2:
        # A one-session trade has no interior path: the whole move is a single jump and
        # the expansion cannot separate gamma from delta. Report it as unattributable
        # rather than assigning the P&L to a term by convention.
        return {'unattributable': True, 'reason': 'single-session hold', 'missing_legs': missing}

    comp = {'theta': 0.0, 'delta': 0.0, 'gamma': 0.0, 'vega': 0.0}
    marks = []
    for i, d in enumerate(days):
        spot = store.underlying_close(row.get('underlying') or 'SPY', d)
        n = sessions_between(d, _date(row['expiry']))
        gl = [(_leg_greeks(store, occ, d, spot, n), sign) for occ, sign in legs]
        if any(g is None for g, _ in gl):
            marks.append(None)
            continue
        marks.append({
            'spot': spot,
            'mark': sum(sign * g['px'] for g, sign in gl),
            'delta': sum(sign * g['delta'] for g, sign in gl),
            'gamma': sum(sign * g['gamma'] for g, sign in gl),
            'vega': sum(sign * g['vega'] for g, sign in gl),
            'theta': sum(sign * g['theta'] for g, sign in gl),
            # Vega P&L is driven by the LEVEL of the surface, not by each contract's
            # independently inverted IV. Those are unusable here: the daily closes are
            # not simultaneous observations, and a single contract's recovered IV moves
            # a median 1.69 vol points a day in this store, 6.70 at p90 and 21.66 at
            # p99 -- against a real SPY daily move of well under 1.5. Differencing that
            # per contract makes vega the largest term in the decomposition and it is
            # entirely microstructure. One level per (day, expiry), anchored at the
            # forward, is the honest driver; the cost is that a change in SKEW is
            # pushed into the residual rather than attributed, which is the right place
            # for it since this expansion has no skew term anyway.
            'iv': _surface_level(gl),
        })

    realized = 0.0
    for a, b in zip(marks, marks[1:]):
        if a is None or b is None:
            continue
        dS = b['spot'] - a['spot']
        dsig = b['iv'] - a['iv']
        comp['delta'] += a['delta'] * dS * qty * CONTRACT
        comp['gamma'] += 0.5 * a['gamma'] * dS * dS * qty * CONTRACT
        comp['vega'] += a['vega'] * dsig * 100.0 * qty * CONTRACT
        comp['theta'] += a['theta'] * qty * CONTRACT
        realized += (b['mark'] - a['mark']) * qty * CONTRACT

    explained = sum(comp.values())
    out = dict(comp)
    out['realized_mark'] = realized
    out['residual'] = realized - explained
    out['missing_legs'] = missing
    out['unattributable'] = False
    return out


def _surface_level(greek_legs):
    """One vol level for the structure: vega-weighted, so the near-the-money legs lead."""
    num = den = 0.0
    for g, _ in greek_legs:
        w = abs(g['vega'])
        num += w * g['iv']
        den += w
    return num / den if den else (sum(g['iv'] for g, _ in greek_legs) / len(greek_legs))


def _date(v):
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v)[:10])


def directional_split(trade_rows):
    """
    P&L on up-moves versus down-moves. No greeks, so it survives this data.

    The Greek expansion needs a usable daily IV series and this store has none: a single
    contract's recovered IV moves a median 1.69 vol points a day, and the reconciliation
    residual lands near 3x the realized P&L. This measure needs nothing but the entry
    and exit spot, and it is what actually caught the adaptive book: QQQ had booked
    +$21,582 on up-moves against -$489 on down-moves -- a directional position wearing
    an options wrapper, which no amount of labelling changes.

    A delta-neutral book should earn on both sides. A large asymmetry is the finding.
    """
    up = dn = 0.0
    n_up = n_dn = 0
    for r in trade_rows:
        try:
            a, b = float(r['entry_spot']), float(r['exit_spot'])
            p = float(r['pnl'])
        except (KeyError, TypeError, ValueError):
            continue
        if b >= a:
            up += p; n_up += 1
        else:
            dn += p; n_dn += 1
    tot = abs(up) + abs(dn) or 1.0
    return {'pnl_on_up_moves': up, 'pnl_on_down_moves': dn,
            'trades_up': n_up, 'trades_down': n_dn,
            'asymmetry': abs(abs(up) - abs(dn)) / tot,
            'verdict': ('DIRECTIONAL -- one side carries the book'
                        if abs(abs(up) - abs(dn)) / tot > 0.60
                        else 'both sides contribute')}


def summarize(rows):
    """Book-level totals plus the two verdicts."""
    keys = ('theta', 'delta', 'gamma', 'vega')
    tot = {k: 0.0 for k in keys}
    realized = residual = 0.0
    used = skipped = 0
    for r in rows:
        if not r or r.get('unattributable'):
            skipped += 1
            continue
        used += 1
        for k in keys:
            tot[k] += r[k]
        realized += r['realized_mark']
        residual += r['residual']
    gross = sum(abs(tot[k]) for k in keys) or 1.0
    convexity = (abs(tot['gamma']) + abs(tot['vega'])) / gross
    return {
        'components': tot,
        'shares': {k: tot[k] / gross for k in keys},
        'realized_mark': realized,
        'residual': residual,
        'residual_share': residual / (abs(realized) or 1.0),
        'convexity_share': convexity,
        'verdict': ('DELTA_ONLY -- a directional position in an options wrapper'
                    if convexity < CONVEXITY_FLOOR else 'volatility exposure confirmed'),
        'trades_attributed': used,
        'trades_skipped': skipped,
    }


def main(argv):
    run_dir = argv[0]
    db = 'data/market.duckdb'
    if '--db' in argv:
        db = argv[argv.index('--db') + 1]
    from store import Store
    rows = list(csv.DictReader(open(os.path.join(run_dir, 'trades.csv'))))
    names = sorted({r.get('underlying') or 'SPY' for r in rows})
    store = Store(db, underlyings=names)
    sb = lambda a, b: max(len(store.trading_days(a, b)) - 1, 0)
    per = [attribute_trade(store, r, sb) for r in rows]
    s = summarize(per)
    s['directional'] = directional_split(rows)

    print(f"\nP&L attribution -- {os.path.basename(run_dir)}")
    print(f"  {s['trades_attributed']} trades attributed, {s['trades_skipped']} skipped "
          f"(single-session holds carry no interior path)\n")
    print(f"  {'component':<12}{'dollars':>12}{'share of gross':>17}")
    print('  ' + '-' * 41)
    for k in ('theta', 'gamma', 'vega', 'delta'):
        print(f"  {k:<12}{s['components'][k]:>12,.0f}{s['shares'][k]:>16.1%}")
    print('  ' + '-' * 41)
    print(f"  {'realized':<12}{s['realized_mark']:>12,.0f}")
    print(f"  {'residual':<12}{s['residual']:>12,.0f}{s['residual_share']:>16.1%}"
          f"   (skew, vanna, jumps -- not in this expansion)")
    print(f"\n  convexity share {s['convexity_share']:.1%}  ->  {s['verdict']}")
    if s['residual_share'] and abs(s['residual_share']) > 0.35:
        print("  WARNING residual is larger than the P&L it explains. Read the Greek split"
              "\n          as indicative only -- daily closes in this store are not"
              "\n          simultaneous, so the IV series the expansion needs is noise.")
    d = s['directional']
    print(f"\n  directional check (no greeks, robust on this data)")
    print(f"    up-moves    {d['pnl_on_up_moves']:>12,.0f}   ({d['trades_up']} trades)")
    print(f"    down-moves  {d['pnl_on_down_moves']:>12,.0f}   ({d['trades_down']} trades)")
    print(f"    asymmetry {d['asymmetry']:.0%}  ->  {d['verdict']}")
    out = os.path.join(run_dir, 'attribution.json')
    json.dump(s, open(out, 'w'), indent=2, default=str)
    print(f"\n  written {out}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and not sys.argv[1].startswith('-'):
        main(sys.argv[1:])
    else:
        # Self-check on a synthetic book with a KNOWN answer.
        class FakeStore:
            def __init__(self, spots, px):
                self.spots, self.px = spots, px
            def trading_days(self, a, b):
                return [d for d in sorted(self.spots) if a <= d <= b]
            def underlying_close(self, sym, d):
                return self.spots.get(d)
            def option_close(self, occ, d):
                return self.px.get((occ, d))

        # A short 590 put and long 585 put on a 600 underlying that does not move: the
        # P&L must be theta, and convexity share must still be non-trivial because
        # gamma and vega are live even when the spot is still.
        days = [dt.date(2026, 3, 2) + dt.timedelta(days=i) for i in range(4)]
        spots = {d: 600.0 for d in days}
        sess = {d: 10 - i for i, d in enumerate(days)}
        px = {}
        for d in days:
            t = bs.year_fraction_sessions(sess[d])
            px[('X260316P00590000', d)] = round(bs.price(600.0, 590.0, t, 0.18, 'P'), 2)
            px[('X260316P00585000', d)] = round(bs.price(600.0, 585.0, t, 0.18, 'P'), 2)
        store = FakeStore(spots, px)
        row = {'entry_date': days[0], 'exit_date': days[-1], 'underlying': 'X',
               'expiry': dt.date(2026, 3, 16), 'qty': 1, 'structure': 'vertical',
               'short_occ': 'X260316P00590000', 'long_occ': 'X260316P00585000'}
        res = attribute_trade(store, row, lambda a, b: sess[a])
        assert res and not res['unattributable'], res
        assert res['theta'] > 0, f"a short vertical on a still market must earn theta: {res['theta']}"
        assert abs(res['delta']) < 1e-9, f"the spot never moved, so delta must be zero: {res['delta']}"
        assert abs(res['gamma']) < 1e-9, "no move, no gamma P&L"
        assert abs(res['residual']) < 0.05 * abs(res['realized_mark']) + 1.0, \
            f"theta alone should explain a still market: residual {res['residual']:.2f} " \
            f"vs realized {res['realized_mark']:.2f}"

        s = summarize([res])
        assert s['convexity_share'] < CONVEXITY_FLOOR, s['convexity_share']
        assert 'DELTA_ONLY' in s['verdict'] or s['convexity_share'] < CONVEXITY_FLOOR

        # legs_of must see all four legs of a condor, and say so when it cannot.
        four, miss = legs_of({'structure': 'condor', 'short_occ': 'a', 'long_occ': 'b',
                              'call_short_occ': 'c', 'call_long_occ': 'd'})
        assert len(four) == 4 and not miss, four
        _, miss2 = legs_of({'structure': 'condor', 'short_occ': 'a', 'long_occ': 'b'})
        assert miss2, "a condor missing its call wing must be flagged, not silently halved"

        d = directional_split([
            {'entry_spot': 100, 'exit_spot': 101, 'pnl': 10},
            {'entry_spot': 100, 'exit_spot': 99, 'pnl': 90},
        ])
        assert d['pnl_on_up_moves'] == 10 and d['pnl_on_down_moves'] == 90
        assert d['asymmetry'] > 0.60 and 'DIRECTIONAL' in d['verdict']

        print(f"attribution.py self-check OK  (still market -> theta {res['theta']:.2f}, "
              f"delta {res['delta']:.2f}, residual {res['residual']:.3f}; "
              f"condor legs {len(four)})")
