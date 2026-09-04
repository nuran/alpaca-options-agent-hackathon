"""
Code that obeys skills/options-core-patterns (P4/P8/P9, S1–S5, Stage 5).

The LLM never calls this. classify_setup / pick_short_structure do. VIX-option S5
is untradable on this venue: S5 maps to a VXX put debit (V5: tactical, not a warehouse).
S1 is a 2-leg short strangle (MAE miss → iron condor). S3 is an intra-window calendar.
S4 is a 2-leg ATM straddle only when nothing longer than 2 DTE is in the pool.
"""
from __future__ import annotations

import math
import os
import importlib.util

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

def _load(name, rel):
    path = os.path.join(REPO, rel)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

SEL = _load('asset_selector', os.path.join('data', 'asset_selector.py'))
UA_VOL = _load('ua_vol', os.path.join('us_adaptive', 'vol.py'))

# Numbers that appear in the skill text, not invented here.
K_SHORT = 1.2          # S1: IV vs HAR with a buffer (same bar as min_iv_rv_ratio)
K_LONG = 0.85          # below this, IV is cheap vs the forecast → debit
SKEW_RICH = 1.12       # S2: 15d put IV / 15d call IV — crash wing vs the other wing
MAE_BREACH = 1.15      # MAE vs short-strike distance: 15%+ → no new short
OVERNIGHT_MAX = 0.55   # share of variance that arrives unhedgeable
VIX_STRESS = 0.70      # 1y percentile: short-vol gate
VIX_FLATTEN = 0.50     # prefer S2 (defined-risk put) over S1 when the curve is tired
S4_MAX_DTE = 2         # 0–2 DTE short ATM: friction dominates; skip
HAR_HORIZON = 10       # forecast the structure's 7–12d window
EARNINGS_SIG = 0.40    # auto-detected event-mode from OHLCV gaps
EFF_TREND = 0.40       # Kaufman efficiency: movement exists for a debit


def har_forecast_rv(o, h, l, c, horizon_days=HAR_HORIZON):
    """Purged HAR-RV, annualised. None if the sample cannot fit. Never trailing HV."""
    if o is None or len(o) < 90:
        return None
    try:
        o, h, l, c, _n = UA_VOL.sanitize_bars(o, h, l, c)
        out = UA_VOL.har_forecast(o, h, l, c, int(horizon_days))
    except Exception:
        return None
    rv_f = out.get('rv_f')
    if rv_f is None or rv_f != rv_f or rv_f <= 0:
        return None
    return float(rv_f)


def ohlcv_metrics(o, h, l, c, v=None):
    """Stage-4 metrics from live/history bars. None if the window is too short."""
    if o is None or len(o) < 260:
        return None
    if v is None:
        v = [1.0] * len(c)
    return SEL.metrics(o, h, l, c, v)


def vix_context(ranked=None, rules=None):
    """
    VIX as a structure input, not only an on/off switch.

    Returns dict: short_ok, size_mult, prefer_s2, pct, contango, reason.
    Missing snapshot → UNKNOWN, short_ok True with size_mult 1 (caller still has
    live IV/HAR). Backwardation / 70th percentile → no new short vega; debit ok.
    """
    vix = (ranked or {}).get('vix') if ranked else None
    sk = (rules or {}).get('skills') or {}
    if sk.get('enforce_vix_regime') is False:
        return {'short_ok': True, 'size_mult': 1.0, 'prefer_s2': False,
                'pct': None, 'contango': None, 'reason': 'regime_gate_disabled'}
    if not vix:
        return {'short_ok': True, 'size_mult': 1.0, 'prefer_s2': False,
                'pct': None, 'contango': None, 'reason': 'no_vix_snapshot'}
    pct = vix.get('vix_pct_1y')
    contango = vix.get('contango_proxy')
    if contango is False:
        return {'short_ok': False, 'size_mult': 0.0, 'prefer_s2': True,
                'pct': pct, 'contango': False, 'reason': 'P8_backwardation'}
    if pct is not None and pct >= VIX_STRESS:
        return {'short_ok': False, 'size_mult': 0.0, 'prefer_s2': True,
                'pct': pct, 'contango': contango, 'reason': 'P8_vix_pct_1y>=0.70'}
    flatten = pct is not None and pct >= VIX_FLATTEN
    size = 0.5 if flatten else 1.0
    return {'short_ok': True, 'size_mult': size, 'prefer_s2': flatten,
            'pct': pct, 'contango': contango, 'reason': 'contango_proxy'}


