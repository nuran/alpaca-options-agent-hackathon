"""
Evidence statistics the 2026 audits ask for and this repo did not yet compute.

Three things, all on data the repo already produces (`runs/*/equity.csv`, `round_trips.csv`):

1. **Deflated Sharpe Ratio** (Bailey & Lopez de Prado). A Sharpe of 0.84 measured over ~640
   sessions, chosen from a family of tried variants, is not the same evidence as a Sharpe of
   0.84 measured once. DSR is the probability that the true Sharpe exceeds zero after
   correcting for the number of trials, the length of the sample, and the skew and kurtosis
   of the return series. "The Alpha Illusion" (arXiv 2605.16895) lists multiple-testing
   inflation as one of the five failure modes; this is the standard correction for it.

2. **Probability of Backtest Overfitting** via Combinatorially Symmetric Cross-Validation
   (Bailey, Borwein, Lopez de Prado, Zhu). Split the daily P&L matrix of all tried variants
   into S blocks, take every half as "in sample", pick the best variant there, and look at
   where it ranks out of sample. PBO is how often the in-sample winner lands in the bottom
   half out of sample. This is the direct measure of "did we pick the FOMC blackout because
   it works, or because we looked at fourteen variants".

3. **Rolling-window net-of-cost reporting** (Nguyen & Pham's fifth criterion). One headline
   number hides regime dependence; a rolling table shows it.

Nothing here changes a trading decision. It changes what we are allowed to claim.

Self-check: `python3 backtest/stats.py`
"""
import math
import statistics as st


# ---------------------------------------------------------------- helpers

def _moments(returns):
    n = len(returns)
    mu = sum(returns) / n
    sd = st.pstdev(returns) or 1e-12
    z = [(r - mu) / sd for r in returns]
    skew = sum(x ** 3 for x in z) / n
    kurt = sum(x ** 4 for x in z) / n          # non-excess
    return mu, sd, skew, kurt


def _phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def sharpe(returns, periods=252):
    if len(returns) < 2:
        return float('nan')
    mu, sd, *_ = _moments(returns)
    return mu / sd * math.sqrt(periods)


# ---------------------------------------------------------------- 1. DSR

def expected_max_sharpe(n_trials, variance_of_trial_sharpes):
    """
    E[max SR] over `n_trials` independent trials with the given cross-trial variance --
    the benchmark a selected strategy has to beat (Bailey & Lopez de Prado, eq. for SR*).
    """
    if n_trials <= 1:
        return 0.0
    e = 0.5772156649015329                      # Euler-Mascheroni
    s = math.sqrt(max(variance_of_trial_sharpes, 1e-18))
    q1 = _z(1 - 1.0 / n_trials)
    q2 = _z(1 - 1.0 / (n_trials * math.e))
    return s * ((1 - e) * q1 + e * q2)


def _z(p):
    """Inverse standard normal (Acklam's rational approximation, ~1e-9 accurate)."""
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


def deflated_sharpe(returns, n_trials, trial_sharpes=None, periods=252):
    """
    Probability that the true Sharpe is positive, given that this variant was SELECTED from
    `n_trials`. `trial_sharpes` (annualised Sharpes of every variant tried) sets the
    selection benchmark; without it a conservative default variance of 1.0 is assumed.

    Returns a dict with the observed Sharpe, the benchmark SR*, and DSR in [0, 1].
    A DSR below ~0.95 means the result is not distinguishable from the best of N noise draws.
    """
    n = len(returns)
    if n < 30:
        return {'sharpe': float('nan'), 'sr_star': float('nan'), 'dsr': float('nan'), 'n': n}
    mu, sd, skew, kurt = _moments(returns)
    sr = mu / sd                                     # per period, not annualised
    var_trials = (st.pvariance([s / math.sqrt(periods) for s in trial_sharpes])
                  if trial_sharpes and len(trial_sharpes) > 1 else 1.0 / periods)
    sr_star = expected_max_sharpe(n_trials, var_trials)
    denom = math.sqrt(max(1e-18, 1 - skew * sr + (kurt - 1) / 4 * sr ** 2))
    dsr = _phi(((sr - sr_star) * math.sqrt(n - 1)) / denom)
    return {'sharpe': sr * math.sqrt(periods), 'sr_star': sr_star * math.sqrt(periods),
            'dsr': dsr, 'n': n, 'skew': skew, 'kurtosis': kurt, 'n_trials': n_trials}


# ---------------------------------------------------------------- 2. PBO / CSCV

def _combinations(items, k):
    if k == 0:
        yield ()
        return
    for i in range(len(items) - k + 1):
        for rest in _combinations(items[i + 1:], k - 1):
            yield (items[i],) + rest


