"""
Short-dated defined-risk premium selling on index ETFs: vertical credit spreads and
delta-neutral iron condors, gated on implied-vs-realized volatility.

The design here is the product of three backtests over 603,962 SPY option bars
(Feb 2024 - Aug 2026), each of which corrected the previous one:

1. **Vertical credit spreads: -8.24%.** The retail options literature says the losing
   trade is *buying* short-dated OTM premium, so selling it looked sound. It was not:
   ranking candidates by credit/width kept picking the CALL side, and 37 of 41 trades
   were short calls into a market that rose 56%. Direction swamped everything.

2. **Delta-neutral condors: -0.39% at ZERO friction.** Selling both wings cancels the
   directional bleed -- and reveals that nothing is left. Unconditional premium
   selling is fairly priced. There is no free lunch in it.

3. **Condors gated on vol richness: +5.71%.** The variance risk premium is
   *time-varying*. Selling only when implied vol is at least 1.2x trailing realized
   vol turns a zero-edge trade into a positive one, and stays positive across the
   whole 1%/3%/6% friction sweep.

Read that as a modest, regime-dependent edge rather than a discovery: it is carried by
2025-2026 (Sharpe 1.05) while 2024 was negative, no sub-period is statistically
significant, and it still underperforms simply holding SPY on both return and Sharpe.

Selection, per candidate day:
  1. Pick the nearest expiry inside [min_dte, max_dte].
  2. Recover each contract's delta and IV by inverting Black-Scholes on its traded
     close -- historical bars carry no Greeks (see blackscholes.py).
  3. Choose short strikes near the target delta on each side; buy protection `width`
     further out.
  4. Reject unless credit/width clears the minimum, measured on the COMBINED credit
     for a condor.
  5. Reject unless the implied vol being sold is rich against trailing realized vol.
"""
import datetime as dt
import math
import os
import sys

import blackscholes as bs

_AGENT = os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')), 'agent')
if _AGENT not in sys.path:
    sys.path.insert(0, _AGENT)
import canon as CANON
import playbook as PB              # the vertical-vs-condor router (imports strategy lazily)

DEFAULTS = {
    'underlying': 'SPY',
    'min_dte': 1,
    'max_dte': 7,
    'target_delta': 0.20,     # short-leg |delta|; ~1 SD out
    'delta_tolerance': 0.10,  # accept 0.10 - 0.30
    'width': 5.0,             # dollars between strikes (cap when width_pct is set)
    'width_pct': None,        # if set, wings = min(width, max(width_min, round(spot * width_pct)))
    'width_min': 1.0,
    'min_credit_ratio': 0.15, # credit / width -- reject cheap risk (condor: combined)
    'min_credit_ratio_vertical': 0.08,  # single-wing floor; 0.15 on one wing is the condor combined bar
    'max_credit_ratio': 0.60, # implausibly rich => almost certainly a bad print
    'side': 'both',           # 'put' | 'call' | 'both' (ignored when structure=adaptive)
    'structure': 'vertical',  # 'vertical' | 'condor' | 'adaptive' (setup portrait)
    'k_long': 0.85,           # IV_atm / HAR-forecast RV below this is cheap vol → debit
    'move_sigma_cap': 1.5,    # |5d return| / (RV*sqrt(5/252)) above this: move already realized
    'trend_sigma': 0.5,       # |10d return| vs expected 10d RV to count as up/down vs chop
    'condor_vs_put_ratio': 0.90,  # unused by the portrait; kept so old tests compile
    'enabled_structures': None,
    'enable_debit': True,     # S4–S7 mapping: defined-risk debit when IV is cheap vs HAR
    'skew_rich': 1.12,        # S2: 15d put/call IV — crash wing vs the other wing
    'overnight_share_max': 0.55,
    'har_horizon_days': 10,
    'max_dte_s3': 12,
    # Only sell when implied vol is rich relative to recent realized vol. The variance
    # risk premium is time-varying, and this is what separates "sell premium always"
    # (no edge -- measured at -0.39% over 2.5 years even at ZERO friction) from
    # "sell premium when it is actually overpriced". None disables the filter.
    'min_iv_rv_ratio': None,
    'rv_window': 21,
    'vol_gate_iv': 'short_leg',  # 'short_leg' (incumbent, backtested) | 'atm' (at the forward; tested 2026-08-28, not better)
    # Second gate (default off): reject when overnight variance share exceeds this.
    # Feature-lab candidate; live keeps null until A/B improves winrate_gap ≥3pts.
    'max_overnight_share': None,
    'min_iv_har_ratio': None,  # second-gate cycle 2; null = off
    # Macro dates: the always-condor book still skips; portrait sets earnings_in_window
    # and ranks S3 calendar instead of halting the day.
    'event_blackout': [],
}


def pick_expiry(available, today, min_dte, max_dte):
    """Nearest expiry whose DTE falls in range."""
    ok = [e for e in available if min_dte <= (e - today).days <= max_dte]
    return min(ok) if ok else None


def dollar_width(spot, cfg):
    """Wings as a fraction of spot, capped at the SPY-calibrated dollar width."""
    base = float(cfg.get('width', 5.0))
    pct = cfg.get('width_pct')
    if pct and spot:
        w = max(float(cfg.get('width_min', 1.0)), float(round(float(spot) * float(pct))))
        return min(base, w)
    return base


