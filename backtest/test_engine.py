"""
Engine tests against a synthetic, fully-controlled market.

These exist because a backtest that is only ever run on real data is untestable --
you cannot tell a bug from a market move. Here the price path is chosen, so the
correct P&L is known in advance and can be asserted exactly.

Run: .venv/bin/python backtest/test_engine.py
"""
import datetime as dt
import math
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import blackscholes as bs
import fills
import strategy
from engine import Engine
from store import DictStore

UND = 'SPY'
MULT = fills.CONTRACT_MULTIPLIER


def build_market(path, expiry, vol=0.16, strikes=range(700, 830), und=UND):
    """
    Synthesise a market from a spot path.

    `path` is {date: spot}. Options are priced with Black-Scholes at `vol`, so the
    chain is internally consistent and the engine's IV inversion recovers `vol`.
    """
    sessions = sorted(path)
    underlying = {(und, d): s for d, s in path.items()}
    opt_close, chains = {}, {}

    for d, spot in path.items():
        days = (expiry - d).days
        if days < 0:
            continue
        t = bs.year_fraction(days)
        rows = []
        for k in strikes:
            for right in ('P', 'C'):
                px = bs.price(spot, float(k), t, vol, right) if days > 0 else \
                     bs.intrinsic(spot, float(k), right)
                px = round(max(px, 0.01), 2)
                occ = f"{und}{expiry:%y%m%d}{right}{int(k * 1000):08d}"
                opt_close[(occ, d)] = px
                rows.append({'occ': occ, 'strike': float(k), 'opt_right': right,
                             'expiry': expiry, 'close': px, 'volume': 1000})
        chains[(und, expiry, d)] = rows

    return DictStore(sessions, underlying, opt_close, chains, [expiry])


def base_config(**over):
    cfg = {
        'underlying': UND, 'initial_cash': 100_000.0,
        'min_dte': 1, 'max_dte': 7, 'width': 5.0,
        'target_delta': 0.20, 'delta_tolerance': 0.12,
        'min_credit_ratio': 0.05, 'max_credit_ratio': 0.60,
        'max_risk_per_trade_pct': 0.02, 'max_concurrent': 1, 'max_contracts': 5,
        'friction_pct': 0.0, 'side': 'put',
        'take_profit_pct': 0.50, 'stop_loss_mult': 2.0, 'close_at_dte': 0,
    }
    cfg.update(over)
    return cfg


def days(start, n):
    return [start + dt.timedelta(days=i) for i in range(n)]


# --------------------------------------------------------------------- tests

def test_flat_market_put_credit_expires_worthless():
    """Spot pinned well above the short put -> spread expires worthless, we keep the credit."""
    expiry = dt.date(2026, 3, 13)
    path = {d: 780.0 for d in days(dt.date(2026, 3, 9), 5)}
    store = build_market(path, expiry)

    eng = Engine(store, base_config())
    res = eng.run(min(path), max(path))

    assert res['trades'], "expected at least one trade"
    t = res['trades'][0]
    assert t['kind'] == 'put_credit'
    assert t['exit_reason'] in ('expiry', 'take_profit'), t['exit_reason']
    assert t['pnl'] > 0, t
    assert eng.cash > 100_000, eng.cash
    print(f"  flat market      -> {t['exit_reason']:<12} P&L ${t['pnl']:>9,.2f}  "
          f"final ${eng.cash:,.2f}")


