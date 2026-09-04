"""
The vertical-vs-condor router, from `iron_condor_vs_vertical_spreads_playbook.md`.

THE CLAIM THIS IMPLEMENTS. An iron condor is a bull put spread plus a bear call spread,
so its expected value is the sum of theirs less four-leg execution cost:

    EV_condor = EV_put + EV_call - four_leg_cost

It follows that a condor whose call side has no edge is a good put spread diluted by a
bad one. The playbook's rule is therefore: trade a condor only when you would open BOTH
component spreads as standalone trades. When one side is mispriced, trade that vertical
alone. When neither is, pass.

The incumbent book on this store does not ask that question. `_select_condor` accepts a
structure whenever the COMBINED credit/width lands in range, so a condor is built on days
when only one wing carries the premium.

WHAT THIS IS NOT. `_select_condor` records that applying the CREDIT-RATIO filter per wing
"produced literally zero trades over 2.5 years". That is a different test and this module
must not become it: the per-side test here is whether that side's implied vol is rich
against a horizon-matched forecast and its short strike clears the measured tail --
`edge = IV_side / rv_forecast`, plus `mae_ok`. Credit adequacy is still judged on the
finished structure, exactly as before. If trade count collapses anyway, the gate has
become the credit filter in disguise and that is the finding, not a threshold to loosen.

DELIBERATELY NOT IMPLEMENTED: unequal notionals per side. The playbook lists it under
section 3D, but `fills.condor_max_loss` and `engine.Position` both assume one wing can
finish in the money and that risk is the wider wing less the TOTAL credit. Unequal
quantities break that arithmetic, so max loss and therefore position sizing would be
silently wrong. That is a fill-model change, not a routing change. Strike and width
asymmetry ARE implemented, and they are the axes that place the structure.

Asymmetry is declared, never fitted: `put_delta`/`call_delta` and `put_width`/`call_width`
default to the symmetric values, so the default behaviour is unchanged and any asymmetry
is a pre-registered configuration rather than a number tuned on results.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(
    os.path.abspath(os.path.join(os.path.dirname(__file__), '..')), 'agent'))

import canon as CANON

# Section 4's matrix, as thresholds. Both come from canon so the router and the
# incumbent portrait cannot disagree about what "rich" and "cheap" mean.
K_SHORT = CANON.K_SHORT      # IV/forecast at or above this -> that side is sellable
K_LONG = CANON.K_LONG        # at or below this -> IV is cheap, a debit is the trade

FAMILIES = ('iron_condor', 'put_credit', 'call_credit', 'put_debit', 'call_debit', None)


def side_cfg(cfg, right):
    """
    Per-side strike geometry. This is the whole of the asymmetry mechanism.

    `_select_vertical` reads target_delta / delta_tolerance / width off the cfg it is
    given, so handing it two different cfgs is all it takes to place the wings
    independently -- no change to strike selection itself.
    """
    out = dict(cfg)
    pre = 'put' if right == 'P' else 'call'
    out['target_delta'] = float(cfg.get(f'{pre}_delta') or cfg['target_delta'])
    if cfg.get(f'{pre}_width'):
        out['width'] = float(cfg[f'{pre}_width'])
    return out


def side_edge(contracts, right, cfg, rv_forecast):
    """
    That side's implied vol against the forecast: the playbook's IV_down / IV_up test.

    Returns (edge, iv). None when the side cannot be priced -- which is a fail, never a
    pass: an unmeasurable side is not an edge, and the default outcome is NO_TRADE.
    """
    from strategy import _delta_iv
    if not rv_forecast or rv_forecast <= 0:
        return None, None
    target = float(side_cfg(cfg, right)['target_delta'])
    iv = _delta_iv(contracts, right, target)
    if not iv or iv <= 0:
        return None, None
    return iv / rv_forecast, iv


def side_ok(contracts, right, cfg, rv_forecast, spot, mae10_p95):
    """
    Would this side be worth opening on its own? (edge test + measured tail test)

    Returns (ok, detail). The tail test is `canon.mae_ok`, the same one the strangle
    path already uses, so "far enough out" means one thing in this repo.
    """
    edge, iv = side_edge(contracts, right, cfg, rv_forecast)
    d = {'right': right, 'edge': round(edge, 3) if edge else None,
         'iv': round(iv, 4) if iv else None,
         'required': float(cfg.get('min_iv_rv_ratio') or K_SHORT)}
    if edge is None:
        d['reason'] = 'no_side_iv_or_forecast'
        return False, d
    if edge < d['required']:
        d['reason'] = 'iv_not_rich_enough'
        return False, d
    # Tail: place a provisional short at this side's delta and ask whether the
    # underlying's own measured 10-day excursion overruns it.
    from strategy import _select_vertical
    # The probe exists ONLY to locate this side's short strike for the tail test, so
    # it must not apply the credit-ratio filter. Letting it through would make the
    # per-side gate the very credit filter this module's docstring warns about --
    # the one _select_condor records as having produced zero trades over 2.5 years.
    # Credit adequacy is still judged on the finished structure, as before.
    probe_cfg = side_cfg(cfg, right)
    probe_cfg['min_credit_ratio'], probe_cfg['max_credit_ratio'] = 0.0, 1.0
    probe = _select_vertical(contracts, right, probe_cfg)
    if probe is None:
        d['reason'] = 'no_buildable_vertical'
        return False, d
    dist = abs(probe['short_strike'] - spot) if spot else None
    ok, why = CANON.mae_ok(spot, dist, mae10_p95)
    d['mae'] = why
    d['short_strike'] = probe['short_strike']
    if not ok:
        d['reason'] = f'mae_{why}'
        return False, d
    d['reason'] = 'ok'
    return True, d


def route(contracts, cfg, spot, rv_forecast, trend, metrics=None, event=False):
    """
    Section 10's router. One family, or None with the binding reason named.

    Order matters and follows the playbook: the event gate first, then direction, then
    the both-sides condor test, then the debit case, then pass.

    The event branch routes to a VERTICAL or a PASS -- never to a calendar. That is the
    one place the incumbent `canon.rank_setup` fails: its event branch short-circuits to
    `calendar` before anything else is considered, which is how 63.4% of that book's
    entries became calendars and why it returned -74%.
    """
    m = metrics or {}
    mae = m.get('mae10_p95')
    out = {'family': None, 'reason': 'unclassified', 'trend': trend,
           'rv_forecast': round(rv_forecast, 4) if rv_forecast else None,
           'event': bool(event)}

    put_ok, put_d = side_ok(contracts, 'P', cfg, rv_forecast, spot, mae)
    call_ok, call_d = side_ok(contracts, 'C', cfg, rv_forecast, spot, mae)
    out['put'], out['call'] = put_d, call_d

    # Section 4: "Pre-earnings -- generic condor prohibited." A single vertical on the
    # side that is rich is still allowed; only the two-sided structure is barred,
    # because an event invalidates a RANGE thesis specifically.
    if event:
        out['condor_barred'] = 'event_in_window'

    put_edge = put_d.get('edge')
    call_edge = call_d.get('edge')
    cheap = [e for e in (put_edge, call_edge) if e is not None]
    k_long = float(cfg.get('k_long') or K_LONG)

    if trend == 'up' and put_ok:
        out.update(family='put_credit', reason='bullish_and_downside_iv_rich')
    elif trend == 'down' and call_ok:
        out.update(family='call_credit', reason='bearish_and_upside_iv_rich')
    elif trend == 'chop' and put_ok and call_ok and not event:
        out.update(family='iron_condor', reason='range_and_both_tails_rich')
    elif trend == 'chop' and (put_ok or call_ok):
        # Range regime but only one tail pays. The playbook is explicit: trade the one
        # vertical rather than diluting it with a side that has no edge.
        out.update(family='put_credit' if put_ok else 'call_credit',
                   reason='range_but_only_one_side_rich')
    elif cfg.get('enable_debit') and cheap and min(cheap) <= k_long and trend in ('up', 'down'):
        out.update(family='call_debit' if trend == 'up' else 'put_debit',
                   reason='strong_trend_and_iv_cheap')
    else:
        # Name the reason the sides ACTUALLY failed. Reporting "no side rich enough"
        # when both sides were rich but unbuildable is a false statement about the
        # decision -- and it is what the live trace caught on 2026-09-01, where edges
        # of 1.8-5.5x were summarised as insufficient richness.
        if put_ok or call_ok:
            out['reason'] = 'direction_and_richness_disagree'
        else:
            reasons = {put_d.get('reason'), call_d.get('reason')}
            reasons.discard('ok')
            out['reason'] = ('both_sides_' + sorted(reasons)[0] if len(reasons) == 1
                             else 'no_side_tradable:' + '/'.join(sorted(reasons)))
    return out


def select(contracts, cfg, spot, rv_forecast, trend, metrics=None, event=False):
    """
    Route, then build exactly the family named. Returns (structures, decision).

    `structures` is a list of 0 or 1, matching what `pick_for_setup` returns, so the
    caller's loop is unchanged.
    """
    from strategy import _select_condor, _select_vertical, _select_vertical_debit
    d = route(contracts, cfg, spot, rv_forecast, trend, metrics, event)
    fam = d['family']
    built = None

    if fam == 'iron_condor':
        # Per-side cfgs are what make it asymmetric.
        built = _select_condor(contracts, cfg,
                               put_cfg=side_cfg(cfg, 'P'), call_cfg=side_cfg(cfg, 'C'))
    elif fam in ('put_credit', 'call_credit'):
        right = 'P' if fam == 'put_credit' else 'C'
        vcfg = side_cfg(cfg, right)
        # A single vertical is judged on the single-wing floor, not the condor's
        # combined bar -- the same distinction generate_candidates already makes.
        vcfg['min_credit_ratio'] = cfg.get('min_credit_ratio_vertical',
                                           cfg.get('min_credit_ratio', 0.08))
        built = _select_vertical(contracts, right, vcfg)
    elif fam in ('put_debit', 'call_debit'):
        built = _select_vertical_debit(contracts, 'P' if fam == 'put_debit' else 'C', cfg)

    if built is None:
        if fam:
            d['reason'] = f'{fam}_not_buildable'
        return [], d
    built['playbook'] = d['reason']
    built['playbook_family'] = fam
    built['put_edge'] = d['put'].get('edge')
    built['call_edge'] = d['call'].get('edge')
    return [built], d


if __name__ == "__main__":
    # Offline: a synthetic chain with per-side IV set by hand, so each branch of the
    # router is exercised against a known answer rather than against market data.
    import datetime as _dt

    def chain(spot=100.0, put_iv=0.30, call_iv=0.30, dte=5, step=1.0, n=30):
        """Strikes either side of spot; IV constant within a side so edge is exact."""
        rows = []
        for i in range(-n, n + 1):
            k = round(spot + i * step, 2)
            if k <= 0:
                continue
            dist = (k - spot) / spot
            for right in ('P', 'C'):
                # Delta and price approximated linearly in moneyness -- enough for
                # strike SELECTION and for a credit to exist, which is all these
                # fixtures test. |delta| must FALL as the strike moves out of the
                # money, and the nearer leg must be worth more than the further one,
                # or no credit spread can be built at all.
                mag = 0.5 + dist * 6 if right == 'P' else 0.5 - dist * 6
                mag = max(0.01, min(0.99, mag))
                px = max(0.05, round(2.0 + (dist if right == 'P' else -dist) * spot * 0.15, 2))
                rows.append({'occ': f'X{right}{k}', 'underlying': 'X', 'strike': k,
                             'opt_right': right, 'right': right,
                             'expiry': _dt.date(2026, 1, 9), 'dte': dte,
                             'close': px, 'mid': px, 'bid': px * 0.97, 'ask': px * 1.03,
                             'iv': put_iv if right == 'P' else call_iv,
                             'delta': -mag if right == 'P' else mag})
        return rows

    CFG = {'target_delta': 0.15, 'delta_tolerance': 0.10, 'width': 5.0,
           'min_credit_ratio': 0.15, 'max_credit_ratio': 0.60,
           'min_credit_ratio_vertical': 0.08, 'min_iv_rv_ratio': 1.2,
           'spot': 100.0, 'asof': _dt.date(2026, 1, 5), 'enable_debit': True}
    RV = 0.20                       # forecast: 1.2x bar is IV >= 0.24

    # --- the both-sides rule, which is the playbook's central claim ---------------
    # Both tails rich + range regime -> condor.
    d = route(chain(put_iv=0.30, call_iv=0.30), CFG, 100.0, RV, 'chop')
    assert d['family'] == 'iron_condor', d
    assert d['put']['edge'] == 1.5 and d['call']['edge'] == 1.5, d

    # ONLY the put side rich, still a range regime -> the single vertical, NOT a
    # condor. This is the case the incumbent book gets wrong: it would build a condor
    # because the COMBINED credit is adequate.
    d = route(chain(put_iv=0.30, call_iv=0.18), CFG, 100.0, RV, 'chop')
    assert d['family'] == 'put_credit', d
    assert d['reason'] == 'range_but_only_one_side_rich', d
    assert d['call']['reason'] == 'iv_not_rich_enough', d

    # Only the call side rich -> the call vertical.
    d = route(chain(put_iv=0.18, call_iv=0.30), CFG, 100.0, RV, 'chop')
    assert d['family'] == 'call_credit', d

    # Neither side rich -> PASS. NO_TRADE is the default, not a fallback, and the
    # reason names WHY the sides failed rather than asserting a generic one -- a
    # summary that says "not rich enough" over sides that were rich but unbuildable
    # is a false statement about the decision.
    d = route(chain(put_iv=0.18, call_iv=0.18), CFG, 100.0, RV, 'chop')
    assert d['family'] is None, d
    assert d['reason'] == 'both_sides_iv_not_rich_enough', d

    # --- direction routing --------------------------------------------------------
    d = route(chain(put_iv=0.30, call_iv=0.30), CFG, 100.0, RV, 'up')
    assert d['family'] == 'put_credit', d          # bullish + rich downside
    d = route(chain(put_iv=0.30, call_iv=0.30), CFG, 100.0, RV, 'down')
    assert d['family'] == 'call_credit', d         # bearish + rich upside

    # Cheap IV + a trend -> debit, in the direction of the trend.
    d = route(chain(put_iv=0.15, call_iv=0.15), CFG, 100.0, RV, 'up')
    assert d['family'] == 'call_debit', d
    d = route(chain(put_iv=0.15, call_iv=0.15), CFG, 100.0, RV, 'down')
    assert d['family'] == 'put_debit', d
    # ... and never when debit is disabled.
    off = dict(CFG); off['enable_debit'] = False
    assert route(chain(put_iv=0.15, call_iv=0.15), off, 100.0, RV, 'up')['family'] is None

    # --- the event gate bars the CONDOR, not the vertical -------------------------
    # This is the specific failure of the incumbent portrait path, which short-circuits
    # every event session to a calendar regardless of anything else.
    d = route(chain(put_iv=0.30, call_iv=0.30), CFG, 100.0, RV, 'chop', event=True)
    assert d['family'] == 'put_credit', d          # a vertical, NOT a calendar
    assert d.get('condor_barred') == 'event_in_window', d

    # --- an unmeasurable side is a FAIL, never a pass ------------------------------
    assert route(chain(), CFG, 100.0, None, 'chop')['family'] is None
    d = route(chain(put_iv=0.30, call_iv=0.30), CFG, 100.0, RV, 'chop',
              metrics={'mae10_p95': 0.50})        # tail overruns any strike here
    assert d['family'] is None, d
    assert 'mae' in d['put']['reason'], d['put']
    assert 'mae' in d['reason'], d          # and the summary says so too

    # A side that is RICH but unbuildable must not be reported as insufficiently rich.
    # Live on 2026-09-01 this misreported edges of 1.8-5.5x as 'no_side_rich_enough'.
    # A width that is merely huge is not enough: _select_vertical falls back to the
    # nearest further-OTM strike. Put the SHORT out of reach instead -- an empty delta
    # band -- while _delta_iv still prices the side, so it is rich and unbuildable.
    narrow = dict(CFG); narrow['target_delta'] = 0.95; narrow['delta_tolerance'] = 0.001
    d = route(chain(put_iv=0.30, call_iv=0.30), narrow, 100.0, RV, 'chop')
    assert d['family'] is None, d
    assert d['put']['edge'] == 1.5, d['put']           # the side WAS rich
    assert 'rich' not in d['reason'], d                # ... so do not say otherwise

    # --- asymmetry is real, and off by default ------------------------------------
    sym = side_cfg(CFG, 'P'), side_cfg(CFG, 'C')
    assert sym[0]['target_delta'] == sym[1]['target_delta'] == 0.15
    asym = dict(CFG); asym.update(put_delta=0.20, call_delta=0.10,
                                  put_width=5.0, call_width=8.0)
    p_cfg, c_cfg = side_cfg(asym, 'P'), side_cfg(asym, 'C')
    assert (p_cfg['target_delta'], p_cfg['width']) == (0.20, 5.0), p_cfg
    assert (c_cfg['target_delta'], c_cfg['width']) == (0.10, 8.0), c_cfg

    # And it reaches the built structure: an asymmetric condor's wings must differ.
    built, dec = select(chain(put_iv=0.30, call_iv=0.30), asym, 100.0, RV, 'chop')
    assert dec['family'] == 'iron_condor', dec
    if built:
        b = built[0]
        pw = abs(b['short_strike'] - b['long_strike'])
        cw = abs(b['call_short_strike'] - b['call_long_strike'])
        assert pw != cw, f"asymmetric widths did not reach the structure: {pw} vs {cw}"
        print(f"  asymmetric condor: put wing {pw} @ {b['short_strike']}, "
              f"call wing {cw} @ {b['call_short_strike']}")

    print("  routing table: both-rich->condor, one-rich->that vertical, "
          "none->PASS, cheap+trend->debit, event->vertical not calendar")
    print("playbook.py self-check OK")