def sigma_move(spot, rv, sessions):
    """One-standard-deviation move to expiry, in dollars, on the 252 clock."""
    if not spot or not rv or not sessions:
        return None
    return float(spot) * float(rv) * math.sqrt(max(float(sessions), 1.0) / 252.0)


def wing_width(cfg, contracts):
    """
    The wing for THIS expiry, in dollars.

    With `wing_sigmas` set the wing is a fixed number of standard-deviation moves; the
    dollar figure then falls out of the underlying's vol and the time left. Without it
    the configured dollar width is used unchanged.

    A fixed dollar wing is not one structure at different tenors, it is different
    structures wearing one label. Measured on SPY at 12.6% vol, $5 is:

        DTE  1  ->  1.05 sigma      DTE 10  ->  0.33 sigma
        DTE  7  ->  0.40 sigma      DTE 15  ->  0.27 sigma

    The short strike is pinned at 0.15 delta, so the probability of a breach barely
    moves with tenor -- but the protection creeps toward the short leg, the credit
    thins, and the payoff decays. That is visible directly in the DTE ladder on this
    store (SPY, sessions basis, IV/RV 1.2, no drawdown halt):

        DTE 1-3   -1.90%  Sharpe -0.08        DTE  8-11  -7.08%  Sharpe -0.62
        DTE 4-7   +2.89%  Sharpe +0.23        DTE 12-15  -9.94%  Sharpe -1.06

    Whether that decay IS the geometry or something else is the question a
    sigma-anchored wing answers: hold the wing constant in sigma and the slope should
    flatten. If it does not, the tenor itself is the problem and the geometry is
    exonerated.
    """
    sig = cfg.get('wing_sigmas')
    if not sig:
        return float(cfg.get('width') or 5.0)
    sessions = next((c.get('sessions_to_expiry') or c.get('dte') for c in contracts
                     if c.get('sessions_to_expiry') or c.get('dte')), None)
    move = sigma_move(cfg.get('spot'), cfg.get('_rv_geometry'), sessions)
    if not move:
        return float(cfg.get('width') or 5.0)
    return max(float(cfg.get('width_min', 1.0)), round(float(sig) * move))


def choose_short(condor, put, floor=0.90):
    """
    Legacy credit/width tie-break. Not the portrait: ranking condor vs put by
    credit/width always prefers the condor (two credits, one width).
    """
    if condor and put:
        return condor if condor['credit_ratio'] >= float(floor) * put['credit_ratio'] else put
    return condor or put


def _delta_iv(contracts, right, target=0.15):
    same = [c for c in contracts if _right(c) == right and c.get('iv') and c.get('delta') is not None]
    if not same:
        return None
    return min(same, key=lambda c: abs(abs(c['delta']) - target))['iv']


def classify_setup(spot, contracts, realized_vol, prior_closes=None, cfg=None):
    """
    short-dte S1–S5 + P4: side from IV vs HAR forecast, structure from path + skew + VIX.

    family is one of short_strangle | short_straddle | iron_condor | put_credit |
    put_debit | call_debit | calendar | None.
    Call *credit* is never a family — ranking put vs call by credit/width sold
    calls into a rally. Cheap IV vs the forecast is a debit, not a skip.
    """
    c = dict(DEFAULTS)
    if cfg:
        c.update(cfg)

    iv = atm_iv(contracts)
    skew = None
    piv, civ = _delta_iv(contracts, 'P'), _delta_iv(contracts, 'C')
    if piv and civ and civ > 0:
        skew = round(piv / civ, 3)

    # P4: IV vs HAR. `rv_forecast` in cfg is the live/engine value; tests that
    # omit the key fall back to injected realized_vol (the fixture IS the forecast).
    if 'rv_forecast' in c:
        rv_f = c.get('rv_forecast')
    else:
        rv_f = None
        ohlc = c.get('ohlc')
        if ohlc and len(ohlc) >= 4:
            rv_f = CANON.har_forecast_rv(ohlc[0], ohlc[1], ohlc[2], ohlc[3],
                                         c.get('har_horizon_days') or CANON.HAR_HORIZON)
        if rv_f is None:
            rv_f = realized_vol

    path_rv = realized_vol or rv_f
    trend, move_sigma = CANON.path_shape(prior_closes, path_rv, c)
    dtes = [x.get('dte') for x in (contracts or []) if x.get('dte') is not None]
    dte = min(dtes) if dtes else None
    c['has_longer_tenor'] = any(d >= 3 for d in dtes)
    portrait = CANON.rank_setup(
        iv, rv_f, dte, trend, move_sigma, skew,
        ohlcv=c.get('ohlcv_metrics'), vix=c.get('vix_context'),
        event_mode=bool(c.get('event_mode')),
        earnings_in_window=bool(c.get('earnings_in_window')),
        cfg=c, enable_debit=c.get('enable_debit', True))
    if move_sigma is not None:
        portrait['move_sigma'] = round(move_sigma, 3)
    portrait['spot'] = spot
    return portrait