def mae_ok(spot, short_distance, mae10_p95, breach=MAE_BREACH):
    """True if historical 10d MAE does not overrun the short strike by 15%+."""
    if not spot or not short_distance or mae10_p95 is None:
        return True, 'no_mae'
    if mae10_p95 != mae10_p95:  # NaN
        return True, 'no_mae'
    dist_pct = abs(short_distance) / float(spot)
    if dist_pct <= 0:
        return False, 'zero_wing'
    if mae10_p95 > breach * dist_pct:
        return False, 'mae_overruns_wing'
    return True, 'mae_ok'


def rank_setup(iv_atm, rv_forecast, dte, trend, move_sigma, skew=None,
               ohlcv=None, vix=None, event_mode=False, earnings_in_window=False,
               cfg=None, enable_debit=True):
    """
    S1–S5 ranking for one name, one session. family is the only structure to build.

    S5 (VIX options) is absent on Alpaca — VXX put debit instead. S1 is a 2-leg
    short strangle (MAE overrun → iron condor). S4 is a 2-leg ATM straddle only
    when the pool has no tenor ≥ 3 DTE. S3 is an intra-window calendar.
    """
    c = dict(cfg or {})
    k_short = float(c.get('min_iv_rv_ratio') or K_SHORT)
    k_long = float(c.get('k_long') or K_LONG)
    skew_bar = float(c.get('skew_rich') or SKEW_RICH)
    move_cap = float(c.get('move_sigma_cap') or 1.5)
    overnight_max = float(c.get('overnight_share_max') or OVERNIGHT_MAX)
    if c.get('enable_debit') is False:
        enable_debit = False

    out = {
        'rank': None, 'family': None, 'branch': None, 'reason': 'unclassified',
        'iv_atm': iv_atm, 'iv_har': None, 'rv_forecast': rv_forecast,
        'trend': trend, 'move_sigma': move_sigma, 'skew': skew,
        'vix': vix, 'dte': dte, 'stage5': [],
    }
    vix = vix or {'short_ok': True, 'size_mult': 1.0, 'prefer_s2': False,
                  'reason': 'no_vix'}

    if not rv_forecast or rv_forecast <= 1e-8:
        out['reason'] = 'no_har_forecast'
        out['stage5'].append('NO_TRADE_no_har')
        return out
    if not iv_atm:
        out['reason'] = 'no_atm_iv'
        out['stage5'].append('NO_TRADE_no_iv')
        return out

    iv_har = iv_atm / rv_forecast
    out['iv_har'] = round(iv_har, 3)
    out['iv_rv'] = round(iv_har, 3)  # same field the journal already prints

    m = ohlcv or {}
    event = (event_mode or earnings_in_window
             or (m.get('earnings_sig') or 0) >= EARNINGS_SIG)
    overnight_heavy = (m.get('overnight_share') or 0) > overnight_max

    def _long(family, reason, rank='LC'):
        out['branch'] = 'long'
        if not enable_debit:
            out['reason'] = reason + '_debit_disabled'
            return out
        out['family'] = family
        out['rank'] = rank
        out['reason'] = reason
        return out

    def _skip(reason, branch=None):
        out['branch'] = branch
        out['reason'] = reason
        out['stage5'].append('NO_TRADE_' + reason)
        return out

    # S3: event is its own regime — intra-window calendar, not a VRP skip.
    if event:
        out['branch'] = 'event'
        out['family'] = 'calendar'
        out['rank'] = 'S3'
        out['reason'] = 's3_event_calendar'
        return out

    # Cheap IV vs HAR → long convexity (defined-risk debit).
    if iv_har <= k_long:
        if trend == 'up':
            return _long('call_debit', 'cheap_iv_uptrend_call_debit')
        if trend == 'down':
            return _long('put_debit', 'cheap_iv_downtrend_put_debit')
        if (m.get('eff_ratio') or 0) >= EFF_TREND:
            return _long('call_debit', 'cheap_iv_efficient_call_debit')
        return _skip('cheap_iv_no_convexity_trigger', 'long')

    if iv_har < k_short:
        return _skip('fair_iv_no_vrp')

    symbol = (c.get('underlying') or '').upper()
    if not vix.get('short_ok', True):
        # S5 on Alpaca: VIX options do not exist. Normalization = VXX put debit (V5).
        if symbol == 'VXX':
            return _long('put_debit', 's5_vxx_put_debit_normalization', rank='S5')
        return _skip(vix.get('reason') or 'vix_no_short')

    if (move_sigma or 0) > move_cap:
        return _skip('move_already_realized', 'short')

    if overnight_heavy:
        return _skip('overnight_gap_too_large', 'short')

    out['branch'] = 'short'
    dte = 0 if dte is None else int(dte)
    has_longer = bool(c.get('has_longer_tenor'))
    if dte <= S4_MAX_DTE and not has_longer:
        out['family'] = 'short_straddle'
        out['rank'] = 'S4'
        out['reason'] = 's4_short_atm_straddle'
        return out
    if trend == 'down':
        return _skip('rich_iv_downtrend_skip', 'short')
    skew_rich = bool(skew and skew >= skew_bar)
    prefer_s2 = bool(vix.get('prefer_s2') or skew_rich)
    if trend == 'up' or prefer_s2:
        out['family'] = 'put_credit'
        out['rank'] = 'S2'
        out['reason'] = 's2_put_credit_rich_wing' if skew_rich else 'rich_iv_uptrend_put'
        return out
    out['family'] = 'short_strangle'
    out['rank'] = 'S1'
    out['reason'] = 's1_short_strangle'
    return out


