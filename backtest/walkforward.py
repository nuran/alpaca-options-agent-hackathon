"""
Is the edge stable in time, or is the pooled number one regime?

A pooled t-statistic assumes independent trades. These are not independent: trades
opened in the same week share a regime, and trades on six correlated underlyings share
a market. The pooled t = 2.36 on the put-vertical book is therefore an upper bound on
the evidence, not the evidence.

Three things here, none of which needs new data:

  1. ROLLING WINDOWS. Mean return-on-risk per calendar block. An edge that is real
     shows up in most blocks; an edge that is one regime shows up in one. The by-year
     split already hinted at the second: t was 0.33 in 2024, 1.65 in 2025, 1.98 in 2026.

  2. BLOCK BOOTSTRAP. Resample contiguous blocks of calendar time with replacement and
     recompute the mean. Blocks preserve the dependence that trade-level resampling
     destroys, so the resulting interval is honest about serial and cross-sectional
     correlation. This is the number to quote.

  3. DEFLATED SHARPE. The week's search space was large -- time basis, wing width, seven
     DTE buckets, eight stop multipliers, five deltas, four friction models, three
     structures, six names. Picking the best cell of that and reporting its t is the
     definition of an inflated result. DSR discounts the observed Sharpe by how many
     trials it was chosen from and how much they varied.

Usage:
    python3 backtest/walkforward.py <label-glob> [--block 20] [--boot 5000]
    python3 backtest/walkforward.py 'pv-*,vv-put'
"""
from __future__ import annotations

import csv
import datetime as dt
import glob
import json
import math
import os
import random
import statistics as st
import sys
from collections import defaultdict

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))


def load_trades(labels):
    """Every trade of every run whose label matches, as (entry_date, return_on_risk)."""
    out = []
    for lab in labels:
        for p in glob.glob(os.path.join(REPO, 'runs', f'*_{lab}_1Day', 'trades.csv')):
            for x in csv.DictReader(open(p)):
                try:
                    ml = float(x.get('max_loss') or 0)
                    if ml <= 0:
                        continue
                    out.append((dt.date.fromisoformat(x['entry_date'][:10]),
                                float(x['pnl']) / ml))
                except (TypeError, ValueError, KeyError):
                    continue
    out.sort()
    return out


