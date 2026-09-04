"""Realized-vol estimators, HAR forecast, path/gap statistics.

All estimators take arrays of daily OHLC (oldest first) and return ANNUALISED vol (decimal).
"""
from __future__ import annotations
import numpy as np

ANN = 252.0


def _ln(a, b):
    return np.log(np.asarray(a, float) / np.asarray(b, float))


def sanitize_bars(o, h, l, c, max_range: float = 0.35) -> tuple:
    """Repair impossible highs/lows (IEX bars contain bad prints, e.g. SPY 2026-02-02 low=69.005 vs close 695).
    A high above (1+max_range)*max(o,c) or a low below (1-max_range)*min(o,c) is replaced by max(o,c)/min(o,c).
    Returns (o, h, l, c, n_repaired).  Repairs are logged upstream — never silent in the ledger."""
    o, h, l, c = (np.asarray(x, float).copy() for x in (o, h, l, c))
    hi, lo = np.maximum(o, c), np.minimum(o, c)
    bad_h = h > hi * (1 + max_range)
    bad_l = l < lo * (1 - max_range)
    h[bad_h] = hi[bad_h]; l[bad_l] = lo[bad_l]
    h = np.maximum(h, hi); l = np.minimum(l, lo)
    return o, h, l, c, int(bad_h.sum() + bad_l.sum())


def garman_klass_daily(o, h, l, c):
    """Per-day GK variance (not annualised)."""
    hl = _ln(h, l) ** 2
    co = _ln(c, o) ** 2
    return 0.5 * hl - (2 * np.log(2) - 1) * co


def yang_zhang(o, h, l, c, window: int = 20) -> float:
    """Yang-Zhang annualised vol over the last `window` days (needs window+1 rows)."""
    o, h, l, c = map(lambda x: np.asarray(x, float), (o, h, l, c))
    if len(c) < window + 1:
        return float("nan")
    o, h, l, c, pc = o[-window:], h[-window:], l[-window:], c[-window:], c[-window - 1:-1]
    on = _ln(o, pc)
    oc = _ln(c, o)
    rs = _ln(h, c) * _ln(h, o) + _ln(l, c) * _ln(l, o)
    n = window
    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    v = on.var(ddof=1) + k * oc.var(ddof=1) + (1 - k) * rs.mean()
    return float(np.sqrt(max(v, 0) * ANN))


def close_to_close(c, window: int = 20) -> float:
    c = np.asarray(c, float)
    if len(c) < window + 1:
        return float("nan")
    r = np.diff(np.log(c[-window - 1:]))
    return float(r.std(ddof=1) * np.sqrt(ANN))


def har_forecast(o, h, l, c, horizon_days: int, refit_days: int = 500, windows=(1, 5, 22),
                 purge_embargo: int = 5) -> dict:
    """HAR-RV on daily GK variance nodes; forecasts mean daily variance over the next `horizon_days`.

    y_t = mean(GK_{t+1..t+h});  x_t = [GK_t, mean(GK_{t-4..t}), mean(GK_{t-21..t})]
    Fit by OLS on the last `refit_days` rows with a purge/embargo gap of `purge_embargo` sessions
    between the training window and the forecast origin (no overlap of labels with the origin).
    Returns annualised vol forecast + persistence baseline + in-sample R2 (diagnostic only).
    """
    gk = garman_klass_daily(o, h, l, c)
    gk = np.clip(gk, 1e-10, None)
    n = len(gk)
    W = max(windows)
    h = int(horizon_days)
    if n < W + h + 60:
        return {"rv_f": float("nan"), "rv_persist": float("nan"), "r2_is": float("nan"), "n_fit": 0}
    feats, ys = [], []
    last_origin = n - 1 - h - purge_embargo
    first_origin = max(W - 1, last_origin - refit_days)
    for t in range(first_origin, last_origin + 1):
        x = [gk[t]] + [gk[t - w + 1:t + 1].mean() for w in windows[1:]]
        feats.append(x)
        ys.append(gk[t + 1:t + 1 + h].mean())
    X = np.column_stack([np.ones(len(feats)), np.array(feats)])
    y = np.array(ys)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    yhat = X @ beta
    r2 = 1 - ((y - yhat) ** 2).sum() / max(((y - y.mean()) ** 2).sum(), 1e-18)
    t = n - 1
    x_now = np.array([1.0, gk[t]] + [gk[t - w + 1:t + 1].mean() for w in windows[1:]])
    var_f = float(max(x_now @ beta, 1e-10))
    return {"rv_f": float(np.sqrt(var_f * ANN)),
            "rv_persist": float(np.sqrt(gk[-22:].mean() * ANN)),
            "r2_is": float(r2), "n_fit": len(y), "beta": beta.tolist()}


def max_adverse_excursion(h, l, c, lookback: int = 63, horizon: int = 10) -> dict:
    """Largest fractional move against a hypothetical short strike within any `horizon`-day window
    over the last `lookback` days: max over windows of (max(high)/entry_close - 1) and (1 - min(low)/entry_close).
    """
    h, l, c = map(lambda x: np.asarray(x, float), (h, l, c))
    n = len(c)
    ups, dns = [], []
    start = max(1, n - lookback - horizon)
    for i in range(start, n - 1):
        j = min(n, i + 1 + horizon)
        ups.append(h[i + 1:j].max() / c[i] - 1)
        dns.append(1 - l[i + 1:j].min() / c[i])
    ups, dns = np.array(ups), np.array(dns)
    return {"mae_up": float(ups.max()), "mae_dn": float(dns.max()), "mae": float(max(ups.max(), dns.max())),
            "mae_up_p50": float(np.median(ups)), "mae_dn_p50": float(np.median(dns)),
            "mae_up_p90": float(np.quantile(ups, 0.9)), "mae_dn_p90": float(np.quantile(dns, 0.9))}


def gap_stats(o, c, lookback: int = 252) -> dict:
    o, c = np.asarray(o, float), np.asarray(c, float)
    g = np.abs(o[1:] / c[:-1] - 1)[-lookback:]
    return {"gap_max": float(g.max()), "gap_p99": float(np.quantile(g, 0.99)), "gap_mean": float(g.mean())}


def efficiency_ratio(c, window: int = 20) -> float:
    c = np.asarray(c, float)[-window - 1:]
    if len(c) < window + 1:
        return float("nan")
    net = abs(c[-1] - c[0])
    path = np.abs(np.diff(c)).sum()
    return float(net / path) if path > 0 else 0.0


def dollar_adv(c, v, window: int = 20) -> float:
    c, v = np.asarray(c, float)[-window:], np.asarray(v, float)[-window:]
    return float((c * v).mean())


def percentile_rank(series, value) -> float:
    s = np.asarray(series, float)
    s = s[~np.isnan(s)]
    return float((s < value).mean()) if len(s) else float("nan")


def beta_corr(rets_x, rets_m) -> tuple[float, float]:
    x, m = np.asarray(rets_x, float), np.asarray(rets_m, float)
    n = min(len(x), len(m))
    x, m = x[-n:], m[-n:]
    if n < 30 or m.std() == 0:
        return float("nan"), float("nan")
    cov = np.cov(x, m)
    return float(cov[0, 1] / cov[1, 1]), float(np.corrcoef(x, m)[0, 1])