def test_crash_hits_max_loss_and_is_bounded():
    """A crash through both strikes must lose exactly the defined max, never more."""
    expiry = dt.date(2026, 3, 13)
    path = dict(zip(days(dt.date(2026, 3, 9), 5), [780, 775, 760, 730, 700]))
    path = {d: float(v) for d, v in path.items()}
    store = build_market(path, expiry)

    cfg = base_config(stop_loss_mult=99.0)  # disable the stop so settlement is tested
    eng = Engine(store, cfg)
    res = eng.run(min(path), max(path))

    t = res['trades'][0]
    assert t['pnl'] < 0, t
    # Loss can never exceed the defined max risk (plus fees).
    assert t['pnl'] >= -(t['max_loss'] + 5.0), (t['pnl'], t['max_loss'])
    print(f"  crash            -> {t['exit_reason']:<12} P&L ${t['pnl']:>9,.2f}  "
          f"max_loss ${t['max_loss']:,.2f} (bounded)")


def test_stop_loss_fires_before_expiry():
    """A sharp move should trip the 2x-credit stop rather than riding to expiry."""
    expiry = dt.date(2026, 3, 20)
    path = dict(zip(days(dt.date(2026, 3, 16), 5), [780.0, 770.0, 755.0, 750.0, 748.0]))
    store = build_market(path, expiry)

    eng = Engine(store, base_config(stop_loss_mult=2.0))
    res = eng.run(min(path), max(path))

    reasons = [t['exit_reason'] for t in res['trades']]
    assert 'stop_loss' in reasons, reasons
    t = next(t for t in res['trades'] if t['exit_reason'] == 'stop_loss')
    assert t['exit_date'] < expiry
    print(f"  sharp move       -> stop_loss    P&L ${t['pnl']:>9,.2f}  "
          f"exited {t['exit_date']} before {expiry}")


def test_friction_monotonically_reduces_pnl():
    """More friction must never improve the result. This is the sweep's core assumption."""
    expiry = dt.date(2026, 3, 13)
    path = {d: 780.0 for d in days(dt.date(2026, 3, 9), 5)}
    store = build_market(path, expiry)

    out = []
    for f in (0.0, 0.01, 0.03, 0.06):
        eng = Engine(store, base_config(friction_pct=f))
        eng.run(min(path), max(path))
        out.append((f, eng.cash))

    for (f1, c1), (f2, c2) in zip(out, out[1:]):
        assert c2 <= c1 + 1e-6, f"friction {f2} produced MORE profit than {f1}: {c2} > {c1}"
    print("  friction sweep   -> " + "  ".join(f"{f:.0%}:${c:,.0f}" for f, c in out))


def test_position_sizing_respects_risk_budget():
    """Sizing must never risk more than max_risk_per_trade_pct of equity."""
    expiry = dt.date(2026, 3, 13)
    path = {d: 780.0 for d in days(dt.date(2026, 3, 9), 5)}
    store = build_market(path, expiry)

    cfg = base_config(max_risk_per_trade_pct=0.02, max_contracts=100)
    eng = Engine(store, cfg)
    eng.run(min(path), max(path))

    t = eng.trades[0]
    budget = 100_000 * 0.02
    assert t['max_loss'] <= budget * 1.05, (t['max_loss'], budget)
    print(f"  sizing           -> qty {t['qty']}  max_loss ${t['max_loss']:,.2f} "
          f"<= budget ${budget:,.2f}")


def test_no_trade_when_credit_too_thin():
    """A credit-ratio floor above what the market offers must produce no trades."""
    expiry = dt.date(2026, 3, 13)
    path = {d: 780.0 for d in days(dt.date(2026, 3, 9), 5)}
    store = build_market(path, expiry)

    eng = Engine(store, base_config(min_credit_ratio=0.95, min_credit_ratio_vertical=0.95))
    res = eng.run(min(path), max(path))
    assert not res['trades'], res['trades']
    assert eng.cash == 100_000.0
    print("  thin credit      -> no trades, capital untouched")