def path_shape(closes, rv, cfg=None):
    """Chop / up / down from 10d return vs expected 10d RV; 5d move in sigma."""
    c = dict(cfg or {})
    closes = [x for x in (closes or []) if x]
    if not rv or rv <= 1e-8:
        return None, None
    exp10 = rv * math.sqrt(10 / 252)
    exp5 = rv * math.sqrt(5 / 252)
    r10 = (closes[-1] / closes[-11] - 1.0) if len(closes) >= 11 else (
        closes[-1] / closes[0] - 1.0 if len(closes) >= 2 else 0.0)
    r5 = (closes[-1] / closes[-6] - 1.0) if len(closes) >= 6 else 0.0
    move_sigma = abs(r5) / exp5 if exp5 else 0.0
    band = float(c.get('trend_sigma') or 0.5) * exp10
    if r10 > band:
        trend = 'up'
    elif r10 < -band:
        trend = 'down'
    else:
        trend = 'chop'
    return trend, move_sigma


if __name__ == '__main__':
    # Cheap IV vs HAR → debit, not a skip. Rich chop → S1 condor. Skew → S2.
    vix_ok = {'short_ok': True, 'size_mult': 1.0, 'prefer_s2': False, 'reason': 'ok'}
    p = rank_setup(0.16, 0.12, dte=5, trend='chop', move_sigma=0.2, skew=1.0, vix=vix_ok)
    assert p['family'] == 'short_strangle' and p['rank'] == 'S1', p
    p = rank_setup(0.16, 0.12, dte=5, trend='up', move_sigma=0.2, skew=1.0, vix=vix_ok)
    assert p['family'] == 'put_credit' and p['rank'] == 'S2', p
    p = rank_setup(0.16, 0.12, dte=5, trend='chop', move_sigma=0.2, skew=1.20, vix=vix_ok)
    assert p['family'] == 'put_credit' and p['rank'] == 'S2', p
    p = rank_setup(0.16, 0.30, dte=5, trend='up', move_sigma=0.2, vix=vix_ok)
    assert p['family'] == 'call_debit' and p['branch'] == 'long', p
    p = rank_setup(0.16, 0.30, dte=5, trend='down', move_sigma=0.2, vix=vix_ok)
    assert p['family'] == 'put_debit', p
    p = rank_setup(0.16, 0.12, dte=1, trend='chop', move_sigma=0.2, vix=vix_ok,
                   cfg={'has_longer_tenor': True})
    assert p['family'] == 'short_strangle' and p['rank'] == 'S1', p
    p = rank_setup(0.16, 0.12, dte=1, trend='chop', move_sigma=0.2, vix=vix_ok)
    assert p['family'] == 'short_straddle' and p['rank'] == 'S4', p
    p = rank_setup(0.16, 0.12, dte=5, trend='chop', move_sigma=0.2, vix=vix_ok,
                   event_mode=True)
    assert p['family'] == 'calendar' and p['rank'] == 'S3', p
    vix_off = {'short_ok': False, 'size_mult': 0.0, 'prefer_s2': True,
               'reason': 'P8_backwardation'}
    p = rank_setup(0.20, 0.12, dte=5, trend='chop', move_sigma=0.2, vix=vix_off,
                   cfg={'underlying': 'VXX'})
    assert p['family'] == 'put_debit' and p['rank'] == 'S5', p
    p = rank_setup(0.20, 0.12, dte=5, trend='chop', move_sigma=0.2, vix=vix_off)
    assert p['family'] is None and 'P8' in p['reason'], p
    ok, why = mae_ok(100, 5.0, 0.04)
    assert ok and why == 'mae_ok', (ok, why)
    ok, why = mae_ok(100, 2.0, 0.04)
    assert not ok, (ok, why)
    assert har_forecast_rv(None, None, None, None) is None
    print('canon.py self-check OK')