def pbo_cscv(matrix, s=8, periods=252):
    """
    Probability of Backtest Overfitting by Combinatorially Symmetric Cross-Validation.

    `matrix`: {variant_name: [per-period returns]} -- all variants on the SAME periods.
    The series are split into `s` contiguous blocks; for each of the C(s, s/2) ways to pick
    half the blocks as in-sample, the best in-sample variant is found and its out-of-sample
    rank is recorded. PBO is the share of splits where the in-sample winner ranks in the
    bottom half out of sample.
    """
    names = sorted(matrix)
    T = min(len(matrix[n]) for n in names)
    if T < s * 4 or len(names) < 2:
        return {'pbo': float('nan'), 'splits': 0, 'variants': len(names), 'periods': T}
    blocks = [list(range(i * T // s, (i + 1) * T // s)) for i in range(s)]
    logits, worse = [], 0
    splits = list(_combinations(list(range(s)), s // 2))
    for ins in splits:
        oos = [b for b in range(s) if b not in ins]
        idx_in = [i for b in ins for i in blocks[b]]
        idx_out = [i for b in oos for i in blocks[b]]
        sr_in = {n: sharpe([matrix[n][i] for i in idx_in], periods) for n in names}
        sr_out = {n: sharpe([matrix[n][i] for i in idx_out], periods) for n in names}
        best = max(names, key=lambda n: sr_in[n])
        ranked = sorted(names, key=lambda n: sr_out[n])
        rank = ranked.index(best) + 1                      # 1 = worst OOS
        w = rank / (len(names) + 1)
        w = min(max(w, 1e-6), 1 - 1e-6)
        logits.append(math.log(w / (1 - w)))
        if rank <= len(names) / 2:
            worse += 1
    return {'pbo': worse / len(splits), 'splits': len(splits), 'variants': len(names),
            'periods': T, 'median_logit': st.median(logits)}


# ---------------------------------------------------------------- 3. rolling windows

def rolling_windows(dates, returns, window=63, step=21, periods=252):
    """Non-overlapping-ish rolling net-of-cost windows: (start, end, return, sharpe)."""
    out = []
    for i in range(0, max(0, len(returns) - window + 1), step):
        seg = returns[i:i + window]
        total = 1.0
        for r in seg:
            total *= (1 + r)
        out.append({'start': dates[i], 'end': dates[i + window - 1],
                    'return': total - 1, 'sharpe': sharpe(seg, periods)})
    return out


# ---------------------------------------------------------------- io helpers

def equity_to_returns(path, basis=None):
    """
    Read a run's equity.csv -> (dates, simple daily returns).

    Prefers `equity_mtm` when the column exists. The `equity` column is realised CASH,
    which for a marked run is a different and flattering series: on the 16-name book it
    reports Sharpe 1.97 where the marked curve reports 1.36, because a drawdown that has
    not been closed yet does not appear in cash. Pass basis='cash' to force the old
    column deliberately.
    """
    import csv
    rows = list(csv.DictReader(open(path)))
    dates = [r['date'] for r in rows][1:]
    col = 'equity'
    if basis != 'cash' and rows and rows[0].get('equity_mtm') not in (None, ''):
        col = 'equity_mtm'
    eq = [float(r[col]) for r in rows]
    rets = [(eq[i] / eq[i - 1] - 1.0) if eq[i - 1] else 0.0 for i in range(1, len(eq))]
    return dates, rets


if __name__ == "__main__":
    import random
    rng = random.Random(11)
    # A genuinely positive series must score a high DSR when it was the only trial...
    good = [rng.gauss(0.0008, 0.004) for _ in range(600)]
    d1 = deflated_sharpe(good, n_trials=1)
    assert d1['dsr'] > 0.9, d1
    # ...and the same series must be deflated once it is the best of many trials.
    d20 = deflated_sharpe(good, n_trials=20, trial_sharpes=[sharpe(good)] + [rng.gauss(0, 0.6) for _ in range(19)])
    assert d20['dsr'] < d1['dsr'] and d20['sr_star'] > 0, d20
    # PBO on pure noise must be high: with complementary splits the in-sample winner is the
    # variant that got lucky in those blocks, so it lands in the bottom half out of sample
    # most of the time. A variant with a real edge must survive.
    noise = {f'v{i}': [rng.gauss(0, 0.004) for _ in range(600)] for i in range(8)}
    p_noise = pbo_cscv(noise, s=8)
    assert p_noise['pbo'] >= 0.5, p_noise
    real = dict(noise); real['edge'] = [rng.gauss(0.0012, 0.004) for _ in range(600)]
    p_real = pbo_cscv(real, s=8)
    assert p_real['pbo'] < p_noise['pbo'], (p_real, p_noise)
    w = rolling_windows([str(i) for i in range(600)], good, window=63, step=21)
    assert len(w) > 5 and all('sharpe' in x for x in w)
    print(f"stats.py self-check OK  (single-trial DSR {d1['dsr']:.3f}, 20-trial DSR {d20['dsr']:.3f}, "
          f"noise PBO {p_noise['pbo']:.2f}, with-edge PBO {p_real['pbo']:.2f})")