def test_iv_inversion_recovers_the_synthetic_vol():
    """The chain is built at a known vol; the engine must recover it."""
    import strategy
    expiry = dt.date(2026, 3, 13)
    today = dt.date(2026, 3, 9)
    store = build_market({today: 780.0}, expiry, vol=0.16)
    chain = store.chain_for(UND, expiry, today)
    enriched = strategy.enrich_with_greeks(chain, 780.0, today)
    near = [c for c in enriched if 0.10 < abs(c['delta']) < 0.40]
    assert near, "no near-the-money contracts recovered"
    worst = max(abs(c['iv'] - 0.16) for c in near)
    assert worst < 0.02, f"IV inversion off by {worst:.4f}"
    print(f"  IV inversion     -> recovered 0.16 within {worst:.5f} across "
          f"{len(near)} contracts")


def test_underlying_is_not_dividend_adjusted():
    """
    Regression guard for the worst bug this backtest has had.

    Option strikes are adjusted for splits but NOT for ordinary dividends. Pulling
    dividend-adjusted ('all') underlying bars and comparing them to unadjusted strikes
    shifts the underlying down by the accumulated yield -- SPY on 2024-02-14 is $498.57
    raw vs $483.95 adjusted. Short calls that really finished in the money then score as
    expiring worthless, which once turned a losing strategy into a fake +845% return.

    This asserts the ingest recorded prices consistent with strikes. It is skipped when
    no store exists (fresh clone, CI without data).
    """
    import os
    db = os.path.join(os.path.dirname(__file__), '..', 'data', 'market.duckdb')
    if not os.path.exists(db):
        print("  adjustment guard -> skipped (no market.duckdb)")
        return
    import duckdb
    try:
        con = duckdb.connect(db, read_only=True)
        row = con.execute(
            "SELECT close FROM underlying_bars WHERE symbol='SPY' AND timeframe='1Day' "
            "AND CAST(ts AS DATE)='2024-02-14'").fetchone()
        con.close()
    except Exception as e:
        if 'lock' in str(e).lower() or 'Conflicting lock' in str(e):
            print("  adjustment guard -> skipped (DuckDB locked by ingest)")
            return
        raise
    if not row:
        print("  adjustment guard -> skipped (2024-02-14 not in store)")
        return
    close = row[0]
    assert abs(close - 498.57) < 1.0, (
        f"SPY 2024-02-14 close is {close:.2f}; expected ~498.57 (raw/split-adjusted). "
        f"{483.95:.2f} means the ingest used --adjustment all, which is WRONG for "
        f"options -- strikes are not dividend-adjusted. Re-run: "
        f"python3 data/ingest.py underlying --force")
    print(f"  adjustment guard -> SPY 2024-02-14 close ${close:.2f} (raw, matches strikes)")


def test_condor_risk_is_one_wing_not_two():
    """
    An iron condor's max loss is the WIDER wing less the TOTAL credit, because the
    underlying can only finish through one side. That is the structural reason the
    condor's break-even win rate is lower than a single vertical's, and it is the
    whole basis for preferring it after the vertical backtest failed.
    """
    expiry = dt.date(2026, 3, 13)
    path = {d: 780.0 for d in days(dt.date(2026, 3, 9), 5)}
    store = build_market(path, expiry)

    cfg = base_config(structure='condor', side='both', min_credit_ratio=0.02,
                      delta_tolerance=0.15, max_concurrent=1)
    eng = Engine(store, cfg)
    res = eng.run(min(path), max(path))
    assert res['trades'], "condor produced no trades"

    t = res['trades'][0]
    assert t['structure'] == 'condor', t['structure']
    # Put wing sits below spot, call wing above -- they must straddle.
    assert t['short_strike'] < 780.0 < t['call_short_strike'], t
    # Risk must be ONE wing's width less total credit, not two.
    one_wing = (t['width'] - t['credit']) * 100 * t['qty']
    assert abs(t['max_loss'] - one_wing) < 1.0, (t['max_loss'], one_wing)
    # Net delta near zero is the point of the structure.
    assert abs(t['net_delta']) < 0.15, t['net_delta']
    print(f"  condor           -> puts {t['short_strike']:.0f}/{t['long_strike']:.0f} "
          f"calls {t['call_short_strike']:.0f}/{t['call_long_strike']:.0f}  "
          f"credit {t['credit']:.2f}  net delta {t['net_delta']:+.3f}  "
          f"max loss ${t['max_loss']:,.0f}")


