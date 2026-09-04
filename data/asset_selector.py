"""
Deterministic five-stage asset funnel for US equity/ETF options, DTE ≤ 12.

Stages 0–3 drop names for reasons that kill a trade regardless of edge.
Stage 4 ranks survivors on ≥2y daily OHLCV. Stage 5 (trade-time) is IV-blind
until a live chain exists — default outcome is NO_TRADE.

    Stage 0  Venue & style          european / not tradable on Alpaca
    Stage 1  Expiry density         monthly-only names cannot express DTE≤12
    Stage 2  Executability          penny program, strike density, OI floor
    Stage 3  Factor de-duplication  one slot per cluster; leveraged / crypto tagged
    Stage 4  OHLCV scoring          ShortVolScore / LongConvexScore → top-3 / branch
    Stage 5  IV gate                HAR-matched IV, VIX regime, MAE, spreads

Census source: `option_underlyings` in the data-profile DuckDB when present
(`make data-profile`). Without it the selector live-probes a liquid seed and
tags `census_source=live_probe` — OI/expiry counts are then floors, same as
the capped crawl, and must not be treated as a full census.

Usage:
    python3 data/asset_selector.py [--db PATH] [--out data/universe_ranked.json]
                                   [--years 2] [--stage5] [--top N]
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import sys

import numpy as np
import requests

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, 'alpaca'))

from alpaca.env import load_env_var
from alpaca._common import auth_headers, PAPER_HOST

ANN = 252.0
LN2 = math.log(2.0)
OI_CORE = 1_000_000
OI_EXTENDED = 300_000
MIN_STRIKES = 40
DAILIES = ('13+', '7-12')          # census labels: 13+ dailies, 7-12 rich weeklies
WEEKLIES = ('4-6 — weeklies', '4-6')
KEEP_TIERS = {
    '13+', '7-12', '4-6 — weeklies', '4-6',
    'dailies', 'weeklies',              # live-probe buckets
}

# Index exposure on this venue is American physically-settled ETFs. VIX options
# do not exist here; VIX is a regime input. VXX is the only vol instrument.
SEED = [
    # CORE factors (2026-08 census result)
    'SPY', 'QQQ', 'IWM', 'GLD', 'XLF', 'SMH',
    # other index / sector ETFs
    'DIA', 'MDY', 'QQQM', 'IJR', 'IVV', 'VOO',
    'XLK', 'XLE', 'XLV', 'XLI', 'XLY', 'XLP', 'XLU', 'XLB', 'XLRE', 'XLC',
    'XBI', 'XRT', 'KRE', 'KBE', 'SOXX', 'IGV', 'ITB', 'IYR', 'GDX', 'SLV',
    'TLT', 'HYG', 'LQD', 'EEM', 'EFA', 'USO', 'UNG',
    # vol ETP — VXX only (path-dependent; never a hedge warehouse)
    'VXX',
    # liquid names (event-mode by default)
    'AAPL', 'MSFT', 'NVDA', 'AMZN', 'META', 'GOOGL', 'TSLA', 'AMD', 'AVGO',
    'NFLX', 'JPM', 'BAC', 'XOM', 'UNH', 'COST', 'WMT',
]
CRYPTO = {
    'IBIT', 'ETHA', 'MSTR', 'COIN', 'MARA', 'RIOT', 'CLSK', 'HUT', 'BITF',
    'IREN', 'BTDR', 'BITO', 'GBTC', 'ARKB', 'FBTC', 'BITB', 'HODL', 'ETHE',
}
VOL_ETP = {'VXX', 'UVXY', 'SVXY', 'SVIX', 'VIXY', 'VIXM', 'VXZ', 'VIX'}
LEVERAGED = re.compile(
    r'^(TQQQ|SQQQ|SOXL|SOXS|UPRO|SPXU|TNA|TZA|UDOW|SDOW|QLD|QID|SPXL|SPXS|'
    r'TMF|TMV|UCO|SCO|LABU|LABD|NAIL|CURE|DFEN|FAS|FAZ|NUGT|DUST|JNUG|JDST)$'
)
ETF_HINTS = ('ETF', 'TRUST', 'FUND', 'ISHARES', 'SPDR', 'VANGUARD', 'INVESCO',
             'PROSHARES', 'DIREXION', 'SELECT SECTOR', 'ETN', 'WISDOMTREE')
CLUSTER = {
    'SPY': 'broad', 'IVV': 'broad', 'VOO': 'broad', 'DIA': 'broad', 'SPLG': 'broad',
    'QQQ': 'nasdaq', 'QQQM': 'nasdaq',
    'IWM': 'small', 'IJR': 'small', 'MDY': 'small',
    'GLD': 'gold', 'IAU': 'gold', 'GLDM': 'gold', 'SGOL': 'gold',
    'XLF': 'financials', 'VFH': 'financials', 'IYF': 'financials',
    'KRE': 'regional_banks', 'KBE': 'regional_banks',
    'SMH': 'semis', 'SOXX': 'semis',
    'XLK': 'tech', 'VGT': 'tech',
    'XLE': 'energy', 'XOM': 'energy',
    'XLV': 'health', 'XBI': 'biotech',
    'TLT': 'rates', 'HYG': 'credit', 'LQD': 'credit',
    'SLV': 'silver', 'GDX': 'miners',
    'USO': 'oil', 'UNG': 'gas',
    'EEM': 'em', 'EFA': 'intl',
    'VXX': 'vol', 'UVXY': 'vol', 'SVXY': 'vol', 'VIXY': 'vol',
}

CBOE = 'https://cdn.cboe.com/api/global/us_indices/daily_prices/{}_History.csv'


# ------------------------------------------------------------------ helpers

def _cli():
    import shutil
    for p in (os.environ.get('ALPACA_CLI'), '/opt/homebrew/bin/alpaca',
              '/usr/local/bin/alpaca', shutil.which('alpaca')):
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    raise SystemExit('Alpaca CLI not found')


def _cli_json(args, allow_fail=True, timeout=180):
    import subprocess
    env = dict(os.environ)
    env['ALPACA_API_KEY'] = load_env_var('ALPACA_API_KEY')
    env['ALPACA_SECRET_KEY'] = load_env_var('ALPACA_SECRET_KEY')
    env['ALPACA_QUIET'] = '1'
    proc = subprocess.run([_cli(), *args, '--quiet'],
                          capture_output=True, text=True, env=env, timeout=timeout)
    if proc.returncode != 0 or not (proc.stdout or '').strip():
        if allow_fail:
            return None
        raise SystemExit(proc.stderr or proc.stdout)
    try:
        body = json.loads(proc.stdout)
    except json.JSONDecodeError:
        if allow_fail:
            return None
        raise SystemExit(proc.stdout[:400])
    if isinstance(body, dict) and body.get('error'):
        if allow_fail:
            return None
        raise SystemExit(body['error'])
    return body


def _zrank(values):
    """Average percentile rank in [0, 1]. Ties share a rank."""
    a = np.asarray(values, float)
    n = len(a)
    if n <= 1:
        return np.zeros(n) if n else a
    order = a.argsort(kind='mergesort')
    ranks = np.empty(n, float)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and a[order[j + 1]] == a[order[i]]:
            j += 1
        mid = 0.5 * (i + j)
        ranks[order[i:j + 1]] = mid / (n - 1)
        i = j + 1
    return ranks


def _nan(x):
    return x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x)))


# ------------------------------------------------------------------ Stage 3 tags

def is_etf(name, symbol):
    if symbol in VOL_ETP or symbol in CLUSTER:
        return symbol not in {'AAPL', 'MSFT', 'NVDA', 'AMZN', 'META', 'GOOGL', 'TSLA',
                              'AMD', 'AVGO', 'NFLX', 'JPM', 'BAC', 'XOM', 'UNH', 'COST', 'WMT'}
    n = (name or '').upper()
    return any(h in n for h in ETF_HINTS)


def factor_tag(symbol, name):
    if symbol in VOL_ETP:
        return 'vol_etp'
    if LEVERAGED.match(symbol or ''):
        return 'leveraged'
    if symbol in CRYPTO:
        return 'crypto_proxy'
    if is_etf(name, symbol):
        return 'etf'
    return 'stock'


def cluster_of(symbol, tag):
    if tag == 'crypto_proxy':
        return 'crypto'
    if tag == 'vol_etp':
        return 'vol'
    if tag == 'leveraged':
        return f'lev:{symbol}'
    return CLUSTER.get(symbol, f'single:{symbol}')


def expiry_bucket(tier, n_exp):
    """Map census tier labels (and live-probe counts) onto dailies/weeklies/monthly."""
    t = (tier or '').strip()
    if t.startswith('13') or t == 'dailies':
        return 'dailies'
    if t.startswith('7-12') or t.startswith('4-6') or t == 'weeklies':
        return 'weeklies'
    if n_exp is not None:
        if n_exp >= 13:
            return 'dailies'
        if n_exp >= 4:
            return 'weeklies'
    return 'monthly'


# ------------------------------------------------------------------ Stage 4 metrics

def daily_var(o, h, l, c):
    """0.5 Parkinson + 0.5 close-to-close, per day. Length n-1 (aligned to c[1:])."""
    o, h, l, c = (np.asarray(x, float) for x in (o, h, l, c))
    park = np.log(np.maximum(h[1:] / np.maximum(l[1:], 1e-12), 1e-12)) ** 2 / (4.0 * LN2)
    ctc = np.log(np.maximum(c[1:] / np.maximum(c[:-1], 1e-12), 1e-12)) ** 2
    return np.clip(0.5 * park + 0.5 * ctc, 1e-12, None)


def rv21_series(dvar):
    if len(dvar) < 21:
        return np.array([])
    w = np.convolve(dvar, np.ones(21) / 21.0, mode='valid')
    return np.sqrt(w * ANN)


def har_lite_r2(dvar):
    """In-sample R² of log fwd-5d mean var ~ log(1d, 5d, 22d) averages. Ranking only."""
    n = len(dvar)
    if n < 40:
        return float('nan')
    ys, xs = [], []
    for t in range(21, n - 5):
        y = dvar[t + 1:t + 6].mean()
        x1 = dvar[t]
        x5 = dvar[t - 4:t + 1].mean()
        x22 = dvar[t - 21:t + 1].mean()
        if min(y, x1, x5, x22) <= 0:
            continue
        ys.append(math.log(y))
        xs.append([1.0, math.log(x1), math.log(x5), math.log(x22)])
    if len(ys) < 30:
        return float('nan')
    X, y = np.asarray(xs), np.asarray(ys)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    yhat = X @ beta
    ss_res = ((y - yhat) ** 2).sum()
    ss_tot = ((y - y.mean()) ** 2).sum()
    return float(1 - ss_res / max(ss_tot, 1e-18))


def metrics(o, h, l, c, v):
    o, h, l, c, v = (np.asarray(x, float) for x in (o, h, l, c, v))
    if len(c) < 260:
        return None
    dvar = daily_var(o, h, l, c)
    rv = rv21_series(dvar)
    if len(rv) < 30:
        return None
    rv21 = float(rv[-1])
    win = rv[-252:] if len(rv) >= 252 else rv
    pct_1y = float((win < rv21).mean())
    overnight = np.log(np.maximum(o[1:] / np.maximum(c[:-1], 1e-12), 1e-12))
    intraday = np.log(np.maximum(c[1:] / np.maximum(o[1:], 1e-12), 1e-12))
    vo, vi = float(np.var(overnight)), float(np.var(intraday))
    overnight_share = vo / max(vo + vi, 1e-18)
    daily_ret = np.log(np.maximum(c[1:] / np.maximum(c[:-1], 1e-12), 1e-12))
    sigma = float(daily_ret.std(ddof=1) or 1e-8)
    gap_abs = np.abs(overnight)
    gap_tail = float(np.quantile(gap_abs, 0.99) / sigma)
    # MAE over 10-day windows, both sides, via H/L
    maes = []
    for i in range(max(0, len(c) - 252), len(c) - 10):
        up = h[i + 1:i + 11].max() / c[i] - 1.0
        dn = 1.0 - l[i + 1:i + 11].min() / c[i]
        maes.append(max(up, dn))
    mae10_p95 = float(np.quantile(maes, 0.95)) if maes else float('nan')
    if len(rv) >= 40:
        dlog = np.diff(np.log(np.maximum(rv, 1e-8)))
        vol_of_vol = float(dlog.std(ddof=1) * math.sqrt(ANN))
        exp = [rv[t + 21] / rv[t] > 1.5 for t in range(0, len(rv) - 21)]
        expansion_prob = float(np.mean(exp)) if exp else float('nan')
    else:
        vol_of_vol = expansion_prob = float('nan')
    ers = []
    for i in range(20, len(c)):
        sl = c[i - 20:i + 1]
        path = np.abs(np.diff(sl)).sum()
        ers.append(abs(sl[-1] - sl[0]) / path if path > 0 else 0.0)
    eff_ratio = float(np.median(ers)) if ers else float('nan')
    # earnings signature: top-2% |gaps| spaced ~63±8 sessions
    k = max(2, int(0.02 * len(gap_abs)))
    top_idx = np.argsort(gap_abs)[-k:]
    top_idx.sort()
    hits = 0
    for a, b in zip(top_idx, top_idx[1:]):
        if 55 <= (b - a) <= 71:
            hits += 1
    earnings_sig = float(hits / max(len(top_idx) - 1, 1))
    dollar_vol = float(np.median(c * v))
    return {
        'rv21': rv21, 'pct_1y': pct_1y, 'har_r2': har_lite_r2(dvar),
        'overnight_share': overnight_share, 'gap_tail': gap_tail,
        'mae10_p95': mae10_p95, 'vol_of_vol': vol_of_vol,
        'expansion_prob': expansion_prob, 'eff_ratio': eff_ratio,
        'earnings_sig': earnings_sig, 'dollar_vol': dollar_vol,
        'n_bars': int(len(c)),
    }


def cross_sectional_scores(rows):
    def col(key, default=0.0):
        return [default if _nan(r.get(key)) else r[key] for r in rows]

    def midness(pcts):
        return [1.0 - 2.0 * abs(p - 0.5) for p in pcts]

    n = len(rows)
    if n == 0:
        return rows
    r_har = _zrank(col('har_r2'))
    r_gap = _zrank([-x for x in col('gap_tail', 10)])
    r_on = _zrank([-x for x in col('overnight_share', 1)])
    r_vov = _zrank([-x for x in col('vol_of_vol', 1)])
    r_rv = _zrank(col('rv21'))
    r_mid = _zrank(midness(col('pct_1y', 0.5)))
    r_dv = _zrank(col('dollar_vol'))
    r_npct = _zrank([-x for x in col('pct_1y', 0.5)])
    r_exp = _zrank(col('expansion_prob'))
    r_vov_pos = _zrank(col('vol_of_vol'))
    r_eff = _zrank(col('eff_ratio'))
    for i, r in enumerate(rows):
        ev = 1.0 if r.get('event_mode') else 0.0
        r['short_vol_score'] = (
            0.25 * r_har[i] + 0.20 * r_gap[i] + 0.15 * r_on[i]
            + 0.10 * r_vov[i] + 0.10 * r_rv[i] + 0.10 * r_mid[i]
            + 0.10 * r_dv[i] - 0.15 * ev
        )
        r['long_convex_score'] = (
            0.30 * r_npct[i] + 0.25 * r_exp[i] + 0.20 * r_vov_pos[i]
            + 0.15 * r_eff[i] + 0.10 * r_dv[i]
        )
    return rows


def top_n(rows, key, n=3):
    """Top-n by score; skip leveraged; one slot per factor cluster."""
    picked, used = [], set()
    for r in sorted(rows, key=lambda x: x.get(key) or -1e9, reverse=True):
        if r.get('tag') == 'leveraged':
            continue
        cl = r.get('cluster')
        if cl in used:
            continue
        used.add(cl)
        picked.append(r)
        if len(picked) >= n:
            break
    return picked


# ------------------------------------------------------------------ data fetch

def load_census(db_path):
    import duckdb
    con = duckdb.connect(db_path, read_only=True)
    try:
        rows = con.execute("""
            SELECT symbol, name, exchange, expiry_tier, expirations, strikes,
                   penny_contracts, total_oi, european_style, tradable,
                   coalesce(adjusted_contracts, 0)
            FROM option_underlyings
        """).fetchall()
    except Exception as e:
        con.close()
        raise SystemExit(f"option_underlyings missing in {db_path}: {e}\n"
                         "Run `make data-profile` or pass --db after derive.py.")
    con.close()
    out = []
    for r in rows:
        out.append({
            'symbol': r[0], 'name': r[1], 'exchange': r[2], 'expiry_tier': r[3],
            'expirations': int(r[4] or 0), 'strikes': int(r[5] or 0),
            'penny_contracts': int(r[6] or 0), 'total_oi': float(r[7] or 0),
            'european_style': bool(r[8]), 'tradable': r[9] is not False,
            'adjusted_contracts': int(r[10] or 0),
        })
    return out


def probe_contracts(symbol, today):
    """Live floor of chain shape. Both expiry bounds required (Alpaca front-week trap)."""
    lo, hi = today.isoformat(), (today + dt.timedelta(days=400)).isoformat()
    params = {
        'underlying_symbols': symbol, 'status': 'active', 'limit': 10000,
        'expiration_date_gte': lo, 'expiration_date_lte': hi,
    }
    contracts, token = [], None
    headers = auth_headers()
    for _ in range(8):
        p = dict(params)
        if token:
            p['page_token'] = token
        try:
            resp = requests.get(f'{PAPER_HOST}/v2/options/contracts',
                                headers=headers, params=p, timeout=30)
        except Exception:
            return None
        if not resp.ok:
            return None
        body = resp.json() if resp.text.strip() else {}
        batch = body.get('option_contracts') or []
        contracts.extend(batch)
        token = body.get('next_page_token')
        if not token:
            break
    if not contracts:
        return None
    exps = set()
    for c in contracts:
        e = c.get('expiration_date')
        if e:
            exps.add(str(e)[:10])
    n12 = n45 = 0
    for e in exps:
        try:
            d = dt.date.fromisoformat(e)
        except ValueError:
            continue
        delta = (d - today).days
        if 0 <= delta <= 12:
            n12 += 1
        if 0 <= delta <= 45:
            n45 += 1
    # Census tiers count expirations in a ~month walk, not a 400-day listing.
    # 0DTE names put 6+ dates inside DTE≤12; weeklies put 1–5 plus ≥4 in 45d.
    if n12 >= 6:
        tier = 'dailies'
    elif n12 >= 1 and n45 >= 4:
        tier = 'weeklies'
    else:
        tier = 'monthly'
    strikes = {c.get('strike_price') for c in contracts if c.get('strike_price') is not None}
    oi = sum(float(c['open_interest'] or 0) for c in contracts if c.get('open_interest') is not None)
    penny = sum(1 for c in contracts if c.get('ppind'))
    euro = sum(1 for c in contracts if (c.get('style') or '').lower() == 'european')
    name = (contracts[0].get('name') or '')
    return {
        'symbol': symbol, 'name': name, 'exchange': None,
        'expirations': n45, 'expirations_12d': n12, 'expirations_listed': len(exps),
        'strikes': len(strikes),
        'penny_contracts': penny, 'total_oi': oi,
        'european_style': euro > 0, 'tradable': True, 'adjusted_contracts': 0,
        'expiry_tier': tier,
    }


def fetch_bars(symbols, years=2):
    end = dt.date.today() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=int(365 * years) + 30)
    out = {s: [] for s in symbols}

    def pull(chunk, feed):
        rows_by = {s: [] for s in chunk}
        token = None
        while True:
            args = ['data', 'multi-bars', '--symbols', ','.join(chunk),
                    '--timeframe', '1Day', '--start', str(start),
                    '--end', str(end), '--feed', feed,
                    '--adjustment', 'split', '--limit', '10000']
            if token:
                args += ['--page-token', token]
            body = _cli_json(args, allow_fail=True, timeout=180)
            if not body:
                return None
            bars = body.get('bars') or {}
            for s, rows in bars.items():
                rows_by.setdefault(s, []).extend(rows or [])
            token = body.get('next_page_token')
            if not token:
                break
        return rows_by

    for i in range(0, len(symbols), 50):
        chunk = symbols[i:i + 50]
        got = pull(chunk, 'sip') or pull(chunk, 'iex') or {}
        for s, rows in got.items():
            out[s] = rows
        print(f"  bars {chunk[0]}..{chunk[-1]}: "
              f"{sum(1 for s in chunk if len(out.get(s) or []) >= 260)}/"
              f"{len(chunk)} with ≥260 days", flush=True)
    return out


def fetch_vix():
    series = {}
    for idx in ('VIX', 'VIX3M'):
        r = requests.get(CBOE.format(idx), timeout=30)
        r.raise_for_status()
        import csv, io
        d = {}
        for row in csv.DictReader(io.StringIO(r.text)):
            k = row.get('DATE') or row.get('Date')
            v = row.get('CLOSE') or row.get('Close')
            if not k or not v:
                continue
            try:
                day = dt.datetime.strptime(k, '%m/%d/%Y').date()
            except ValueError:
                day = dt.date.fromisoformat(k[:10])
            d[day] = float(v)
        series[idx] = d
    days = sorted(series['VIX'])
    if len(days) < 60:
        return None
    vix = [series['VIX'][t] for t in days]
    last, last_t = vix[-1], days[-1]
    win = vix[-252:] if len(vix) >= 252 else vix
    pct = float(sum(1 for x in win if x < last) / len(win))
    v3 = series['VIX3M'].get(last_t)
    ratio = (last / v3) if v3 else None
    return {
        'as_of': str(last_t), 'vix': last, 'vix_pct_1y': pct,
        'vix3m': v3, 'vix_vix3m': ratio,
        'contango_proxy': (ratio < 1.0) if ratio else None,
        'vx_proxy': 'vix3m_not_futures',
    }


def fetch_chain(symbol, dte_lo=1, dte_hi=12):
    today = dt.date.today()
    return _cli_json([
        'data', 'option', 'chain', '--underlying-symbol', symbol,
        '--feed', 'indicative', '--limit', '1000',
        '--expiration-date-gte', str(today + dt.timedelta(days=dte_lo)),
        '--expiration-date-lte', str(today + dt.timedelta(days=dte_hi)),
    ], allow_fail=True)


# ------------------------------------------------------------------ stages 0–3

def eligible(universe):
    """Stages 0–3. Each drop has a reason. Ranking does not happen here."""
    kept, dropped = [], []
    for u in universe:
        sym = u['symbol']
        if u.get('european_style') or not u.get('tradable', True):
            dropped.append((sym, 'S0_venue_style')); continue
        if sym in VOL_ETP and sym != 'VXX':
            dropped.append((sym, 'S0_vol_etp_not_vxx')); continue
        if (u.get('adjusted_contracts') or 0) > 0:
            dropped.append((sym, 'S0_adjusted')); continue
        bucket_exp = expiry_bucket(u.get('expiry_tier'), u.get('expirations'))
        if bucket_exp == 'monthly':
            dropped.append((sym, 'S1_monthly_only')); continue
        if (u.get('penny_contracts') or 0) <= 0:
            dropped.append((sym, 'S2_not_penny')); continue
        if (u.get('strikes') or 0) < MIN_STRIKES:
            dropped.append((sym, 'S2_sparse_strikes')); continue
        oi = float(u.get('total_oi') or 0)
        if bucket_exp == 'dailies' and oi >= OI_CORE:
            bucket = 'CORE'
        elif oi >= OI_EXTENDED:
            bucket = 'EXTENDED'
        else:
            dropped.append((sym, 'S2_oi_floor')); continue
        tag = factor_tag(sym, u.get('name'))
        kept.append({
            **u, 'bucket': bucket, 'tag': tag,
            'cluster': cluster_of(sym, tag),
            'event_mode': tag == 'stock',
            'expiry_bucket': bucket_exp,
        })
    return kept, dropped


# ------------------------------------------------------------------ Stage 5

def stage5(row, bars, vix):
    """Trade-time gates. Default NO_TRADE. OHLCV rank is not a licence to trade."""
    reasons = []
    chain = fetch_chain(row['symbol'])
    snaps = (chain or {}).get('snapshots') or {}
    ivs, spreads, deltas = [], [], []
    for snap in snaps.values():
        g, q = snap.get('greeks') or {}, snap.get('latestQuote') or {}
        iv, dlt, bid, ask = snap.get('impliedVolatility'), g.get('delta'), q.get('bp'), q.get('ap')
        if iv:
            ivs.append(float(iv))
        if dlt is not None:
            deltas.append(abs(float(dlt)))
        if bid and ask and ask > 0:
            mid = (bid + ask) / 2
            if mid > 0:
                spreads.append((ask - bid) / mid)
    atm_iv = float(np.median(ivs)) if ivs else None
    spread = float(np.median(spreads)) if spreads else None

    o = [b['o'] for b in bars]; h = [b['h'] for b in bars]
    l = [b['l'] for b in bars]; c = [b['c'] for b in bars]
    try:
        from us_adaptive.vol import har_forecast, sanitize_bars
        so, sh, sl, sc, _ = sanitize_bars(o, h, l, c)
        har = har_forecast(so, sh, sl, sc, horizon_days=10)
        rv_f = har.get('rv_f')
    except Exception:
        rv_f = None
        har = {}

    if not atm_iv or not rv_f or _nan(rv_f):
        reasons.append('missing_iv_or_rvf')
    elif atm_iv < 1.2 * rv_f:
        reasons.append(f'iv_cheap_vs_rvf {atm_iv:.3f}<1.2*{rv_f:.3f}')

    if not vix:
        reasons.append('no_vix')
    else:
        if vix['vix_pct_1y'] >= 0.70:
            reasons.append(f'vix_pct {vix["vix_pct_1y"]:.2f}>=0.70')
        if row.get('short_vol') and vix.get('contango_proxy') is False:
            reasons.append('backwardation_vix3m_proxy')

    if spread is None or spread > 0.10:
        reasons.append(f'spread {spread}')

    mae = row.get('mae10_p95')
    if mae and mae > 0.12:
        reasons.append(f'mae10_p95 {mae:.3f} — defined-risk only')

    # Condors are defined-risk, so the MAE note does not alone veto a short-vol ETF.
    hard = [x for x in reasons if not x.startswith('mae10')]
    decision = 'NO_TRADE' if hard else 'INVESTIGATE'
    return {
        'decision': decision, 'reasons': reasons,
        'atm_iv': atm_iv, 'rv_f': None if _nan(rv_f) else rv_f,
        'iv_rv_f': (atm_iv / rv_f) if atm_iv and rv_f else None,
        'median_spread': spread, 'n_quotes': len(snaps),
        'har_r2_purged': har.get('r2_is') if isinstance(har, dict) else None,
    }


# ------------------------------------------------------------------ run

def run(db=None, out_path=None, years=2, do_stage5=False, top=3):
    today = dt.date.today()
    db = db or os.path.join(REPO, 'reports/data-profile/alpaca_data.duckdb')
    if os.path.exists(db):
        universe = load_census(db)
        source = 'option_underlyings'
    else:
        print(f"no census at {db} — live-probing {len(SEED)} liquid names "
              f"(OI/expiry counts are floors, not a census)", flush=True)
        universe = []
        for i, s in enumerate(SEED, 1):
            u = probe_contracts(s, today)
            if not u:
                detail = 'skip'
            else:
                detail = (f"{u['expiry_tier']:<8} exp12={u.get('expirations_12d')} "
                          f"oi={u['total_oi']:.0f} penny={u['penny_contracts']}")
            print(f"  probe {i}/{len(SEED)} {s}: {detail}", flush=True)
            if u:
                universe.append(u)
        source = 'live_probe'

    kept, dropped = eligible(universe)
    drop_counts = {}
    for _, why in dropped:
        drop_counts[why] = drop_counts.get(why, 0) + 1
    print(f"\nS0–S3  in={len(universe)}  eligible={len(kept)}  "
          f"CORE={sum(1 for k in kept if k['bucket']=='CORE')}  "
          f"EXTENDED={sum(1 for k in kept if k['bucket']=='EXTENDED')}")
    for k, n in sorted(drop_counts.items()):
        print(f"  drop {k}: {n}")

    symbols = [k['symbol'] for k in kept]
    print(f"\nS4 fetching {years}y daily bars for {len(symbols)} names…", flush=True)
    bars = fetch_bars(symbols, years=years) if symbols else {}
    scored = []
    for k in kept:
        rows = bars.get(k['symbol']) or []
        if len(rows) < 260:
            k['skip'] = 'S4_insufficient_ohlcv'
            continue
        o = [b.get('o') or b.get('open') for b in rows]
        h = [b.get('h') or b.get('high') for b in rows]
        l = [b.get('l') or b.get('low') for b in rows]
        c = [b.get('c') or b.get('close') for b in rows]
        v = [b.get('v') or b.get('volume') or 0 for b in rows]
        m = metrics(o, h, l, c, v)
        if not m:
            k['skip'] = 'S4_metrics_failed'
            continue
        # stocks with a strong earnings signature stay in event_mode
        if k['tag'] == 'stock' or m['earnings_sig'] >= 0.3:
            k['event_mode'] = True
        scored.append({**k, **m})
    skip_n = {}
    for k in kept:
        if k.get('skip'):
            skip_n[k['skip']] = skip_n.get(k['skip'], 0) + 1
    print(f"S4 scored={len(scored)}" +
          ('' if not skip_n else '  ' + ' '.join(f'{w}={n}' for w, n in skip_n.items())))
    scored = cross_sectional_scores(scored)

    # crypto_proxy occupies the crypto slot — exclude from both VRP branches.
    # Single names are event-mode (S3): excluded from the short-vol / VRP branch.
    vrp = [r for r in scored if r['tag'] != 'crypto_proxy']
    shortlist_sv = top_n([r for r in vrp if not r.get('event_mode')],
                         'short_vol_score', top)
    shortlist_lc = top_n(vrp, 'long_convex_score', top)
    for r in shortlist_sv:
        r['short_vol'] = True

    vix = None
    try:
        vix = fetch_vix()
        print(f"\nVIX {vix['vix']:.1f}  pct1y={vix['vix_pct_1y']:.2f}  "
              f"VIX/VIX3M={vix['vix_vix3m']}  contango_proxy={vix['contango_proxy']}  "
              f"({vix['vx_proxy']})")
    except Exception as e:
        print(f"VIX fetch failed: {e}")

    stage5_out = {}
    if do_stage5:
        print("\nS5 trade-time IV gate on shortlists (default NO_TRADE)…", flush=True)
        seen = {r['symbol']: r for r in shortlist_sv + shortlist_lc}
        for sym, r in seen.items():
            gate = stage5(r, bars.get(sym) or [], vix)
            stage5_out[sym] = gate
            print(f"  {sym:<6} {gate['decision']:<12} iv/rv_f={gate['iv_rv_f']}  {gate['reasons'][:3]}")

    def slim(r):
        keys = ('symbol', 'bucket', 'tag', 'cluster', 'event_mode', 'expiry_bucket',
                'total_oi', 'strikes', 'penny_contracts', 'rv21', 'pct_1y', 'har_r2',
                'overnight_share', 'gap_tail', 'mae10_p95', 'vol_of_vol',
                'expansion_prob', 'eff_ratio', 'earnings_sig', 'dollar_vol',
                'short_vol_score', 'long_convex_score')
        return {k: r.get(k) for k in keys}

    doc = {
        'as_of': today.isoformat(),
        'census_source': source,
        'integrity': [
            'top-N from ~200 is multiple testing: a set of hypotheses, not a proven order',
            'har_r2 is in-sample — ranking only; finalist needs purged walk-forward HAR',
            'OI figures are floors (capped crawl or live probe) — rank, not census',
            'VIX/VIX3M is a contango proxy; VX futures are not on this venue',
            'Stage 5 default is NO_TRADE; rank is not a licence to trade',
        ],
        'vix': vix,
        's0_s3': {
            'in': len(universe), 'eligible': len(kept),
            'core': [k['symbol'] for k in kept if k['bucket'] == 'CORE'],
            'extended_n': sum(1 for k in kept if k['bucket'] == 'EXTENDED'),
            'drops': drop_counts,
        },
        'scored': [slim(r) for r in sorted(scored, key=lambda x: -x['short_vol_score'])],
        'shortlist_sv': [slim(r) for r in shortlist_sv],
        'shortlist_lc': [slim(r) for r in shortlist_lc],
        'stage5': stage5_out,
    }
    out_path = out_path or os.path.join(REPO, 'data', 'universe_ranked.json')
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(doc, f, indent=2, default=str)
    print(f"\nCORE: {doc['s0_s3']['core']}")
    print(f"ShortVol top-{top}: {[r['symbol'] for r in shortlist_sv]}")
    print(f"LongConvex top-{top}: {[r['symbol'] for r in shortlist_lc]}")
    print(f"wrote {out_path}")
    return doc


def parse_args(argv):
    o = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == '--stage5':
            o['stage5'] = True; i += 1
        elif a.startswith('--'):
            o[a[2:].replace('-', '_')] = argv[i + 1]; i += 2
        else:
            sys.exit(f'unrecognized {a}')
    return o


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help'):
        print(__doc__); sys.exit(0)
    # self-check (no network)
    rng = np.random.default_rng(0)
    n = 400
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    high = close * (1 + np.abs(rng.normal(0, 0.005, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.005, n)))
    opn = np.r_[close[0], close[:-1] * (1 + rng.normal(0, 0.002, n - 1))]
    vol = rng.integers(1_000_000, 5_000_000, n).astype(float)
    m = metrics(opn, high, low, close, vol)
    assert m and m['rv21'] > 0 and 0 <= m['pct_1y'] <= 1, m
    assert m['har_r2'] <= 1, m
    dummy = [{**m, 'event_mode': False, 'tag': 'etf', 'cluster': 'a', 'symbol': 'A'},
             {**m, 'event_mode': True, 'tag': 'stock', 'cluster': 'b', 'symbol': 'B',
              'har_r2': m['har_r2'] / 2}]
    dummy[1]['gap_tail'] = m['gap_tail'] * 3
    dummy = cross_sectional_scores(dummy)
    assert dummy[0]['short_vol_score'] > dummy[1]['short_vol_score']
    assert factor_tag('VXX', '') == 'vol_etp' and factor_tag('TQQQ', '') == 'leveraged'
    assert factor_tag('IBIT', '') == 'crypto_proxy' and factor_tag('SPY', 'SPDR S&P 500 ETF') == 'etf'
    assert expiry_bucket('13+', 20) == 'dailies' and expiry_bucket('1 — monthly only', 1) == 'monthly'
    kept, dropped = eligible([
        {'symbol': 'SPX', 'european_style': True, 'tradable': True, 'expiry_tier': '13+',
         'penny_contracts': 10, 'strikes': 200, 'total_oi': 2e7, 'expirations': 20, 'name': ''},
        {'symbol': 'UVXY', 'european_style': False, 'tradable': True, 'expiry_tier': 'weeklies',
         'penny_contracts': 10, 'strikes': 80, 'total_oi': 5e5, 'expirations': 8, 'name': ''},
        {'symbol': 'ABC', 'european_style': False, 'tradable': True, 'expiry_tier': '1 — monthly only',
         'penny_contracts': 10, 'strikes': 80, 'total_oi': 2e6, 'expirations': 1, 'name': 'ABC Inc'},
        {'symbol': 'SPY', 'european_style': False, 'tradable': True, 'expiry_tier': '13+',
         'penny_contracts': 10, 'strikes': 200, 'total_oi': 2e6, 'expirations': 20,
         'name': 'SPDR S&P 500 ETF'},
        {'symbol': 'IWM', 'european_style': False, 'tradable': True, 'expiry_tier': 'weeklies',
         'penny_contracts': 5, 'strikes': 80, 'total_oi': 4e5, 'expirations': 8,
         'name': 'iShares Russell 2000 ETF'},
    ])
    why = {s: w for s, w in dropped}
    assert why['SPX'] == 'S0_venue_style' and why['UVXY'] == 'S0_vol_etp_not_vxx'
    assert why['ABC'] == 'S1_monthly_only'
    by = {k['symbol']: k for k in kept}
    assert by['SPY']['bucket'] == 'CORE' and by['IWM']['bucket'] == 'EXTENDED'
    print('asset_selector.py self-check OK')
    if len(sys.argv) == 1:
        sys.exit(0)
    o = parse_args(sys.argv[1:])
    run(db=o.get('db'), out_path=o.get('out'), years=float(o.get('years', 2)),
        do_stage5=bool(o.get('stage5')), top=int(o.get('top', 3)))