def pick_for_setup(contracts, cfg, portrait):
    """Build exactly the family the portrait named. S1 falls back to condor when MAE fails."""
    fam = portrait.get('family')
    if fam == 'calendar':
        x = _select_calendar(contracts, cfg)
        return [x] if x else []
    if fam == 'short_straddle':
        x = _select_straddle(contracts, cfg)
        return [x] if x else []
    if fam == 'short_strangle':
        x = _select_strangle(contracts, cfg)
        mae = (cfg.get('ohlcv_metrics') or {}).get('mae10_p95')
        spot = cfg.get('spot') or portrait.get('spot')
        if x and mae and spot:
            dist = min(abs(x['put_short_strike'] - spot), abs(x['call_short_strike'] - spot))
            ok, _ = CANON.mae_ok(spot, dist, mae)
            if not ok:
                x = _select_condor(contracts, cfg)
        elif x is None:
            x = _select_condor(contracts, cfg)
        return [x] if x else []
    if fam == 'iron_condor':
        x = _select_condor(contracts, cfg)
        return [x] if x else []
    if fam == 'put_credit':
        pcfg = dict(cfg)
        pcfg['min_credit_ratio'] = cfg.get('min_credit_ratio_vertical', 0.08)
        x = _select_vertical(contracts, 'P', pcfg)
        return [x] if x else []
    if fam == 'put_debit':
        x = _select_vertical_debit(contracts, 'P', cfg)
        return [x] if x else []
    if fam == 'call_debit':
        x = _select_vertical_debit(contracts, 'C', cfg)
        return [x] if x else []
    return []


def enrich_with_greeks(chain_rows, spot, today, sessions_to_expiry=None):
    """
    Attach delta and implied vol to each contract by inverting Black-Scholes.

    chain_rows: iterable of dicts with occ, strike, opt_right, expiry, close.
    Rows whose IV cannot be recovered (no time value, price below intrinsic, a
    stale print) are dropped -- they are exactly the contracts we should not trade.

    `sessions_to_expiry(today, expiry) -> int` puts the inversion on TRADING time, the
    same 252 clock the realized-vol forecast uses. Without it the inversion runs on
    calendar days over 365 and a Friday option is read ~30% cheaper than the identical
    Tuesday one, purely from the day count -- see blackscholes.year_fraction. Any rule
    comparing this IV with a forecast then becomes a weekday detector. Callers that
    only compare IV with other IV (the reporting scripts) may leave it None.
    """
    out = []
    for r in chain_rows:
        days = (r['expiry'] - today).days
        if days < 0 or not r.get('close'):
            continue
        sessions = None
        if sessions_to_expiry is not None:
            sessions = sessions_to_expiry(today, r['expiry'])
            if sessions is None or sessions < 0:
                continue
        delta, iv = bs.delta_from_price(r['close'], spot, r['strike'], days,
                                        r['opt_right'], sessions=sessions)
        if delta is None:
            continue
        row = dict(r)
        row['delta'] = delta
        row['iv'] = iv
        row['dte'] = days
        row['sessions_to_expiry'] = sessions
        out.append(row)
    return out


def _right(c):
    return c.get('opt_right') or c.get('right')


def atm_iv(contracts):
    """
    Implied vol AT THE FORWARD for one expiry, from deltas -- no spot or rate needed.

    Why (fix D5): the vol gate used to compare the IV of the short 15-delta PUT with
    realized vol. Index put skew keeps that IV 10-20% above the at-the-money level, so
    the "1.2x" richness test was largely measuring skew, not richness. On the
    2026-08-27 SPY snapshot ATM IV / RV was 1.11 (gate closed) while the 15-delta put
    read 1.35 (gate open).

    Method: the forward is where the call delta crosses 0.50 (equivalently the put
    delta crosses -0.50); IV is linearly interpolated across the two bracketing
    strikes on each side and the two sides are averaged. Works identically on live
    Alpaca Greeks and on Black-Scholes-inverted backtest deltas, so the gate is the
    same function in both worlds. Returns None if either side cannot bracket 0.50.
    """
    vals = []
    for right in ('C', 'P'):
        side = sorted([c for c in contracts if _right(c) == right and c.get('iv')
                       and c.get('delta') is not None], key=lambda c: c['strike'])
        if len(side) < 2:
            continue
        d = [abs(c['delta']) for c in side]        # decreasing in strike for both rights
        found = None
        for i in range(len(side) - 1):
            lo, hi = d[i], d[i + 1]
            if (lo - 0.5) * (hi - 0.5) <= 0 and lo != hi:
                w = (lo - 0.5) / (lo - hi)
                found = side[i]['iv'] + w * (side[i + 1]['iv'] - side[i]['iv'])
                break
        if found is not None and found > 0:
            vals.append(found)
    if not vals:
        return None
    return sum(vals) / len(vals)


def _one_expiry(contracts, cfg):
    """Keep one expiry so mixed-window chains cannot glue a 1d put to a 9d call."""
    today = cfg.get('asof')
    exps = sorted({c['expiry'] for c in contracts if c.get('expiry')})
    if not exps:
        return []
    if today is None:
        return [c for c in contracts if c['expiry'] == exps[0]]
    exp = pick_expiry(exps, today, cfg.get('min_dte', 1), cfg.get('max_dte', 7))
    if exp is None:
        return []
    return [c for c in contracts if c['expiry'] == exp]


def _select_short_leg(contracts, right, cfg, target=None):
    same = [c for c in contracts if c['opt_right'] == right]
    if not same:
        return None
    target = cfg['target_delta'] if target is None else target
    tol = cfg['delta_tolerance'] if target != 0.50 else 0.15
    band = [c for c in same if abs(abs(c['delta']) - target) <= tol]
    if not band:
        return None
    return min(band, key=lambda c: abs(abs(c['delta']) - target))