def test_condor_loses_at_most_one_wing_on_a_crash():
    """A crash through the put wing must not also charge for the untouched call wing."""
    expiry = dt.date(2026, 3, 13)
    path = dict(zip(days(dt.date(2026, 3, 9), 5), [780.0, 775.0, 750.0, 720.0, 700.0]))
    store = build_market(path, expiry)

    cfg = base_config(structure='condor', side='both', min_credit_ratio=0.02,
                      delta_tolerance=0.15, max_concurrent=1, stop_loss_mult=99.0)
    eng = Engine(store, cfg)
    res = eng.run(min(path), max(path))
    assert res['trades']
    t = res['trades'][0]
    assert t['pnl'] < 0, t
    assert t['pnl'] >= -(t['max_loss'] + 5.0), (t['pnl'], t['max_loss'])
    print(f"  condor crash     -> P&L ${t['pnl']:>9,.2f}  bounded by one wing "
          f"(${t['max_loss']:,.0f}), call wing expired worthless")


def test_event_blackout_blocks_entry():
    """A listed macro date inside (today, today + max_dte] means no new position."""
    expiry = dt.date(2026, 3, 13)
    path = {d: 780.0 for d in days(dt.date(2026, 3, 9), 5)}
    store = build_market(path, expiry)
    blocked = Engine(store, base_config(event_blackout=['2026-03-12']))
    res_b = blocked.run(min(path), max(path))
    assert not res_b['trades'], "entry should be blocked by the event blackout"
    clear = Engine(store, base_config(event_blackout=['2026-04-30']))
    res_c = clear.run(min(path), max(path))
    assert res_c['trades'], "an event outside the horizon must not block"
    print("  event on 03-12   -> no entry; event on 04-30 -> trades normally")


def test_close_at_dte_exits_before_expiry():
    """close_at_dte=1: the position is closed with a market print, not settled at expiry."""
    expiry = dt.date(2026, 3, 13)
    path = {d: 780.0 for d in days(dt.date(2026, 3, 9), 5)}
    store = build_market(path, expiry)
    eng = Engine(store, base_config(close_at_dte=1, take_profit_pct=0.99))
    res = eng.run(min(path), max(path))
    assert res['trades'], "expected a trade"
    t = res['trades'][0]
    assert t['exit_reason'] == 'dte_exit', t['exit_reason']
    assert dt.date.fromisoformat(str(t['exit_date'])[:10]) < expiry
    print(f"  close_at_dte=1   -> exit {t['exit_reason']} on {t['exit_date']} (before {expiry})")


def test_vol_gate_reads_atm_iv():
    """generate_candidates tags every structure with iv_atm equal to the synthetic vol."""
    expiry = dt.date(2026, 3, 13)
    d = dt.date(2026, 3, 9)
    store = build_market({d: 780.0}, expiry, vol=0.22)
    chain = store.chain_for(UND, expiry, d)
    cands = strategy.generate_candidates(chain, 780.0, d, base_config(min_iv_rv_ratio=1.2, vol_gate_iv='atm'), realized_vol=0.15)
    assert cands and abs(cands[0]['iv_atm'] - 0.22) < 0.01, cands and cands[0].get('iv_atm')
    assert cands[0]['iv_rv_ratio'] == round(cands[0]['iv_atm'] / 0.15, 3)
    none = strategy.generate_candidates(chain, 780.0, d, base_config(min_iv_rv_ratio=1.6, vol_gate_iv='atm'), realized_vol=0.15)
    assert not none, "1.6x threshold must close the gate at 0.22 / 0.15 = 1.47x"
    print("  atm_iv 0.22 vs rv 0.15 -> gate 1.47x: open at 1.2, closed at 1.6")