def rolling(trades, months=4):
    """Mean return-on-risk per calendar block, in order."""
    if not trades:
        return []
    buckets = defaultdict(list)
    for d, r in trades:
        key = (d.year, (d.month - 1) // months)
        buckets[key].append(r)
    out = []
    for key in sorted(buckets):
        v = buckets[key]
        if len(v) < 15:
            continue
        m, s = st.mean(v), (st.stdev(v) if len(v) > 1 else 0.0)
        t = m / (s / math.sqrt(len(v))) if s else float('nan')
        out.append({'block': f"{key[0]}-{key[1] * months + 1:02d}", 'n': len(v),
                    'mean': m, 'sd': s, 't': t,
                    'win': sum(1 for x in v if x > 0) / len(v)})
    return out


def block_bootstrap(trades, block_days=20, draws=5000, seed=7):
    """
    Circular block bootstrap over CALENDAR time.

    Resampling individual trades would treat two trades opened the same morning on SPY
    and QQQ as independent evidence. They are not. Resampling contiguous blocks of days
    keeps whatever dependence exists inside a block intact.
    """
    if not trades:
        return None
    days = sorted({d for d, _ in trades})
    by_day = defaultdict(list)
    for d, r in trades:
        by_day[d].append(r)
    n_days = len(days)
    n_blocks = max(1, n_days // block_days)
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        sample = []
        for _ in range(n_blocks):
            start = rng.randrange(n_days)
            for k in range(block_days):
                sample.extend(by_day[days[(start + k) % n_days]])
        if sample:
            means.append(st.mean(sample))
    means.sort()
    lo = means[int(0.025 * len(means))]
    hi = means[int(0.975 * len(means))]
    point = st.mean([r for _, r in trades])
    return {'point': point, 'lo': lo, 'hi': hi,
            'p_le_zero': sum(1 for m in means if m <= 0) / len(means),
            'block_days': block_days, 'draws': draws}


def deflated_sharpe(observed_sr, trials, sr_variance, n_obs, skew=0.0, kurt=3.0):
    """
    Bailey & Lopez de Prado: the probability the true Sharpe is above zero, given that
    this one was selected as the best of `trials` correlated attempts.

    `sr_variance` is the variance of the Sharpe ratios across those attempts. Without it
    the deflation is a guess; with it, the expected maximum of `trials` draws is what the
    observed Sharpe has to beat.
    """
    if trials < 2 or sr_variance <= 0 or n_obs < 3:
        return None
    e = 0.5772156649
    z = ((1 - e) * _norm_ppf(1 - 1.0 / trials)
         + e * _norm_ppf(1 - 1.0 / (trials * math.e)))
    sr0 = math.sqrt(sr_variance) * z                      # expected max under the null
    denom = math.sqrt(1 - skew * observed_sr + (kurt - 1) / 4.0 * observed_sr ** 2)
    if denom <= 0:
        return None
    return _norm_cdf((observed_sr - sr0) * math.sqrt(n_obs - 1) / denom), sr0


def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_ppf(p):
    """Acklam's rational approximation; plenty for a deflation threshold."""
    if not 0 < p < 1:
        raise ValueError(p)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    pl, ph = 0.02425, 1 - 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > ph:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def trial_sharpes(pattern='runs/2026-08-*'):
    """Per-trade Sharpe of every run in the folder -- the search space, measured."""
    out = []
    for d in glob.glob(os.path.join(REPO, pattern)):
        p = os.path.join(d, 'trades.csv')
        if not os.path.exists(p):
            continue
        v = []
        for x in csv.DictReader(open(p)):
            try:
                ml = float(x.get('max_loss') or 0)
                if ml > 0:
                    v.append(float(x['pnl']) / ml)
            except (TypeError, ValueError, KeyError):
                continue
        if len(v) >= 25 and st.stdev(v) > 0:
            out.append(st.mean(v) / st.stdev(v))
    return out


def main(argv):
    labels = [s.strip() for s in argv[0].split(',')] if argv else ['pv-*', 'vv-put']
    block = int(argv[argv.index('--block') + 1]) if '--block' in argv else 20
    draws = int(argv[argv.index('--boot') + 1]) if '--boot' in argv else 5000

    tr = load_trades(labels)
    if not tr:
        raise SystemExit(f"no trades matched {labels}")
    m, s, n = st.mean(r for _, r in tr), st.stdev([r for _, r in tr]), len(tr)
    print(f"\nWalk-forward: {n} trades, {tr[0][0]} -> {tr[-1][0]}")
    print(f"  pooled mean RoR {m:+.2%}  sd {s:.1%}  naive t {m/(s/math.sqrt(n)):.2f}\n")

    print(f"  {'block':<10}{'n':>5}{'win':>8}{'mean RoR':>11}{'t':>7}")
    print('  ' + '-' * 42)
    blocks = rolling(tr)
    for b in blocks:
        print(f"  {b['block']:<10}{b['n']:>5}{b['win']:>8.0%}{b['mean']:>10.2%}{b['t']:>7.2f}")
    pos = sum(1 for b in blocks if b['mean'] > 0)
    print('  ' + '-' * 42)
    print(f"  positive blocks: {pos}/{len(blocks)}")

    bs = block_bootstrap(tr, block, draws)
    print(f"\n  Block bootstrap ({bs['block_days']}-session blocks, {bs['draws']:,} draws)")
    print(f"    mean RoR {bs['point']:+.2%}   95% CI {bs['lo']:+.2%} … {bs['hi']:+.2%}"
          f"   P(mean <= 0) = {bs['p_le_zero']:.1%}")

    srs = trial_sharpes()
    if len(srs) >= 3:
        sr = m / s
        d = deflated_sharpe(sr, len(srs), st.variance(srs), n)
        print(f"\n  Deflated Sharpe over {len(srs)} runs in runs/ (the week's search space)")
        if d:
            dsr, sr0 = d
            print(f"    observed per-trade SR {sr:.4f}   expected max under the null {sr0:.4f}")
            print(f"    DSR = {dsr:.3f}   {'PASSES 0.95' if dsr >= 0.95 else 'fails the 0.95 bar'}")
    out = {'n': n, 'mean': m, 'sd': s, 'blocks': blocks, 'bootstrap': bs,
           'trials': len(srs)}
    dest = os.path.join(REPO, 'runs', 'walkforward.json')
    json.dump(out, open(dest, 'w'), indent=2, default=str)
    print(f"\n  written {os.path.relpath(dest, REPO)}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(sys.argv[1:])
    else:
        # Self-check: a known-answer series must come back with the right verdict.
        base = dt.date(2024, 1, 2)
        rng = random.Random(1)
        pure_noise = [(base + dt.timedelta(days=i), rng.gauss(0, 0.2)) for i in range(400)]
        bs = block_bootstrap(pure_noise, 20, 800)
        assert bs['lo'] < 0 < bs['hi'], f"noise must not be called an edge: {bs}"
        real = [(base + dt.timedelta(days=i), rng.gauss(0.05, 0.05)) for i in range(400)]
        bs2 = block_bootstrap(real, 20, 800)
        assert bs2['lo'] > 0, f"a large real effect must survive the bootstrap: {bs2}"
        assert bs2['p_le_zero'] < 0.05
        one_regime = ([(base + dt.timedelta(days=i), rng.gauss(0.0, 0.05)) for i in range(300)] +
                      [(base + dt.timedelta(days=300 + i), rng.gauss(0.30, 0.05)) for i in range(100)])
        blocks = rolling(one_regime, months=4)
        pos = sum(1 for b in blocks if b['mean'] > 0)
        assert pos < len(blocks), "a one-regime effect must show up as mixed blocks"
        d = deflated_sharpe(0.09, 50, 0.004, 678)
        assert d and 0.0 <= d[0] <= 1.0
        assert deflated_sharpe(0.09, 1, 0.004, 678) is None
        print(f"walkforward.py self-check OK  (noise CI straddles zero, strong effect "
              f"survives, one-regime effect shows mixed blocks, DSR in range)")