def _select_vertical(contracts, right, cfg):
    """
    Build one vertical credit spread for a given right.

    Put credit spread  : sell higher strike, buy lower  (bullish/neutral)
    Call credit spread : sell lower strike, buy higher  (bearish/neutral)
    """
    contracts = _one_expiry(contracts, cfg)
    width = wing_width(cfg, contracts)
    same = [c for c in contracts if c['opt_right'] == right]
    if len(same) < 2:
        return None

    target = cfg['target_delta']
    tol = cfg['delta_tolerance']
    band = [c for c in same if abs(abs(c['delta']) - target) <= tol]
    if not band:
        return None

    short = min(band, key=lambda c: abs(abs(c['delta']) - target))

    # The long leg sits `width` further out of the money.
    if right == 'P':
        long_strike = short['strike'] - width
    else:
        long_strike = short['strike'] + width

    by_strike = {c['strike']: c for c in same}
    long = by_strike.get(long_strike)
    if long is None:
        # Strike ladder is not always complete; take the nearest available further-OTM
        # strike instead of silently skipping the day.
        further = [c for c in same
                   if (c['strike'] < short['strike'] if right == 'P' else c['strike'] > short['strike'])]
        if not further:
            return None
        long = min(further, key=lambda c: abs(abs(c['strike'] - short['strike']) - width))

    width = abs(short['strike'] - long['strike'])
    if width <= 0:
        return None

    # Credit as seen on the tape, before friction. fills.py applies friction.
    raw_credit = short['close'] - long['close']
    if raw_credit <= 0:
        return None

    ratio = raw_credit / width
    if not (cfg['min_credit_ratio'] <= ratio <= cfg['max_credit_ratio']):
        return None

    return {
        'right': right,
        'kind': 'put_credit' if right == 'P' else 'call_credit',
        'structure': 'vertical',
        'side': 'credit',
        'short_occ': short['occ'], 'short_strike': short['strike'],
        'short_close': short['close'], 'short_delta': short['delta'], 'short_iv': short['iv'],
        'long_occ': long['occ'], 'long_strike': long['strike'], 'long_close': long['close'],
        'long_delta': long['delta'],
        'width': width,
        'raw_credit': raw_credit,
        'credit_ratio': ratio,
        'expiry': short['expiry'],
        'dte': short['dte'],
    }


def _select_vertical_debit(contracts, right, cfg):
    """
    Defined-risk debit vertical: buy the target-delta option, sell further OTM.

    Put debit  : buy higher strike, sell lower  (bearish)
    Call debit : buy lower strike, sell higher  (bullish)
    Max loss is the debit paid; max profit is width minus debit.
    """
    contracts = _one_expiry(contracts, cfg)
    same = [c for c in contracts if c['opt_right'] == right]
    if len(same) < 2:
        return None
    target = cfg['target_delta']
    tol = cfg['delta_tolerance']
    band = [c for c in same if abs(abs(c['delta']) - target) <= tol]
    if not band:
        return None
    long = min(band, key=lambda c: abs(abs(c['delta']) - target))
    if right == 'P':
        short_strike = long['strike'] - cfg['width']
    else:
        short_strike = long['strike'] + cfg['width']
    by_strike = {c['strike']: c for c in same}
    short = by_strike.get(short_strike)
    if short is None:
        further = [c for c in same
                   if (c['strike'] < long['strike'] if right == 'P' else c['strike'] > long['strike'])]
        if not further:
            return None
        short = min(further, key=lambda c: abs(abs(c['strike'] - long['strike']) - cfg['width']))
    width = abs(long['strike'] - short['strike'])
    if width <= 0:
        return None
    raw_debit = long['close'] - short['close']
    if raw_debit <= 0:
        return None
    ratio = raw_debit / width
    lo = cfg.get('min_credit_ratio_vertical', cfg.get('min_credit_ratio', 0.08))
    hi = cfg.get('max_credit_ratio', 0.60)
    if not (lo <= ratio <= hi):
        return None
    return {
        'right': right,
        'kind': 'put_debit' if right == 'P' else 'call_debit',
        'structure': 'vertical_debit',
        'side': 'debit',
        'short_occ': short['occ'], 'short_strike': short['strike'],
        'short_close': short['close'], 'short_delta': short['delta'], 'short_iv': short['iv'],
        'long_occ': long['occ'], 'long_strike': long['strike'], 'long_close': long['close'],
        'long_delta': long['delta'],
        'width': width,
        'raw_credit': 0.0,
        'raw_debit': raw_debit,
        'credit_ratio': ratio,
        'expiry': long['expiry'],
        'dte': long['dte'],
    }