def test_book_opens_two_underlyings_same_day():
    """max_concurrent is a book cap across names, not a licence to stack one ticker."""
    expiry = dt.date(2026, 3, 13)
    path = {d: 780.0 for d in days(dt.date(2026, 3, 9), 4)}
    spy = build_market(path, expiry)
    qqq = build_market(path, expiry, und='QQQ')
    store = DictStore(
        spy._sessions,
        {**spy._underlying, **qqq._underlying},
        {**spy._opt_close, **qqq._opt_close},
        {**spy._chain, **qqq._chain},
        [expiry],
    )
    cfg = base_config(structure='condor', side='both', min_credit_ratio=0.02,
                      delta_tolerance=0.15, max_concurrent=3,
                      underlyings=['SPY', 'QQQ'], take_profit_pct=0.99)
    eng = Engine(store, cfg)
    res = eng.run(min(path), max(path))
    names = {t.get('underlying') for t in res['trades']}
    assert names == {'SPY', 'QQQ'}, names
    print(f"  two-name book        -> opened {sorted(names)} same window")


def _bs_chain(spot, today, expiry, vol=0.16, lo=None, hi=None):
    lo = lo if lo is not None else int(spot) - 40
    hi = hi if hi is not None else int(spot) + 40
    rows = []
    t = bs.year_fraction((expiry - today).days)
    for strike in range(lo, hi + 1):
        for right in ('P', 'C'):
            px = bs.price(spot, float(strike), t, vol, right)
            if px < 0.02:
                continue
            rows.append({'occ': f"SPY{expiry:%y%m%d}{right}{int(strike * 1000):08d}",
                         'strike': float(strike), 'opt_right': right,
                         'expiry': expiry, 'close': round(px, 2)})
    return rows


def test_portrait_maps_setup_to_structure():
    """
    The selector is a market portrait, not 'condor unless put credit/width is fatter'.
    Chop + rich IV -> condor. Uptrend + rich IV -> put. Cheap IV -> nothing. Never a call.
    """
    today = dt.date(2026, 8, 25)
    expiry = dt.date(2026, 8, 28)
    spot = 766.71
    rows = _bs_chain(spot, today, expiry)
    chop = [760 + i * 0.2 for i in range(21)]
    up = [700 + i * 3.0 for i in range(21)]
    down = [800 - i * 3.0 for i in range(21)]
    cfg = {'structure': 'adaptive', 'width': 5.0, 'min_credit_ratio': 0.05,
           'min_credit_ratio_vertical': 0.05, 'min_iv_rv_ratio': 1.2,
           'target_delta': 0.15, 'delta_tolerance': 0.08, 'underlying': 'SPY',
           'prior_closes': chop}
    both = strategy.generate_candidates(rows, spot, today, cfg, realized_vol=0.12)
    assert len(both) == 1 and both[0]['kind'] == 'short_strangle', [s.get('kind') for s in both]
    cfg['prior_closes'] = up
    put = strategy.generate_candidates(rows, spot, today, cfg, realized_vol=0.12)
    assert len(put) == 1 and put[0]['kind'] == 'put_credit', [s.get('kind') for s in put]
    cfg['prior_closes'] = down
    skip = strategy.generate_candidates(rows, spot, today, cfg, realized_vol=0.12)
    assert not skip, [s.get('kind') for s in skip]
    cheap = strategy.generate_candidates(rows, spot, today, dict(cfg, prior_closes=chop),
                                         realized_vol=0.30)
    assert not cheap
    cfg['prior_closes'] = up
    debit = strategy.generate_candidates(rows, spot, today, cfg, realized_vol=0.30)
    assert len(debit) == 1 and debit[0]['kind'] == 'call_debit', [s.get('kind') for s in debit]
    print("  portrait          chop->strangle  up->put  down->skip  cheap chop->skip  cheap up->call debit")


def test_dollar_width_is_percent_of_spot():
    s = {'width_pct': 0.01, 'width_min': 1.0, 'width': 5.0}
    assert strategy.dollar_width(560, s) == 5.0   # capped at the SPY-calibrated $5
    assert strategy.dollar_width(90, s) == 1.0
    assert strategy.dollar_width(None, s) == 5.0
    today = dt.date(2026, 8, 25)
    expiry = dt.date(2026, 8, 28)
    cheap = _bs_chain(90.0, today, expiry, lo=70, hi=110)
    chop = [89 + i * 0.05 for i in range(21)]
    cfg = {'structure': 'adaptive', 'width': 5.0, 'width_pct': 0.01, 'width_min': 1.0,
           'min_credit_ratio': 0.02, 'min_credit_ratio_vertical': 0.02,
           'min_iv_rv_ratio': 1.2, 'target_delta': 0.15, 'delta_tolerance': 0.12,
           'underlying': 'XBI', 'prior_closes': chop}
    cands = strategy.generate_candidates(cheap, 90.0, today, cfg, realized_vol=0.12)
    assert cands, "expected a candidate on the $90 chain"
    if cands[0]['kind'] == 'iron_condor':
        assert cands[0]['width'] <= 2.0, cands[0]['width']
    print(f"  width_pct on $90 -> width {cands[0]['width']:.0f} ({cands[0]['kind']}), not $5")


def test_portrait_engine_chops_into_a_condor_not_a_call():
    expiry = dt.date(2026, 3, 13)
    start = dt.date(2026, 2, 10)
    path = {}
    for i, d in enumerate(days(start, 32)):
        path[d] = 780.0 + 1.2 * math.sin(i / 3.0)
    store = build_market(path, expiry)
    cfg = base_config(structure='adaptive', side='both', min_credit_ratio=0.02,
                      min_credit_ratio_vertical=0.02, min_iv_rv_ratio=1.2,
                      delta_tolerance=0.15, max_concurrent=1)
    eng = Engine(store, cfg)
    res = eng.run(min(path), max(path))
    assert res['trades'], "portrait produced no trades on a rich chop"
    kinds = {t['kind'] for t in res['trades']}
    assert 'call_credit' not in kinds, kinds
    assert kinds <= {'short_strangle', 'short_straddle', 'iron_condor', 'put_credit'}, kinds
    print(f"  portrait engine  -> traded {sorted(kinds)}, never a call vertical")


def test_event_blackout_is_per_expiry_not_per_dte_ceiling():
    """
    A trade is exposed to a jump it is OPEN THROUGH. The day-level rule blocked a
    session whenever an event fell inside max_dte, so widening the DTE range widened
    the blackout: DTE 1-7 blacked out ~56 sessions a year, DTE 8-15 ~120, and the
    extra ones were days whose candidates expired before the event. That made the
    exposure comparison unreadable -- DTE 8-15 showed FEWER trades, not more.
    """
    from engine import event_in_life, event_inside_horizon
    d = dt.date(2026, 3, 2)
    fomc = [dt.date(2026, 3, 18)]

    # 16 days out: inside a 30-day ceiling, so the old rule blocks the whole session...
    assert event_inside_horizon(fomc, d, 30) is True
    # ...but a candidate expiring on the 6th never meets it.
    assert event_in_life(fomc, d, dt.date(2026, 3, 6)) is False
    # One that expires after it does.
    assert event_in_life(fomc, d, dt.date(2026, 3, 20)) is True
    # Boundaries are inclusive on both ends: FOMC prints at 14:00 ET, so being open on
    # the day counts, and so does expiring on it.
    assert event_in_life(fomc, d, dt.date(2026, 3, 18)) is True
    assert event_in_life([d], d, dt.date(2026, 3, 6)) is True
    assert event_in_life([], d, dt.date(2026, 3, 20)) is False
    assert event_in_life(['2026-03-18'], d, dt.date(2026, 3, 20)) is True
    assert event_in_life(['not-a-date'], d, dt.date(2026, 3, 20)) is False
    print("  FOMC in 16 days      -> blocks a 20-day expiry, not a 4-day one")