def _select_strangle(contracts, cfg, atm=False):
    """S1 2-leg short strangle (OTM) or S4 ATM straddle. No longs."""
    contracts = _one_expiry(contracts, cfg)
    target = 0.50 if atm else cfg.get('target_delta', 0.15)
    put = _select_short_leg(contracts, 'P', cfg, target=target)
    call = _select_short_leg(contracts, 'C', cfg, target=target)
    if put is None or call is None:
        return None
    if put['strike'] >= call['strike']:
        return None
    credit = put['close'] + call['close']
    if credit <= 0:
        return None
    width = call['strike'] - put['strike']
    kind = 'short_straddle' if atm else 'short_strangle'
    return {
        'kind': kind, 'structure': kind, 'side': 'credit', 'right': None,
        'short_occ': put['occ'], 'short_strike': put['strike'],
        'short_close': put['close'], 'short_delta': put['delta'], 'short_iv': put['iv'],
        'long_occ': call['occ'], 'long_strike': call['strike'], 'long_close': 0.0,
        'put_short_occ': put['occ'], 'put_short_strike': put['strike'],
        'put_short_close': put['close'],
        'call_short_occ': call['occ'], 'call_short_strike': call['strike'],
        'call_short_close': call['close'],
        'width': width, 'raw_credit': credit, 'credit_ratio': credit / max(width, 0.01),
        'max_loss_unit': 2.0 * credit * 100.0,  # stop is 2× credit; MAE is the entry filter
        'expiry': put['expiry'], 'dte': put['dte'],
    }


def _select_straddle(contracts, cfg):
    return _select_strangle(contracts, cfg, atm=True)


def _select_calendar(contracts, cfg):
    """S3: sell the nearer expiry, buy the next, same strike, both ≤ 12 DTE."""
    max_s3 = int(cfg.get('max_dte_s3') or 12)
    by_exp = {}
    for c in contracts:
        if c.get('dte') is None or not (0 <= c['dte'] <= max_s3):
            continue
        by_exp.setdefault(c['expiry'], []).append(c)
    exps = sorted(by_exp)
    if len(exps) < 2:
        return None
    near, far = exps[0], exps[1]
    if (far - near).days < 1:
        return None

    def atm(rows, right):
        side = [r for r in rows if r['opt_right'] == right and r.get('delta') is not None]
        if not side:
            return None
        return min(side, key=lambda r: abs(abs(r['delta']) - 0.50))

    short = atm(by_exp[near], 'C') or atm(by_exp[near], 'P')
    if short is None:
        return None
    right = short['opt_right']
    far_side = [r for r in by_exp[far] if r['opt_right'] == right]
    if not far_side:
        return None
    long = min(far_side, key=lambda r: abs(r['strike'] - short['strike']))
    net = short['close'] - long['close']
    width = max(abs(short['strike'] - long['strike']), 1.0)
    if net > 0:
        side, raw_credit, raw_debit = 'credit', net, 0.0
        max_loss_unit = 1.5 * long['close'] * 100.0
    elif net < 0:
        side, raw_credit, raw_debit = 'debit', 0.0, -net
        max_loss_unit = 1.5 * raw_debit * 100.0
    else:
        return None
    return {
        'kind': 'calendar', 'structure': 'calendar', 'side': side, 'right': right,
        'short_occ': short['occ'], 'short_strike': short['strike'],
        'short_close': short['close'], 'short_delta': short['delta'], 'short_iv': short['iv'],
        'long_occ': long['occ'], 'long_strike': long['strike'], 'long_close': long['close'],
        'width': width, 'raw_credit': raw_credit, 'raw_debit': raw_debit,
        'credit_ratio': abs(net) / width,
        'max_loss_unit': max_loss_unit,
        'expiry': near, 'far_expiry': far, 'dte': short['dte'],
    }


def _select_condor(contracts, cfg, put_cfg=None, call_cfg=None):
    """
    Build an iron condor: a put credit spread and a call credit spread on one expiry.

    `put_cfg` / `call_cfg` let the caller place the two wings independently -- different
    short deltas, different widths. That is the ASYMMETRY the vertical-vs-condor
    playbook asks for (section 3D): the equity distribution is not symmetric, so a
    structure that is symmetric by construction is mismatched to it. Both default to
    `cfg`, so every existing caller builds exactly the symmetric condor it did before.

    Why this exists: the vertical backtest lost not because options were cheap -- implied
    vol sold averaged 24.5% against 15.8% realised, so the premium was genuinely there --
    but because candidate ranking by credit/width kept picking the CALL side in a market
    that rose 56%. Directional exposure swamped the vol premium.

    Selling both wings cancels most of that directional exposure, and because the
    underlying can finish through only one wing, the risk is the wider wing less the
    TOTAL credit. Collecting two credits against one wing's width is what drops the
    break-even win rate: at 0.73 per side on 5-wide wings, from 85.4% to 70.8%.
    """
    # Select each wing WITHOUT the credit-ratio filter. A condor's economics depend on
    # the COMBINED credit against one wing's width, so judging each wing independently
    # rejects perfectly good structures -- a 0.08 wing is fine when the other brings the
    # total to 0.20. Applying the filter per-wing produced literally zero trades over
    # 2.5 years. The combined test below is the one that matters.
    def _wing(base):
        w = dict(base)
        w['min_credit_ratio'] = 0.0
        w['max_credit_ratio'] = 1.0
        return w

    put = _select_vertical(contracts, 'P', _wing(put_cfg or cfg))
    call = _select_vertical(contracts, 'C', _wing(call_cfg or cfg))
    if put is None or call is None:
        return None

    # Both wings must share an expiry, or it is not a condor and the risk does not net.
    if put['expiry'] != call['expiry']:
        return None
    # Wings must not overlap -- the short strikes have to straddle the underlying.
    if put['short_strike'] >= call['short_strike']:
        return None

    total_credit = put['raw_credit'] + call['raw_credit']
    risk_width = max(put['width'], call['width'])
    ratio = total_credit / risk_width
    if not (cfg['min_credit_ratio'] <= ratio <= cfg['max_credit_ratio']):
        return None

    return {
        'kind': 'iron_condor',
        'structure': 'condor',
        'side': 'credit',
        'right': None,
        'put': put,
        'call': call,
        'short_occ': put['short_occ'], 'long_occ': put['long_occ'],
        'short_strike': put['short_strike'], 'long_strike': put['long_strike'],
        'call_short_occ': call['short_occ'], 'call_long_occ': call['long_occ'],
        'call_short_strike': call['short_strike'], 'call_long_strike': call['long_strike'],
        'short_close': put['short_close'], 'long_close': put['long_close'],
        'call_short_close': call['short_close'], 'call_long_close': call['long_close'],
        'short_delta': put['short_delta'], 'call_short_delta': call['short_delta'],
        'short_iv': put['short_iv'],
        # net_delta is the point of the structure -- near zero means direction-neutral.
        'net_delta': put['short_delta'] + call['short_delta'],
        'width': risk_width,
        'put_width': put['width'], 'call_width': call['width'],
        'raw_credit': total_credit,
        'credit_ratio': ratio,
        'expiry': put['expiry'],
        'dte': put['dte'],
    }


def generate_candidates(chain_rows, spot, today, cfg=None, realized_vol=None, stats=None):
    """
    Every tradable structure for one day, best first.

    `realized_vol` is the annualized trailing realized volatility of the underlying,
    computed from sessions strictly before `today`. When cfg['min_iv_rv_ratio'] is
    set it gates entry: sell only when the implied vol being sold is at least that
    multiple of realized. Passing None while the filter is configured means the
    caller could not compute it, and the correct response is to trade nothing rather
    than to trade blind.
    """
    c = dict(DEFAULTS)
    if cfg:
        c.update(cfg)
    if spot:
        c = dict(c)
        c['width'] = dollar_width(spot, c)
        c['spot'] = spot
    c['asof'] = today
    c['_rv_geometry'] = c.get('rv_forecast') or realized_vol

    # A caller that has already inverted this chain (the engine caches it per
    # underlying-day, since the inversion depends on the data and not on any parameter
    # being swept) hands the result in rather than paying for it again.
    enriched = c.get('_enriched')
    if enriched is None:
        enriched = enrich_with_greeks(chain_rows, spot, today,
                                      sessions_to_expiry=c.get('sessions_to_expiry'))
    if stats is not None:
        # How far the funnel got. The engine uses this to distinguish "the greeks
        # inversion produced nothing" (a data or units failure) from "every structure
        # was rejected by a gate" (a decision). Both look like zero trades from outside.
        stats['rows'] = stats.get('rows', 0) + len(list(chain_rows) if not isinstance(chain_rows, list) else chain_rows)
        stats['enriched'] = stats.get('enriched', 0) + len(enriched)
    if not enriched:
        return []

    # The vol gate is applied to the FINISHED structure, not to the contract pool.
    # Filtering contracts first would distort which strikes the delta band selects
    # from -- a different and worse filter. The question being asked is "is the vol
    # I would be selling rich right now", which is a property of the trade.
    vol_floor = None
    playbook = c.get('structure') == 'playbook'
    adaptive = c.get('structure') in ('adaptive', 'best', 'picker', 'portrait') or playbook
    # Portrait already gates on ATM IV vs RV. Do not also require the short-put
    # 1.2x bar — that is the always-condor book, and it silently kills puts.
    if c.get('min_iv_rv_ratio') and not adaptive:
        if not realized_vol or realized_vol <= 0:
            return []
        vol_floor = c['min_iv_rv_ratio'] * realized_vol

    if playbook:
        # The router does its own per-side gating, so the blanket vol floor above is
        # bypassed for the same reason the portrait path bypasses it.
        rv_f = c.get('rv_forecast') or realized_vol
        trend, _move = CANON.path_shape(c.get('prior_closes'), rv_f, c)
        out, decision = PB.select(enriched, c, spot, rv_f, trend,
                                  metrics=c.get('ohlcv_metrics'),
                                  event=bool(c.get('event_mode') or c.get('earnings_in_window')))
        for sp in out:
            sp['setup_trend'] = trend
            sp['portrait'] = sp.get('playbook')
        if stats is not None and not out:
            stats['playbook_pass'] = stats.get('playbook_pass', 0) + 1
            stats.setdefault('playbook_reasons', {})
            r = decision.get('reason')
            stats['playbook_reasons'][r] = stats['playbook_reasons'].get(r, 0) + 1
    elif adaptive:
        portrait = classify_setup(spot, enriched, realized_vol, c.get('prior_closes'), c)
        out = pick_for_setup(enriched, c, portrait)
        for sp in out:
            sp['portrait'] = portrait.get('reason')
            sp['setup_trend'] = portrait.get('trend')
            sp['setup_iv_rv'] = portrait.get('iv_rv')
            sp['setup_rank'] = portrait.get('rank')
            sp['side'] = sp.get('side') or ('debit' if sp.get('kind', '').endswith('_debit') else 'credit')
        mae = (c.get('ohlcv_metrics') or {}).get('mae10_p95')
        if mae and spot:
            kept = []
            for sp in out:
                if sp.get('structure') not in ('short_strangle', 'short_straddle'):
                    kept.append(sp)
                    continue
                dist = min(abs(sp.get('put_short_strike', sp['short_strike']) - spot),
                           abs(sp.get('call_short_strike', sp['short_strike']) - spot))
                ok, why = CANON.mae_ok(spot, dist, mae)
                sp['mae_gate'] = why
                if ok:
                    kept.append(sp)
            out = kept
    elif c.get('structure') == 'condor':
        condor = _select_condor(enriched, c)
        out = [condor] if condor else []
    else:
        # Non-adaptive vertical must use the single-wing floor. Applying the condor
        # combined bar (0.15) to one wing was why the put-vertical diagnostic produced
        # three trades -- min_credit_ratio_vertical (0.08) only fired on adaptive put_credit.
        rights = {'put': ['P'], 'call': ['C'], 'both': ['P', 'C']}[c['side']]
        vcfg = dict(c)
        if c.get('structure') == 'vertical':
            vcfg['min_credit_ratio'] = c.get(
                'min_credit_ratio_vertical', c.get('min_credit_ratio', 0.08))
        out = [sp for sp in (_select_vertical(enriched, r, vcfg) for r in rights) if sp]
        out.sort(key=lambda sp: sp['credit_ratio'], reverse=True)

    # The IV the gate compares with realized vol. 'short_leg' (default) is the short
    # put's IV -- the incumbent that produced the published result. 'atm' is the
    # at-the-forward level (atm_iv); it was tested on 2026-08-28 and did not beat the
    # incumbent (see agent_rules.json _why_vol_gate_iv), so it stays opt-in.
    iv_mode = c.get('vol_gate_iv', 'short_leg')
    iv_gate = atm_iv(enriched) if iv_mode == 'atm' else None
    for sp in out:
        sp['underlying'] = c.get('underlying')
        sp['iv_atm'] = iv_gate
        sp['gate_iv'] = iv_gate if iv_mode == 'atm' else sp.get('short_iv')
        sp['iv_rv_ratio'] = (round(sp['gate_iv'] / realized_vol, 3)
                             if sp['gate_iv'] and realized_vol else None)

    if vol_floor is not None:
        out = [sp for sp in out if sp.get('gate_iv') and sp['gate_iv'] >= vol_floor]

    # Second gate: overnight variance share. None / missing metrics = no filter.
    # Applied after IV/RV so the two questions stay separable in the journal.
    max_on = c.get('max_overnight_share')
    if max_on is not None and max_on != '':
        max_on = float(max_on)
        mets = c.get('ohlcv_metrics') or {}
        share = mets.get('overnight_share')
        if share is None:
            out = []  # cannot verify overnight load -> do not trade blind
        else:
            kept = []
            for sp in out:
                sp['overnight_share'] = round(share, 4)
                if share <= max_on:
                    kept.append(sp)
            out = kept

    # Cycle-2 gate: IV / HAR forecast floor. null = off. Uses cfg['rv_forecast']
    # (engine fills it via ohlc_before); missing forecast -> no trade.
    min_ih = c.get('min_iv_har_ratio')
    if min_ih is not None and min_ih != '':
        min_ih = float(min_ih)
        har = c.get('rv_forecast')
        if har is None or har <= 0:
            out = []
        else:
            kept = []
            for sp in out:
                iv = sp.get('gate_iv') or sp.get('short_iv')
                ratio = (iv / har) if iv else None
                sp['iv_har_ratio'] = round(ratio, 3) if ratio else None
                if ratio is not None and ratio >= min_ih:
                    kept.append(sp)
            out = kept

    return out