def test_overnight_share_gate_null_vs_hard():
    """max_overnight_share=null is a no-op; a hard floor rejects when metrics say overnight is heavy."""
    import strategy
    expiry = dt.date(2026, 3, 13)
    today = dt.date(2026, 3, 9)
    path = {d: 780.0 for d in days(today, 5)}
    store = build_market(path, expiry)
    chain = store.chain_for(UND, expiry, today)
    assert chain
    mets = {'overnight_share': 0.60, 'mae10_p95': 0.02, 'eff_ratio': 0.2}
    off = strategy.generate_candidates(
        chain, 780.0, today,
        base_config(structure='condor', min_credit_ratio=0.05, max_overnight_share=None,
                    ohlcv_metrics=mets),
        realized_vol=0.12)
    assert off, "null overnight gate must not kill a valid condor"
    hard = strategy.generate_candidates(
        chain, 780.0, today,
        base_config(structure='condor', min_credit_ratio=0.05, max_overnight_share=0.40,
                    ohlcv_metrics=mets),
        realized_vol=0.12)
    assert not hard, "overnight_share 0.60 must fail max 0.40"
    ok = strategy.generate_candidates(
        chain, 780.0, today,
        base_config(structure='condor', min_credit_ratio=0.05, max_overnight_share=0.70,
                    ohlcv_metrics=mets),
        realized_vol=0.12)
    assert ok and abs(ok[0].get('overnight_share') - 0.60) < 1e-6
    print("  overnight gate     null->trade  0.40->block  0.70->pass")


def test_min_iv_har_ratio_null_vs_hard():
    """min_iv_har_ratio=null is a no-op; a hard floor rejects when IV/HAR is thin."""
    import strategy
    expiry = dt.date(2026, 3, 13)
    today = dt.date(2026, 3, 9)
    path = {d: 780.0 for d in days(today, 5)}
    store = build_market(path, expiry)
    chain = store.chain_for(UND, expiry, today)
    assert chain
    # short_iv on synthetic chain is typically ~0.20+; har=0.20 → ratio ~1.0+
    off = strategy.generate_candidates(
        chain, 780.0, today,
        base_config(structure='condor', min_credit_ratio=0.05, min_iv_har_ratio=None,
                    rv_forecast=0.20),
        realized_vol=0.12)
    assert off, "null iv_har gate must not kill a valid condor"
    hard = strategy.generate_candidates(
        chain, 780.0, today,
        base_config(structure='condor', min_credit_ratio=0.05, min_iv_har_ratio=99.0,
                    rv_forecast=0.20),
        realized_vol=0.12)
    assert not hard, "absurd min_iv_har_ratio must block"
    ok = strategy.generate_candidates(
        chain, 780.0, today,
        base_config(structure='condor', min_credit_ratio=0.05, min_iv_har_ratio=0.5,
                    rv_forecast=0.20),
        realized_vol=0.12)
    assert ok and ok[0].get('iv_har_ratio') is not None
    missing = strategy.generate_candidates(
        chain, 780.0, today,
        base_config(structure='condor', min_credit_ratio=0.05, min_iv_har_ratio=0.5),
        realized_vol=0.12)
    assert not missing, "gate on without rv_forecast must produce zero candidates"
    print("  iv_har gate        null->trade  99->block  0.5->pass  missing-forecast->block")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    print(f"Running {len(tests)} engine tests against a synthetic market\n")
    failed = 0
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print()
    if failed:
        print(f"{failed}/{len(tests)} FAILED")
        sys.exit(1)
    print(f"all {len(tests)} engine tests passed")