def exit_rules(cfg=None):
    """Mechanical exits. No discretion, and none of it reachable by a model."""
    c = dict(DEFAULTS)
    if cfg:
        c.update(cfg)
    return {
        'take_profit_pct': c.get('take_profit_pct', 0.50),  # close at 50% of credit captured
        'stop_loss_mult': c.get('stop_loss_mult', 2.0),     # stop at 2x credit received
        'close_at_dte': c.get('close_at_dte', 0),           # 0 == hold to expiry
    }


if __name__ == "__main__":
    today = dt.date(2026, 8, 25)
    expiry = dt.date(2026, 8, 28)
    spot = 766.71

    # Synthesise a chain from Black-Scholes so the selector has something realistic.
    rows = []
    for strike in range(730, 805):
        for right in ('P', 'C'):
            t = bs.year_fraction((expiry - today).days)
            px = bs.price(spot, strike, t, 0.16, right)
            if px < 0.02:
                continue
            rows.append({'occ': f"SPY{expiry:%y%m%d}{right}{int(strike*1000):08d}",
                         'strike': float(strike), 'opt_right': right,
                         'expiry': expiry, 'close': round(px, 2)})

    cands = generate_candidates(rows, spot, today, {'width': 5.0, 'min_credit_ratio': 0.05})
    assert cands, "no candidates generated"
    for s in cands:
        assert 0.10 <= abs(s['short_delta']) <= 0.30, s['short_delta']
        assert s['width'] > 0 and s['raw_credit'] > 0
        print(f"  {s['kind']:<12} short {s['short_strike']:.0f} "
              f"(delta {s['short_delta']:+.3f}, iv {s['short_iv']:.3f})  "
              f"long {s['long_strike']:.0f}  width {s['width']:.0f}  "
              f"credit {s['raw_credit']:.2f}  ratio {s['credit_ratio']:.2f}")

    chop = [760 + i * 0.2 for i in range(21)]
    up = [700 + i * 3.0 for i in range(21)]
    ad_cfg = {'structure': 'adaptive', 'width': 5.0, 'min_credit_ratio': 0.05,
              'min_credit_ratio_vertical': 0.05, 'min_iv_rv_ratio': 1.2,
              'target_delta': 0.15, 'delta_tolerance': 0.08, 'prior_closes': chop}
    ad = generate_candidates(rows, spot, today, ad_cfg, realized_vol=0.12)
    assert len(ad) == 1 and ad[0]['kind'] == 'short_strangle', [s.get('kind') for s in ad]
    ad_cfg['prior_closes'] = up
    ad_up = generate_candidates(rows, spot, today, ad_cfg, realized_vol=0.12)
    assert len(ad_up) == 1 and ad_up[0]['kind'] == 'put_credit', [s.get('kind') for s in ad_up]
    ad_cfg['prior_closes'] = chop
    cheap = generate_candidates(rows, spot, today, ad_cfg, realized_vol=0.30)
    assert not cheap, 'cheap IV + chop must not sell premium'
    ad_cfg['prior_closes'] = up
    cheap_up = generate_candidates(rows, spot, today, ad_cfg, realized_vol=0.30)
    assert len(cheap_up) == 1 and cheap_up[0]['kind'] == 'call_debit', [s.get('kind') for s in cheap_up]
    print("  portrait          chop->strangle  uptrend->put  cheap chop->skip  cheap up->call debit")

    event_one = generate_candidates(rows, spot, today, dict(ad_cfg, event_mode=True),
                                    realized_vol=0.12)
    assert not event_one, 'S3 with one expiry must not open'
    far = dt.date(2026, 9, 4)
    cal_rows = list(rows)
    for strike in range(730, 805):
        for right in ('P', 'C'):
            t = bs.year_fraction((far - today).days)
            px = bs.price(spot, strike, t, 0.16, right)
            if px < 0.02:
                continue
            cal_rows.append({'occ': f"SPY{far:%y%m%d}{right}{int(strike*1000):08d}",
                             'strike': float(strike), 'opt_right': right,
                             'expiry': far, 'close': round(px, 2)})
    cal = generate_candidates(cal_rows, spot, today, dict(ad_cfg, event_mode=True),
                              realized_vol=0.12)
    assert len(cal) == 1 and cal[0]['kind'] == 'calendar', [s.get('kind') for s in cal]
    print("  S3 calendar       one expiry -> skip  two expiries -> calendar")

    e = exit_rules()
    assert e['take_profit_pct'] == 0.50 and e['stop_loss_mult'] == 2.0

    # ATM IV must sit at the forward, not on the skewed put wing.
    enriched = enrich_with_greeks(rows, spot, today)
    iv = atm_iv(enriched)
    assert iv is not None and abs(iv - 0.16) < 0.01, iv
    # A skewed chain: put IV rises 0.2 vol points per dollar below spot (index-like
    # skew). The gate IV must sit near the 16% ATM level, not on the rich put wing.
    skewed = []
    for strike in range(730, 805):
        for right in ('P', 'C'):
            t = bs.year_fraction((expiry - today).days)
            sigma = 0.16 + (0.002 * max(0.0, spot - strike) if right == 'P' else 0.0)
            px = bs.price(spot, strike, t, sigma, right)
            if px < 0.02:
                continue
            skewed.append({'occ': f"SPY{expiry:%y%m%d}{right}{int(strike*1000):08d}",
                           'strike': float(strike), 'opt_right': right,
                           'expiry': expiry, 'close': round(px, 2)})
    enriched_skew = enrich_with_greeks(skewed, spot, today)
    iv_skew = atm_iv(enriched_skew)
    puts_15d = [c for c in enriched_skew if c['opt_right'] == 'P' and 0.10 <= abs(c['delta']) <= 0.20]
    assert puts_15d and min(c['iv'] for c in puts_15d) > iv_skew + 0.01, \
        "15d put IV should exceed ATM IV under skew"
    assert abs(iv_skew - 0.16) < 0.015, iv_skew

    # Non-adaptive vertical must use min_credit_ratio_vertical, not the condor combined bar.
    # A wing with credit/width in (0.08, 0.15) must pass vertical and fail when forced to 0.15.
    v_lo = generate_candidates(
        rows, spot, today,
        {'structure': 'vertical', 'side': 'put', 'width': 5.0,
         'min_credit_ratio': 0.15, 'min_credit_ratio_vertical': 0.08,
         'min_iv_rv_ratio': None, 'target_delta': 0.20, 'delta_tolerance': 0.15})
    v_hi = generate_candidates(
        rows, spot, today,
        {'structure': 'vertical', 'side': 'put', 'width': 5.0,
         'min_credit_ratio': 0.15, 'min_credit_ratio_vertical': 0.15,
         'min_iv_rv_ratio': None, 'target_delta': 0.20, 'delta_tolerance': 0.15})
    assert v_lo, "vertical with floor 0.08 must find a put wing on the synthetic chain"
    # With both floors at 0.15 the set is a subset; the point of the fix is that 0.08 is used.
    assert all(0.08 <= s['credit_ratio'] for s in v_lo), [s['credit_ratio'] for s in v_lo]
    print(f"  vertical floor     min_credit_ratio_vertical 0.08 -> {len(v_lo)} put(s); "
          f"0.15 floor -> {len(v_hi)} put(s)")

    print("strategy.py self-check OK")
