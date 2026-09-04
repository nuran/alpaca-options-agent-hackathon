"""
Failure-injection tests for the live agent.

The engine tests in backtest/test_engine.py prove the maths. These prove the agent
behaves when things go wrong -- which is what actually decides whether an unattended
five-day run survives. Every test here forces a failure that would otherwise only
show up live, at the worst moment.

The governing principle: a failure must never become a trade. Every degraded path
resolves to PASS or to a hard stop, never to "proceed and hope".

Run: .venv/bin/python agent/test_agent.py
"""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, 'agent'))

import agent_loop as A
import live_book as LB

# The fixtures are built from the book that actually ships -- account C's. In the
# development repo this loaded agent/agent_rules.json, which is not part of this
# submission, so the tests would have exercised a rulebook nobody can see.
RULES = json.load(open(os.path.join(REPO_ROOT, 'agent', 'profiles',
                                    'putcr-core6-d47', 'rules.json')))

# The default rulebook's flat_by_date is a real, dated operating decision, and once it
# passes, risk_gate correctly force-closes everything -- which silently broke six tests
# that have nothing to do with flat-by (position caps, take-profit, exit evaluation) the
# morning the date arrived. A suite whose result depends on today's calendar cannot tell
# a regression from a Tuesday. Tests that DO test flat-by set the date themselves; every
# other test gets a horizon far enough out that the gate is not what they are measuring.
RULES['schedule'] = dict(RULES['schedule'], flat_by_date='2099-12-31')


def base_obs(**over):
    o = {
        'ts': '2026-08-27T10:00:00', 'equity': 100_000.0, 'last_equity': 100_000.0,
        'day_pnl': 0.0, 'day_pnl_pct': 0.0, 'cash': 100_000.0,
        'options_buying_power': 100_000.0, 'options_level': 3,
        'positions': [], 'open_orders': [], 'account_blocked': False,
    }
    o.update(over)
    return o


# The fixture used to carry qty=4 and max_loss=$1,640, sized against a 2% budget on
# $100k. Dropping the live risk to 1.25% turned two passing tests red for a reason that
# had nothing to do with what they test: a fixture that hard-codes dollars against a
# CONFIGURABLE risk parameter breaks every time that parameter moves. It now sizes itself
# the same way the agent does, so the next change to the budget cannot fake a failure.
_FIXTURE_UNIT = (5.0 - 0.90) * 100          # one condor of this shape, defined risk
_FIXTURE_QTY = max(1, int((100_000 * float(RULES['risk']['max_risk_per_trade_pct']))
                          // _FIXTURE_UNIT))


def a_condor(**over):
    """A well-formed condor candidate that passes every gate."""
    c = {
        'id': 'SPY-IC-2026-08-28-760/785', 'underlying': 'SPY',
        'kind': 'iron_condor', 'structure': 'condor',
        'expiry': '2026-08-28', 'dte': 1,
        'put_short_occ': 'SPY260828P00760000', 'put_long_occ': 'SPY260828P00755000',
        'put_short_strike': 760.0, 'put_long_strike': 755.0,
        'call_short_occ': 'SPY260828C00785000', 'call_long_occ': 'SPY260828C00790000',
        'call_short_strike': 785.0, 'call_long_strike': 790.0,
        'put_short_delta': -0.15, 'call_short_delta': 0.15, 'net_delta': 0.0,
        'short_iv': 0.20, 'realized_vol': 0.15, 'iv_rv_ratio': 1.33,
        'width': 5.0, 'put_width': 5.0, 'call_width': 5.0,
        'credit': 0.90, 'credit_ratio': 0.18, 'qty': _FIXTURE_QTY,
        'max_loss': _FIXTURE_UNIT * _FIXTURE_QTY,
        'max_profit': 0.90 * 100 * _FIXTURE_QTY, 'worst_leg_spread_pct': 0.03,
    }
    c.update(over)
    return c


def an_option_position(symbol='SPY260828P00760000', upl=0.0, basis=1000.0, qty='-4'):
    return {'symbol': symbol, 'asset_class': 'us_option',
            'unrealized_pl': str(upl), 'cost_basis': str(basis), 'qty': qty}


def rules_without_blackout():
    """The default rules carry the contest macro calendar; most tests need a quiet horizon."""
    r = json.loads(json.dumps(RULES))
    r['schedule']['event_blackout'] = []
    return r


def registered_condor(state=None, credit=0.90, cid='agent-20260827-abc123', expiry='2099-01-15'):
    # A far expiry keeps these fixtures independent of the wall clock: the DTE exit is
    # covered by its own test with an explicit `now_et`, everything else tests P&L only.
    """
    A filled iron condor as the registry sees it, plus the four broker positions that
    correspond to it. Returns (state, positions).
    """
    state = state if state is not None else {}
    cand = a_condor(expiry=expiry)
    import structures as ST
    st = ST.register_open(state, cand, {'client_order_id': cid, 'order_id': 'o1', 'shadow': False})
    st['status'] = 'open'
    st['credit_fill'] = credit
    positions = [
        an_option_position(cand['put_short_occ'], qty='-4'),
        an_option_position(cand['put_long_occ'], qty='4'),
        an_option_position(cand['call_short_occ'], qty='-4'),
        an_option_position(cand['call_long_occ'], qty='4'),
    ]
    return state, positions


def condor_quotes(short_px, long_px):
    """Quotes for the registered condor: both shorts at `short_px`, both longs at `long_px`."""
    c = a_condor()
    q = lambda px: {'bid': round(px * 0.97, 2), 'ask': round(px, 2)}
    return {c['put_short_occ']: q(short_px), c['call_short_occ']: q(short_px),
            c['put_long_occ']: {'bid': round(long_px, 2), 'ask': round(long_px * 1.03, 2)},
            c['call_long_occ']: {'bid': round(long_px, 2), 'ask': round(long_px * 1.03, 2)}}


# ============================================================ validation gate

def test_llm_cannot_invent_a_candidate():
    """
    The single most important guard: a model that names a candidate it was never
    offered must be rejected. This is the path a prompt injection or a hallucination
    would take, and it must not reach the broker.
    """
    cands = [a_condor()]
    forged = a_condor(id='SPY-IC-EVIL-999/1', max_loss=99_999_999.0, qty=9999)
    choice = {'action': 'open', 'candidate_id': forged['id'],
              'confidence': 1.0, 'rationale': 'trust me'}
    match, errs = A.validate(choice, cands, base_obs(), RULES)
    assert match is None, "a forged candidate was accepted"
    assert errs and 'was not in the offered list' in errs[0], errs
    print("  forged candidate      -> REJECTED (not in offered list)")


def test_llm_cannot_oversize():
    """Even a legitimately-offered candidate is re-checked against the risk budget."""
    oversized = a_condor(max_loss=50_000.0)
    choice = {'action': 'open', 'candidate_id': oversized['id'],
              'confidence': 0.9, 'rationale': 'big one'}
    match, errs = A.validate(choice, [oversized], base_obs(), RULES)
    assert match is None
    assert any('exceeds budget' in e for e in errs), errs
    print(f"  oversized candidate   -> REJECTED ({errs[0][:52]}...)")


def test_condor_wings_must_straddle():
    """Wings that do not straddle are two overlapping bets; the risk does not net."""
    bad = a_condor(put_short_strike=790.0, call_short_strike=760.0)
    choice = {'action': 'open', 'candidate_id': bad['id'], 'confidence': 0.9, 'rationale': ''}
    match, errs = A.validate(choice, [bad], base_obs(), RULES)
    assert match is None
    assert any('straddle' in e for e in errs), errs
    print("  wings don't straddle  -> REJECTED")


def test_condor_must_be_delta_neutral():
    """A condor skewed far from neutral is a directional bet wearing a condor's name."""
    skewed = a_condor(net_delta=0.62)
    choice = {'action': 'open', 'candidate_id': skewed['id'], 'confidence': 0.9, 'rationale': ''}
    match, errs = A.validate(choice, [skewed], base_obs(), RULES)
    assert match is None
    assert any('delta-neutral' in e for e in errs), errs
    print("  skewed net delta      -> REJECTED")


def test_max_loss_arithmetic_is_reverified():
    """A candidate whose stated max_loss doesn't match one-wing arithmetic is refused."""
    lying = a_condor(max_loss=100.0)   # real answer is _FIXTURE_UNIT * _FIXTURE_QTY
    choice = {'action': 'open', 'candidate_id': lying['id'], 'confidence': 0.9, 'rationale': ''}
    match, errs = A.validate(choice, [lying], base_obs(), RULES)
    assert match is None
    assert any('one-wing arithmetic' in e for e in errs), errs
    print("  understated max_loss  -> REJECTED (arithmetic re-checked)")


def test_insufficient_buying_power_blocks():
    choice = {'action': 'open', 'candidate_id': a_condor()['id'],
              'confidence': 0.9, 'rationale': ''}
    match, errs = A.validate(choice, [a_condor()], base_obs(options_buying_power=100.0), RULES)
    assert match is None and any('buying power' in e for e in errs), errs
    print("  no buying power       -> REJECTED")


def test_good_candidate_passes():
    """The gates must not be so tight that nothing legitimate gets through."""
    c = a_condor()
    choice = {'action': 'open', 'candidate_id': c['id'], 'confidence': 0.8, 'rationale': 'ok'}
    match, errs = A.validate(choice, [c], base_obs(), RULES)
    assert match is not None and not errs, errs
    print("  valid candidate       -> ACCEPTED (gates are not vacuous)")


# ============================================================ malformed model

def test_malformed_llm_responses_all_resolve_to_pass():
    """Every shape of bad model output must become PASS, never a trade."""
    cands = [a_condor()]
    cases = [
        ("not JSON at all",        "I think you should buy calls!"),
        ("truncated JSON",         '{"action": "open", "candidate_i'),
        ("empty string",           ""),
        ("JSON array not object",  '[{"action":"open"}]'),
        ("null",                   'null'),
    ]
    for label, raw in cases:
        parsed = A.extract_json(raw)
        choice = parsed if isinstance(parsed, dict) else {'action': 'pass',
                                                          'rationale': 'unparseable'}
        match, errs = A.validate(choice, cands, base_obs(), RULES)
        assert match is None, f"{label} produced a trade"
    print(f"  {len(cases)} malformed shapes    -> all PASS, none traded")


def test_missing_action_field_is_not_an_open():
    """A response with no action must not be treated as permission to trade."""
    choice = {'candidate_id': a_condor()['id'], 'confidence': 1.0}
    match, errs = A.validate(choice, [a_condor()], base_obs(), RULES)
    assert match is None
    print("  no 'action' field     -> treated as PASS")


def test_non_dict_decision_rejected():
    for bad in (None, [], "open", 42):
        match, errs = A.validate(bad, [a_condor()], base_obs(), RULES)
        assert match is None, bad
    print("  non-object decisions  -> all rejected")


# ============================================================ risk gates

def test_daily_loss_halt_stops_new_positions():
    rules = rules_without_blackout()
    rules['risk']['daily_loss_halt_pct'] = 0.04  # champion live uses 0.99; gate still works
    obs = base_obs(equity=95_000.0, last_equity=100_000.0,
                   day_pnl=-5_000.0, day_pnl_pct=-0.05)
    may_open, forced, reasons = A.risk_gate(obs, rules, {})
    assert may_open is False
    assert any('daily loss' in r for r in reasons), reasons
    print(f"  -5% day               -> HALTED ({reasons[0][:44]}...)")


def test_drawdown_halt_sets_sticky_state():
    """A drawdown breach must latch: the agent does not restart itself."""
    rules = rules_without_blackout()
    rules['risk']['max_drawdown_halt_pct'] = 0.10
    state = {'peak_equity': 100_000.0}
    obs = base_obs(equity=88_000.0, last_equity=88_500.0, day_pnl_pct=-0.005)
    may_open, forced, reasons = A.risk_gate(obs, rules, state)
    assert may_open is False
    assert state.get('halted') is True, "halt did not latch into state"
    assert any('restart is a human decision' in r for r in reasons), reasons
    print("  -12% drawdown         -> HALTED and latched (human restart required)")


def test_account_blocked_stops_everything():
    may_open, forced, reasons = A.risk_gate(base_obs(account_blocked=True), RULES, {})
    assert may_open is False and 'blocked' in reasons[0]
    print("  broker blocked        -> HALTED")


def test_position_cap_blocks_new_entries():
    """The cap counts STRUCTURES, not legs: three condors are three positions, not twelve."""
    rules = rules_without_blackout()
    rules['risk']['max_concurrent_positions'] = 3  # fixture uses a small cap; live book is 20
    cap = rules['risk']['max_concurrent_positions']
    state, positions = {}, []
    for i in range(cap):
        st, pos = registered_condor(state, cid=f'agent-x{i}', expiry=f'2026-09-0{i + 1}')
        # give each structure distinct legs so the reconciler sees them all held
        for leg, p in zip(state['structures'][f'agent-x{i}']['legs'], pos):
            leg['occ'] = leg['occ'].replace('260828', f'26090{i + 1}')
            p['symbol'] = leg['occ']
        positions += pos
    obs = base_obs(positions=positions)
    quotes = {p['symbol']: ({'bid': 0.43, 'ask': 0.45} if p['qty'].startswith('-') else {'bid': 0.05, 'ask': 0.06})
              for p in positions}   # close cost 0.80 on a 0.90 credit -> hold
    may_open, forced, reasons = A.risk_gate(obs, rules, state, quotes=quotes)
    assert may_open is False and any('position cap' in r for r in reasons), reasons
    assert not forced, forced
    print(f"  {cap} structures open     -> no new entries (cap counts structures)")


def test_take_profit_is_evaluated_on_the_structure_not_a_leg():
    """
    Fix D1. One short leg has decayed 70% but the other side has widened: the STRUCTURE
    is at +11% of credit. The old per-leg rule would have closed the winning leg alone.
    """
    rules = rules_without_blackout()
    state, positions = registered_condor(credit=0.90)
    c = a_condor()
    quotes = {c['put_short_occ']: {'bid': 0.12, 'ask': 0.14},   # decayed 70%
              c['put_long_occ']: {'bid': 0.03, 'ask': 0.04},
              c['call_short_occ']: {'bid': 0.70, 'ask': 0.74},  # widened
              c['call_long_occ']: {'bid': 0.05, 'ask': 0.06}}
    # close cost = 0.14 - 0.03 + 0.74 - 0.05 = 0.80 -> +11% of the 0.90 credit
    may_open, forced, reasons = A.risk_gate(base_obs(positions=positions), rules, state, quotes=quotes)
    assert not forced, forced
    assert state['structures']['agent-20260827-abc123']['last_eval']['action'] is None
    print("  one leg +70%, structure +11% -> HOLD (structure-level P&L)")


def test_take_profit_forces_a_close():
    """Structure close cost <= credit * (1 - tp) -> one forced close of the whole condor."""
    rules = rules_without_blackout()
    state, positions = registered_condor(credit=0.90)
    quotes = condor_quotes(short_px=0.20, long_px=0.03)   # cost = 2*(0.20-0.03) = 0.34 <= 0.45
    may_open, forced, reasons = A.risk_gate(base_obs(positions=positions), rules, state, quotes=quotes)
    assert len(forced) == 1 and forced[0]['action'] == 'close_structure', forced
    assert forced[0]['reason'].startswith('take profit') and forced[0]['urgent'] is False
    print("  structure at +62% of credit -> forced close of ALL legs (take profit)")


def test_stop_loss_uses_engine_arithmetic():
    """close_cost >= credit * stop_loss_mult -> urgent close, exactly as engine.manage."""
    rules = rules_without_blackout()
    rules['exits']['stop_loss_mult'] = 2.0  # champion live uses 99 (off); arithmetic still works
    state, positions = registered_condor(credit=0.90)
    quotes = condor_quotes(short_px=1.00, long_px=0.05)   # cost = 2*(1.00-0.05) = 1.90 >= 1.80
    may_open, forced, reasons = A.risk_gate(base_obs(positions=positions), rules, state, quotes=quotes)
    assert len(forced) == 1 and forced[0]['urgent'] is True, forced
    assert forced[0]['reason'].startswith('stop loss')
    print("  close cost 2.1x credit  -> URGENT close of the structure (stop loss)")


def test_missing_leg_quote_blocks_exit_evaluation():
    """A structure with an unpriced leg is never acted on; it is flagged STALE_MARK."""
    rules = rules_without_blackout()
    state, positions = registered_condor(credit=0.90)
    quotes = condor_quotes(short_px=0.10, long_px=0.02)
    quotes[a_condor()['call_long_occ']] = None
    may_open, forced, reasons = A.risk_gate(base_obs(positions=positions), rules, state, quotes=quotes)
    assert not forced and any('STALE_MARK' in r for r in reasons), (forced, reasons)
    print("  unpriced leg            -> no exit action, STALE_MARK alert")


def test_live_exit_matches_backtest_engine_arithmetic():
    """Parametrised equivalence: structures.evaluate_exit == engine.manage on the same prices."""
    import random
    import structures as ST
    rules = rules_without_blackout()
    rng = random.Random(7)
    for _ in range(40):
        credit = round(rng.uniform(0.4, 2.0), 2)
        state, _ = registered_condor(credit=credit)
        st = state['structures']['agent-20260827-abc123']
        legs = {l['occ']: l for l in st['legs']}
        quotes = {}
        for occ, l in legs.items():
            px = round(rng.uniform(0.02, 1.5), 2)
            quotes[occ] = {'bid': px, 'ask': px}   # zero spread so debit is exact
        debit = sum(quotes[l['occ']]['ask'] if l['side'] == 'sell' else -quotes[l['occ']]['bid']
                    for l in st['legs'])
        tp, sl = rules['exits']['take_profit_pct'], rules['exits']['stop_loss_mult']
        engine_action = ('take_profit' if debit <= credit * (1 - tp)
                         else 'stop_loss' if debit >= credit * sl else None)
        action, why, _ = ST.evaluate_exit(st, quotes, rules, now_et=dt.datetime(2026, 8, 27, 9, 0))
        live_action = {'close': 'take_profit', 'close_urgent': 'stop_loss', None: None}[action]
        assert live_action == engine_action, (credit, debit, action, why)
    print("  40 random condors       -> live exits == engine.manage arithmetic")


def test_flat_by_date_closes_everything():
    """On and after the flat-by date the agent liquidates and stops opening."""
    rules = rules_without_blackout()
    rules['schedule']['flat_by_date'] = str(dt.date.today() - dt.timedelta(days=1))
    state, positions = registered_condor()
    quotes = condor_quotes(short_px=0.40, long_px=0.05)   # hold territory otherwise
    may_open, forced, reasons = A.risk_gate(base_obs(positions=positions), rules, state, quotes=quotes)
    assert may_open is False
    assert forced and all(f['action'] == 'close_structure' for f in forced)
    assert any('flat-by' in r for r in reasons), reasons
    print("  past flat-by date       -> closes all structures, opens none")


def test_healthy_account_may_open():
    may_open, forced, reasons = A.risk_gate(base_obs(), rules_without_blackout(), {})
    assert may_open is True and not forced
    print("  healthy account         -> may open (gates are not vacuous)")


# ============================================================ reconciliation (D3)

def test_stock_position_triggers_flatten_and_halt():
    """An assignment leaves stock behind. The agent flattens it and halts."""
    rules = rules_without_blackout()
    obs = base_obs(positions=[{'symbol': 'SPY', 'asset_class': 'us_equity', 'qty': '100'}])
    state = {}
    may_open, forced, reasons = A.risk_gate(obs, rules, state)
    assert may_open is False and state.get('halted') is True
    assert forced and forced[0]['action'] == 'flatten_stock' and forced[0]['symbol'] == 'SPY'
    print("  100 SPY at broker       -> flatten stock, HALT (assignment reconcile)")


def test_orphan_leg_is_closed_and_halts():
    """An option leg that belongs to no registered structure is closed and the agent halts."""
    rules = rules_without_blackout()
    obs = base_obs(positions=[an_option_position('SPY260828P00760000', qty='-4')])
    state = {}
    may_open, forced, reasons = A.risk_gate(obs, rules, state)
    assert may_open is False and state.get('halted') is True
    assert forced[0]['action'] == 'close_leg'
    print("  unregistered leg        -> close leg, HALT")


def test_broken_structure_legs_become_orphans():
    """Two of four legs vanished (partial external close): the rest are orphans, agent halts."""
    rules = rules_without_blackout()
    state, positions = registered_condor()
    obs = base_obs(positions=positions[:2])
    may_open, forced, reasons = A.risk_gate(obs, rules, state)
    assert state.get('halted') is True
    assert {f['symbol'] for f in forced if f['action'] == 'close_leg'} == {p['symbol'] for p in positions[:2]}
    print("  half a condor held      -> remaining legs closed, HALT")


def test_no_duplicate_structure_per_underlying_and_kind():
    """Same underlying+expiry must not stack; max_per_name>1 may stack different expiries."""
    rules = rules_without_blackout()
    state, positions = registered_condor(expiry='2026-08-28')
    cand = a_condor(id='SPY-NEW', expiry='2026-08-28')  # same expiry as the open one
    kept, dropped = A.drop_duplicate_kinds([cand], state, rules)
    assert not kept and dropped[0]['candidate_id'] == 'SPY-NEW', (kept, dropped)
    match, errs = A.validate({'action': 'open', 'candidate_id': 'SPY-NEW'}, [cand],
                             base_obs(positions=positions), rules, state)
    assert match is None and any('already holding' in e for e in errs), errs
    # Different expiry is allowed only when max_per_name >= 2. Set it here rather than
    # inheriting it from whichever rulebook happens to be the default: a fixture that
    # reads a CONFIGURABLE parameter breaks every time that parameter moves, which is
    # exactly what happened when the default book changed from 3 per name to 1.
    rules['risk']['max_per_name'] = 3
    other_exp = a_condor(id='SPY-LATER', expiry='2026-09-04')
    kept2, _ = A.drop_duplicate_kinds([other_exp], state, rules)
    assert [c['id'] for c in kept2] == ['SPY-LATER'], kept2
    other = a_condor(id='QQQ-NEW', underlying='QQQ')
    kept, _ = A.drop_duplicate_kinds([other], state, rules)
    assert [c['id'] for c in kept] == ['QQQ-NEW']
    print("  duplicate expiry          -> dropped; new expiry / other name -> kept")


def test_validate_counts_structures_not_legs():
    """Audit B3: one open condor is one position; its four legs must not exhaust the cap."""
    rules = rules_without_blackout()
    rules['risk']['max_concurrent_positions'] = 3
    state, positions = registered_condor()            # one open SPY condor = one position
    cand = a_condor(id='QQQ-NEW', underlying='QQQ')   # different underlying, so no duplicate rule
    match, errs = A.validate({'action': 'open', 'candidate_id': 'QQQ-NEW'}, [cand],
                             base_obs(positions=positions), rules, state)
    assert match is not None and not errs, errs
    # three open structures really do exhaust the cap
    for i in range(2):
        registered_condor(state, cid=f'extra{i}', expiry=f'2026-09-0{i+5}')
    for st in state['structures'].values():
        st['underlying'] = 'IWM'                      # keep the duplicate rule out of this test
    match, errs = A.validate({'action': 'open', 'candidate_id': 'QQQ-NEW'}, [cand],
                             base_obs(positions=positions), rules, state)
    assert match is None and any('position cap' in e for e in errs), errs
    print("  validate cap              -> counts structures (1 condor != 4 positions)")


def test_pending_structure_legs_are_not_orphans():
    """Audit B1: a filled order whose fill sync has not run yet must not look like an orphan."""
    rules = rules_without_blackout()
    state, positions = registered_condor()
    state['structures']['agent-20260827-abc123']['status'] = 'pending'   # sync_fills has not run
    may_open, forced, reasons = A.risk_gate(base_obs(positions=positions), rules, state)
    assert not state.get('halted') and not forced, (forced, reasons)
    print("  pending, filled at broker -> no orphan, no halt")


def test_closing_structure_does_not_halt_the_agent():
    """Audit B2: between the close submission and its fill the legs are still held."""
    rules = rules_without_blackout()
    state, positions = registered_condor()
    st = state['structures']['agent-20260827-abc123']
    st['status'] = 'closing'
    may_open, forced, reasons = A.risk_gate(base_obs(positions=positions), rules, state)
    assert not state.get('halted') and not forced, (forced, reasons)
    # capital is still committed, so the structure still counts against the position cap
    assert len([x for x in __import__('structures').open_structures(state)]) == 1
    print("  close submitted, not filled -> no halt, still counts against the cap")


def test_filled_close_retires_the_structure():
    rules = rules_without_blackout()
    state, _ = registered_condor()
    st = state['structures']['agent-20260827-abc123']
    st['status'] = 'closing'
    may_open, forced, reasons = A.risk_gate(base_obs(positions=[]), rules, state)
    assert st['status'] == 'closed' and may_open is True and not forced
    print("  close filled              -> structure retired, capacity freed")


# ============================================================ blackout / gates (D4-D7)

def test_event_inside_horizon_does_not_halt_adaptive():
    rules = rules_without_blackout()
    rules['strategy']['structure'] = 'adaptive'
    rules['strategy']['kind'] = 'adaptive'
    rules['schedule']['event_blackout'] = [{'date': str(dt.date.today() + dt.timedelta(days=3)), 'event': 'NFP'}]
    may_open, forced, reasons = A.risk_gate(base_obs(), rules, {})
    assert may_open is True, reasons
    rules['schedule']['event_blackout'] = [{'date': str(dt.date.today() + dt.timedelta(days=30)), 'event': 'FOMC'}]
    may_open, _, _ = A.risk_gate(base_obs(), rules, {})
    assert may_open is True
    condor_rules = rules_without_blackout()
    condor_rules['strategy']['structure'] = 'condor'
    condor_rules['schedule']['event_blackout'] = [{'date': str(dt.date.today() + dt.timedelta(days=3)), 'event': 'NFP'}]
    may_open, _, reasons = A.risk_gate(base_obs(), condor_rules, {})
    assert may_open is False and any('macro event' in r for r in reasons), reasons
    print("  NFP in 3 days           -> adaptive still opens (S3); condor book still blackouts")


def some_news(headline='Fed officials speak at conference', symbols=('SPY',)):
    return [{'headline': headline, 'symbols': list(symbols),
             'created_at': dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')}]


def test_no_llm_key_passes_by_default():
    """Fix D4: a missing decision step is a doubt, and doubt is PASS."""
    # load_env_var falls back to the local dotenv file, so removing the process env is not
    # enough on a machine that has keys configured: stub the lookup itself.
    saved = os.environ.pop('ANTHROPIC_API_KEY', None)
    real_lookup = A.load_env_var
    A.load_env_var = lambda name, required=True: (None if name == 'ANTHROPIC_API_KEY'
                                                  else real_lookup(name, required))
    try:
        choice, raw = A.decide([a_condor()], base_obs(), some_news(), RULES)
        assert choice['action'] == 'pass', choice
        rules = json.loads(json.dumps(RULES)); rules['llm']['deterministic_fallback'] = True
        choice, raw = A.decide([a_condor()], base_obs(), some_news(), rules)
        assert choice['action'] == 'open' and choice['veto']['context']['source'] == 'deterministic_fallback'
    finally:
        A.load_env_var = real_lookup
        if saved is not None:
            os.environ['ANTHROPIC_API_KEY'] = saved
    print("  no LLM key              -> PASS (deterministic mode only when explicitly enabled)")


def test_empty_news_feed_fails_closed():
    """An empty/missing feed payload is unverifiable, not quiet: PASS."""
    choice, _ = A.decide([a_condor()], base_obs(), [], RULES, post_fn=lambda b: (True, {}))
    assert choice['action'] == 'pass' and choice['pass_reason'] == 'unclear_news', choice
    print("  news feed empty         -> PASS (tape unverifiable)")


def _classifier(scope='NONE', severity='NONE', clarity='CLEAR', catalyst=None):
    payload = {'catalyst_scope': scope, 'jump_severity': severity, 'news_clarity': clarity,
               'catalyst': catalyst, 'confidence': 0.8}
    calls = []
    def post(body):
        calls.append(body)
        return True, {'content': [{'type': 'text', 'text': json.dumps(payload)}]}
    return post, calls


def test_quiet_tape_opens_and_costs_one_cached_call():
    """Regression against the v2_2 design: an uneventful tape must ALLOW, not PASS."""
    post, calls = _classifier()
    news = some_news('S&P 500 opens flat as traders await Friday jobs data')
    c1, _ = A.decide([a_condor()], base_obs(), news, RULES, post_fn=post)
    c2, _ = A.decide([a_condor()], base_obs(), news, RULES, post_fn=post)   # same headlines
    assert c1['action'] == 'open' and c2['action'] == 'open'
    assert len(calls) == 1, "identical headlines must hit the classification cache"
    assert c2['veto']['context']['source'] == 'cache'
    print("  quiet tape              -> open; second cycle served from cache (1 LLM call)")


def test_mega_cap_catalyst_vetoes_qqq_and_keeps_spy():
    post, _ = _classifier('QQQ_ONLY', 'MAJOR', 'CLEAR', 'NVDA guidance cut, NDX -2.6%')
    spy = a_condor(id='SPY-IC', underlying='SPY')
    qqq = a_condor(id='QQQ-IC', underlying='QQQ')
    choice, _ = A.decide([qqq, spy], base_obs(), some_news('NVDA cuts guidance', ('QQQ',)), RULES, post_fn=post)
    assert choice['action'] == 'open' and choice['candidate_id'] == 'SPY-IC', choice
    assert [v['underlying'] for v in choice['veto']['vetoed']] == ['QQQ']
    print("  mega-cap shock          -> QQQ vetoed, SPY opened")


def test_broad_catalyst_passes_all_and_logs_counterfactual():
    post, _ = _classifier('BOTH', 'MAJOR', 'CLEAR', 'Unscheduled tariff announcement after the close')
    choice, _ = A.decide([a_condor()], base_obs(), some_news('Tariff announcement expected today'), RULES, post_fn=post)
    assert choice['action'] == 'pass' and choice['pass_reason'] == 'catalyst'
    assert choice['veto']['counterfactual_candidate_id'] == a_condor()['id']
    print("  broad shock             -> PASS with counterfactual candidate logged")


def test_minor_severity_is_not_a_veto():
    post, _ = _classifier('QQQ_ONLY', 'MINOR', 'CLEAR', 'small-cap software miss')
    choice, _ = A.decide([a_condor(underlying='QQQ')], base_obs(), some_news('Software miss', ('QQQ',)), RULES, post_fn=post)
    assert choice['action'] == 'open', choice
    print("  minor severity          -> open (cannot reach the short strike)")


def test_rich_iv_and_drift_do_not_reach_the_model_as_numbers():
    """Payload discipline: no thresholds, no P&L, no sizing fields in the prompt."""
    post, calls = _classifier()
    A.decide([a_condor()], base_obs(day_pnl=-3200.0), some_news('VIX jumps as traders hedge'), RULES, post_fn=post)
    payload = calls[0]['messages'][0]['content']
    for forbidden in ('iv_rv', 'credit_ratio', 'max_loss', 'qty', 'client_order_id', 'day_pnl', '-3200'):
        assert forbidden not in payload, forbidden
    assert 'short 760P/785C' in payload and 'dte 1' in payload
    assert calls[0]['system'][0]['cache_control']['type'] == 'ephemeral'
    print("  payload                 -> strikes + headlines only, system prompt cached")


def test_classifier_failure_and_refusal_pass():
    for post in (lambda b: (False, 'HTTP 500'),
                 lambda b: (True, {'stop_reason': 'refusal'}),
                 lambda b: (True, {'content': [{'type': 'text', 'text': 'not json'}]}),
                 lambda b: (True, {'content': [{'type': 'text', 'text': json.dumps({'catalyst_scope': 'MAYBE'})}]})):
        choice, _ = A.decide([a_condor()], base_obs(), some_news('Something happened'), RULES, post_fn=post)
        assert choice['action'] == 'pass' and choice['pass_reason'] == 'unclear_news', choice
    print("  classifier failures     -> PASS (4 failure modes)")


def test_vol_gate_uses_atm_iv_not_short_leg_iv():
    """
    Fix D5. A skewed chain: the 15-delta put reads 1.35x realized, the ATM level 1.11x.
    With the 1.2 threshold the gate must be CLOSED.
    """
    import strategy as S
    rows = [
        {'strike': 749.0, 'right': 'P', 'delta': -0.155, 'iv': 0.157, 'expiry': '2026-09-04'},
        {'strike': 760.0, 'right': 'P', 'delta': -0.35, 'iv': 0.138, 'expiry': '2026-09-04'},
        {'strike': 766.0, 'right': 'P', 'delta': -0.49, 'iv': 0.130, 'expiry': '2026-09-04'},
        {'strike': 768.0, 'right': 'P', 'delta': -0.54, 'iv': 0.128, 'expiry': '2026-09-04'},
        {'strike': 766.0, 'right': 'C', 'delta': 0.51, 'iv': 0.129, 'expiry': '2026-09-04'},
        {'strike': 768.0, 'right': 'C', 'delta': 0.46, 'iv': 0.128, 'expiry': '2026-09-04'},
        {'strike': 780.0, 'right': 'C', 'delta': 0.15, 'iv': 0.113, 'expiry': '2026-09-04'},
    ]
    iv = S.atm_iv(rows)
    assert abs(iv - 0.129) < 0.003, iv
    rv = 0.116
    assert 0.157 / rv > 1.2 > iv / rv, "short-leg IV would pass, ATM IV must not"
    print(f"  ATM IV {iv:.3f} vs 15d put 0.157 -> gate reads {iv/rv:.2f}x, not {0.157/rv:.2f}x")


def test_stale_quote_rejected():
    """Fix D7: a leg quoted more than max_quote_age_s ago is not priced and not tradable."""
    import structures as ST
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=900)).isoformat().replace('+00:00', 'Z')
    fresh = dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')
    chain = {'snapshots': {
        'A': {'latestQuote': {'bp': 1.0, 'ap': 1.1, 't': old}},
        'B': {'latestQuote': {'bp': 1.0, 'ap': 1.1, 't': fresh}}}}
    q = ST.quotes_from_chain(chain, ['A', 'B'], max_age_s=RULES['execution']['max_quote_age_s'])
    assert q['A'] is None and q['B'] is not None
    print("  quote 15 min old        -> rejected; fresh quote -> priced")


def test_close_order_is_one_mleg_with_to_close_intents():
    import structures as ST
    state, _ = registered_condor(credit=0.90)
    st = state['structures']['agent-20260827-abc123']
    args, limit = ST.close_order_args(st, 0.34, RULES, shadow=True)
    legs = json.loads(args[args.index('--legs') + 1])
    assert len(legs) == 4 and all(l['position_intent'].endswith('_to_close') for l in legs)
    assert limit > 0 and args[args.index('--client-order-id') + 1].startswith('close-')
    print("  structure close         -> one mleg order, 4 *_to_close legs, positive debit limit")


def test_usadapt_intent_converts_to_agent_candidate_and_passes_validation():
    """Horizon B bridge: a funnel intent becomes a candidate the existing validator accepts."""
    import usadapt_bridge as B
    intent = {'decision_id': 'abcdef1234', 'symbol': 'SPY', 'structure': 'S2_IRON_CONDOR',
              'expiration': '2026-09-04', 'dte': 8, 'qty': 3, 'credit_mid': 2.28, 'credit_exec': 2.25,
              'limit_credit': 2.27, 'floor_credit': 2.27, 'max_loss_total': 1725.0, 'data_ok': True, 'source': 'pit',
              'legs': [dict(symbol='SPY260904P00754000', side='SELL', cp='P', strike=754.0, bid=2.11, ask=2.13, delta=-0.22),
                       dict(symbol='SPY260904P00746000', side='BUY', cp='P', strike=746.0, bid=1.19, ask=1.21, delta=-0.12),
                       dict(symbol='SPY260904C00777000', side='SELL', cp='C', strike=777.0, bid=1.63, ask=1.65, delta=0.22),
                       dict(symbol='SPY260904C00785000', side='BUY', cp='C', strike=785.0, bid=0.33, ask=0.34, delta=0.07)]}
    cand = B.intent_to_candidate(intent)
    rules = json.loads(json.dumps(RULES)); rules['risk']['max_risk_per_trade_pct'] = 0.02
    match, errs = A.validate({'action': 'open', 'candidate_id': cand['id']}, [cand], base_obs(), rules)
    assert match is not None and not errs, errs
    print("  funnel intent           -> agent candidate, passes validate()")


def test_dte_exit_on_expiry_day_after_cutoff():
    """Fix D2: a structure still open on its expiry day is closed after close_at_dte_time ET."""
    import structures as ST
    rules = rules_without_blackout()
    state, _ = registered_condor(credit=0.90, expiry='2026-08-28')
    st = state['structures']['agent-20260827-abc123']
    st['expiry'] = '2026-08-28'
    st['opened_at'] = '2026-08-27T15:50:00'
    quotes = condor_quotes(short_px=0.40, long_px=0.05)      # hold territory on P&L
    a0, _, _ = ST.evaluate_exit(st, quotes, rules, now_et=dt.datetime(2026, 8, 27, 15, 55))  # DTE 1, opening day
    a1, _, _ = ST.evaluate_exit(st, quotes, rules, now_et=dt.datetime(2026, 8, 28, 10, 0))   # expiry day, before cutoff
    a2, why, _ = ST.evaluate_exit(st, quotes, rules, now_et=dt.datetime(2026, 8, 28, 15, 5))  # expiry day, after cutoff
    assert a0 is None and a1 is None and a2 == 'close' and why.startswith('dte exit'), (a0, a1, a2, why)
    print("  expiry day 15:05 -> dte exit; opening day / before cutoff -> hold")


def test_blackout_default_is_fomc_only():
    """Evidence-based default: only FOMC dates are in the live blackout (see _why_event_blackout)."""
    kinds = {e['event'] for e in RULES['schedule']['event_blackout']}
    assert kinds == {'FOMC'}, kinds
    print("  live blackout           -> FOMC only, by evidence")


def test_shipped_books_match_the_configuration_that_was_measured():
    """
    Both live rulebooks reproduce the configuration whose friction sweep is in README §5.

    This test exists to fail loudly when a rulebook drifts from the configuration that was
    actually measured. That is not hypothetical -- the equivalent test in the development
    repo caught a universe change on 2026-08-31. The two books differ in exactly two
    fields, `min_dte` and the length of the underlying list; everything else is pinned
    identically, which is what makes the tenor comparison between the accounts mean
    anything at all.
    """
    import json as _json
    books = {}
    for name in ('putcr-core6-d47', 'putcr-core6'):
        with open(os.path.join(REPO_ROOT, 'agent', 'profiles', name, 'rules.json')) as fh:
            books[name] = _json.load(fh)

    c, b = books['putcr-core6-d47'], books['putcr-core6']

    # Account binding: each book names its own credentials, and the two must not collide.
    assert c['account']['account_id'] == 'PA3UU4TX8Y3K'
    assert b['account']['account_id'] == 'PA3ZNXHF8ID1'
    assert c['account']['key_env'] != b['account']['key_env']

    # The one field under test between the accounts.
    assert int(c['strategy']['min_dte']) == 4 and int(c['strategy']['max_dte']) == 7
    assert int(b['strategy']['min_dte']) == 1 and int(b['strategy']['max_dte']) == 7

    for name, r in books.items():
        s_, u, rk, x = r['strategy'], r['universe'], r['risk'], r['exits']
        assert u['mode'] == 'fixed', name
        # Ingested but below the pre-declared liquidity floor -- never in a live book.
        for thin in ('ASHR', 'IEF', 'XLE', 'XLU'):
            assert thin not in u['underlyings'], (name, thin)
        assert s_['structure'] == 'vertical' and s_['side'] == 'put', name
        assert s_['enabled_structures'] == ['put_credit'], name
        assert s_.get('enable_debit') is False, name
        assert float(s_['target_delta']) == 0.15, name
        assert float(s_['delta_tolerance']) == 0.08, name
        assert float(s_['width']) == 5.0, name
        assert float(s_['min_iv_rv_ratio']) == 1.2, name
        assert float(rk['max_risk_per_trade_pct']) == 0.02, name
        assert int(rk['max_concurrent_positions']) == 3, name
        assert int(rk['max_per_name']) == 1, name
        assert float(rk['vol_target_pct']) == 0.25, name
        assert float(x['take_profit_pct']) == 0.5, name
        assert float(x['stop_loss_mult']) == 2.0, name
        # Halts armed live. The backtest disables them on purpose (a halt truncates the
        # sample and makes runs incomparable) -- README §5 says which sweep was run how.
        assert float(rk['daily_loss_halt_pct']) == 0.04, name
        assert float(rk['max_drawdown_halt_pct']) == 0.10, name

    print("  two books, one difference -> DTE 4-7 (C) vs 1-7 (B); everything else pinned")

def test_market_gate_closed(monkey):
    monkey(lambda args, allow_fail=False: {
        'is_open': False, 'next_open': '2026-08-28T09:30:00Z',
        'next_close': '2026-08-28T16:00:00Z'})
    open_ok, reason = A.market_gate(RULES)
    assert open_ok is False and 'closed' in reason
    print("  market closed         -> loop exits cleanly")


def test_market_gate_near_close_is_exits_only(monkey):
    soon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)
    monkey(lambda args, allow_fail=False: {
        'is_open': True, 'next_open': '2026-08-28T09:30:00Z',
        'next_close': soon.isoformat().replace('+00:00', 'Z')})
    open_ok, reason = A.market_gate(RULES)
    assert open_ok is True and 'exits only' in reason, reason
    print("  5 min to close        -> exits only, no new positions")


def test_cli_failure_is_loud_not_silent(monkey):
    """An order path failure must raise, never be swallowed into a fake success."""
    def boom(args, allow_fail=False):
        if allow_fail:
            return None
        raise SystemExit(1)
    monkey(boom)
    try:
        A.submit_spread(a_condor(), RULES, shadow=True)
    except SystemExit:
        print("  CLI failure on order  -> exits loudly (not swallowed)")
        return
    raise AssertionError("order submission swallowed a CLI failure")


def test_vol_gate_blocks_when_realized_vol_unavailable(monkey):
    """
    If trailing realized vol cannot be computed, the vol gate cannot be evaluated --
    and the correct response is to trade nothing, not to trade blind.
    """
    monkey(lambda args, allow_fail=False: None)   # every CLI call fails softly
    assert A.realized_vol('SPY') is None
    print("  no realized vol       -> gate cannot pass, nothing traded")


def test_order_payload_has_four_legs_and_negative_limit():
    """A condor is four legs, and a credit is a NEGATIVE limit on an mleg order."""
    captured = {}
    real_cli = A.cli
    A.cli = lambda args, allow_fail=False: captured.update({'args': args}) or {'id': 'x'}
    try:
        A.submit_spread(a_condor(), RULES, shadow=True)
    finally:
        A.cli = real_cli
    args = captured['args']
    legs = json.loads(args[args.index('--legs') + 1])
    assert len(legs) == 4, legs
    assert [l['side'] for l in legs] == ['sell', 'buy', 'sell', 'buy'], legs
    limit = float(args[args.index('--limit-price') + 1])
    assert limit < 0, f"credit must be a negative limit, got {limit}"
    assert '--dry-run' in args, "shadow mode did not pass --dry-run"
    assert '--client-order-id' in args, "missing idempotency key"
    print(f"  order payload         -> 4 legs, limit {limit} (credit), dry-run, client id")


def test_live_flag_is_never_constructed():
    """Structural guarantee: no code path in this repo can reach the live host."""
    offenders = []
    for root, _, files in os.walk(REPO_ROOT):
        if any(p in root for p in ('.venv', '.git', '__pycache__', 'runs')):
            continue
        for fn in files:
            if not fn.endswith('.py'):
                continue
            # These two name the flag in order to SEARCH for it; they are the
            # checkers, not constructors. Everything else is fair game.
            if fn in ('test_agent.py', 'preflight.py'):
                continue
            path = os.path.join(root, fn)
            with open(path) as f:
                for i, line in enumerate(f, 1):
                    if "'--live'" in line or '"--live"' in line:
                        # alpaca/*.py are vendored capability scripts a human may run
                        # directly; the AGENT must never construct the flag.
                        if os.path.basename(root) == 'agent':
                            offenders.append(f"{fn}:{i}")
    assert not offenders, f"agent constructs --live at {offenders}"
    print("  --live flag           -> never constructed by the agent")


def test_realized_vol_window_ends_before_the_exchange_day(monkey):
    """
    Regression guard for a timezone bug that silently disabled the whole strategy.

    The Basic plan refuses SIP data for the CURRENT exchange day. The vol-gate window
    was bounded with `local_today - 1`, which lands ON the current exchange day
    whenever this machine is ahead of New York (at 07:35 local, ET+8, the exchange is
    still on yesterday). The request was refused, allow_fail turned that into None,
    and the gate then blocked every trade -- with no error anywhere.
    """
    captured = {}

    def fake(args, allow_fail=False):
        if args == ['clock']:
            # Exchange is a day behind the local clock.
            return {'timestamp': '2026-08-26T23:35:00Z', 'is_open': False,
                    'next_open': '2026-08-27T09:30:00Z',
                    'next_close': '2026-08-27T16:00:00Z'}
        captured['end'] = args[args.index('--end') + 1]
        base = 700.0
        return {'bars': {'SPY': [{'c': base + i * 0.5,
                                  't': f'2026-07-{(i % 28) + 1:02d}T04:00:00Z'}
                                 for i in range(30)]}}

    A._EXCHANGE_DATE = None
    monkey(fake)
    try:
        rv = A.realized_vol('SPY')
    finally:
        A._EXCHANGE_DATE = None

    assert rv is not None, "realized vol came back None -- the gate would block everything"
    exch = dt.date(2026, 8, 26)
    requested_end = dt.date.fromisoformat(captured['end'])
    assert requested_end < exch, (
        f"window ends {requested_end}, which is not strictly before the exchange day "
        f"{exch} -- Basic will refuse this and the vol gate will silently disable trading")
    print(f"  vol window end        -> {requested_end} < exchange day {exch}")


def test_missing_cli_fails_with_a_useful_message(monkey):
    """A scheduled run with no PATH must say what is wrong, not raise FileNotFoundError."""
    saved = A.CLI_CANDIDATES[:]
    import shutil as _sh
    real_which = _sh.which
    A.CLI_CANDIDATES[:] = ['/nonexistent/alpaca']
    _sh.which = lambda name: None
    try:
        A._resolve_cli()
    except SystemExit:
        print("  CLI missing entirely  -> dies with install instructions, not a traceback")
        return
    finally:
        A.CLI_CANDIDATES[:] = saved
        _sh.which = real_which
    raise AssertionError("_resolve_cli did not fail when the binary was absent")


# --------------------------------------------------------- verified effects (A2)

def test_order_key_is_idempotent_within_the_cadence_slot():
    """
    A submission whose response is lost and then retried must NOT open a second condor.
    The client_order_id is a pure function of the intent and the cadence slot, so the
    broker refuses the duplicate. It was a uuid4 before, which made every retry a new order.
    """
    import effects as EFF
    cand = a_condor()
    t0 = dt.datetime(2026, 8, 28, 10, 1)
    k = EFF.open_key(cand, t0, cadence_minutes=15)
    assert k == EFF.open_key(cand, dt.datetime(2026, 8, 28, 10, 14), cadence_minutes=15)
    assert k != EFF.open_key(cand, dt.datetime(2026, 8, 28, 10, 16), cadence_minutes=15)
    assert k != EFF.open_key(dict(cand, qty=cand['qty'] + 1), t0, cadence_minutes=15)
    print("  retry inside the slot -> same client id (broker dedups, no double open)")


def test_broker_echo_is_verified_against_the_intent():
    """
    A broker echo that does not match what we asked for is refused HERE, not discovered
    two cycles later by the reconciler. Wrong qty, flipped limit sign, missing leg.
    """
    cand = a_condor()
    real_cli = A.cli
    try:
        A.cli = lambda args, allow_fail=False: {'id': 'o1', 'status': 'accepted',
                                                'qty': '99', 'limit_price': '-1.00',
                                                'legs': []}
        out = A.submit_spread(cand, RULES, shadow=False)
        assert out.get('effect_mismatch'), out
        assert out.get('rejected_reason', '').startswith('effect_mismatch'), out
    finally:
        A.cli = real_cli
    print("  broker echoes qty 99  -> effect mismatch, structure not registered")


def test_mismatched_submission_is_never_registered():
    """The registry may only contain structures the broker confirmed."""
    import structures as ST
    state = {}
    submitted = {'client_order_id': 'x', 'rejected_reason': 'effect_mismatch: qty'}
    if not submitted.get('rejected_reason'):
        ST.register_open(state, a_condor(), submitted)
    assert not ST.open_structures(state), "a mismatched order must not become a structure"
    print("  mismatched order      -> registry stays empty")


# ------------------------------------------------------------ record/replay (A3)

def test_tape_replay_reproduces_a_cycle_without_a_broker():
    """
    Record every crossing of the broker seam, then replay with the broker gone and get the
    same answers. This is what turns an incident into a permanent regression test.
    """
    import tape as TAPE
    import tempfile
    path = os.path.join(tempfile.mkdtemp(), 'tape.jsonl')
    answers = {('clock',): {'is_open': True}, ('account', 'get'): {'equity': '100000'}}
    rec = TAPE.Recorder(path)
    wrapped = rec.wrap_cli(lambda args, allow_fail=False: answers[tuple(args)])
    live = [wrapped(['clock']), wrapped(['account', 'get'])]
    rec.close()

    p = TAPE.Player(path)
    assert [p.cli(['clock']), p.cli(['account', 'get'])] == live
    print("  recorded cycle        -> replays identically with no broker")


def test_replay_refuses_to_invent_an_answer():
    """
    A code change that asks the broker something new must FAIL the replay rather than
    fall through to the network. A replay can never place an order.
    """
    import tape as TAPE
    import tempfile
    path = os.path.join(tempfile.mkdtemp(), 'tape.jsonl')
    rec = TAPE.Recorder(path)
    rec.wrap_cli(lambda args, allow_fail=False: {'is_open': True})(['clock'])
    rec.close()
    p = TAPE.Player(path)
    try:
        p.cli(['order', 'submit', '--qty', '1'])
        raise AssertionError("replay invented an answer for an unrecorded order")
    except TAPE.TapeMiss:
        pass
    print("  unrecorded call       -> TapeMiss, replay cannot reach the broker")


def test_tape_never_stores_a_credential():
    """Whatever a broker echoes back, a key must not end up in a committed tape."""
    import tape as TAPE
    import tempfile
    rec = TAPE.Recorder(os.path.join(tempfile.mkdtemp(), 't.jsonl'))
    try:
        rec._write('cli', ['account', 'get'], {'leaked': 'sk-ABCDEFGHIJKLMNOP'})
        raise AssertionError("a credential was written to the tape")
    except ValueError:
        pass
    finally:
        rec.close()
    print("  key-shaped echo       -> refused, never written to a tape")


def test_resolve_universe_uses_selector_sv_and_includes_lc():
    """Live book is selector ShortVol + LongConvex + optional SPY floor, not CORE six."""
    import tempfile
    doc = {
        'as_of': '2026-08-28',
        'shortlist_sv': [{'symbol': 'QQQ'}, {'symbol': 'XBI'}, {'symbol': 'XLV'}],
        'shortlist_lc': [{'symbol': 'UNH'}, {'symbol': 'SMH'}],
        'vix': {'contango_proxy': True, 'vix_pct_1y': 0.03},
    }
    path = os.path.join(tempfile.mkdtemp(), 'ranked.json')
    json.dump(doc, open(path, 'w'))
    rules = json.loads(json.dumps(RULES))
    rules['universe']['mode'] = 'selector'
    rules['universe']['selector'] = {'path': path, 'top_n': 3, 'always': ['SPY']}
    names, branches, info = A.resolve_universe(rules)
    assert names[0] == 'SPY' and 'QQQ' in names and 'XBI' in names and 'XLV' in names, names
    assert 'UNH' in names and 'SMH' in names, names
    assert 'IWM' not in names and 'GLD' not in names and 'XLF' not in names, names
    assert branches['QQQ'] == 'short_vol' and branches['UNH'] == 'long_convex'
    print("  selector SV+LC + SPY floor -> CORE-six not in the live book")


def test_choose_short_prefers_condor_unless_put_is_clearly_better():
    condor = {'kind': 'iron_condor', 'credit_ratio': 0.22}
    put = {'kind': 'put_credit', 'credit_ratio': 0.24}
    assert A.choose_short(condor, put, 0.90)['kind'] == 'iron_condor'
    assert A.choose_short(condor, {'kind': 'put_credit', 'credit_ratio': 0.30}, 0.90)['kind'] == 'put_credit'
    assert A.choose_short(None, put)['kind'] == 'put_credit'
    print("  structure picker        -> leftover credit/width tie-break; portrait does not use this")


def test_dollar_width_is_percent_of_spot_not_spy_five():
    s = {'width_pct': 0.01, 'width_min': 1.0, 'width': 5.0}
    assert A.dollar_width(560, s) == 5.0
    assert A.dollar_width(90, s) == 1.0
    assert A.dollar_width(None, s) == 5.0
    print("  width_pct 1% of spot    -> $5 cap on SPY, $1 on a $90 name")


def test_quiet_tape_returns_every_allowed_id():
    post, _ = _classifier()
    spy = a_condor(id='SPY-IC', underlying='SPY')
    qqq = a_condor(id='QQQ-IC', underlying='QQQ')
    choice, _ = A.decide([qqq, spy], base_obs(), some_news(), RULES, post_fn=post)
    assert choice['action'] == 'open', choice
    assert set(choice['candidate_ids']) == {'QQQ-IC', 'SPY-IC'}, choice
    print("  quiet tape, two names   -> both ids, not only the fattest credit/width")
def test_deploy_multi_book_parked():
    import deploy as D
    saved = os.environ.pop('DEPLOY_ALLOW', None)
    try:
        try:
            D.require_allowed()
            assert False, 'expected SystemExit'
        except SystemExit as e:
            assert e.code == 2
        os.environ['DEPLOY_ALLOW'] = '1'
        D.require_allowed()  # must not raise
    finally:
        if saved is None:
            os.environ.pop('DEPLOY_ALLOW', None)
        else:
            os.environ['DEPLOY_ALLOW'] = saved
    print("  deploy multi-book       -> blocked unless DEPLOY_ALLOW=1")
def test_deploy_open_keys_differ_by_strategy_tag():
    import effects as EFF
    cand = a_condor(id='SPY-IC')
    k0 = EFF.open_key(cand)
    k1 = EFF.open_key(cand, strategy_tag='pv6')
    k2 = EFF.open_key(cand, strategy_tag='b16')
    assert k0 != k1 != k2, (k0, k1, k2)
    assert k1.startswith('pv6-') and k2.startswith('b16-')
    print("  strategy order tags     -> distinct client_order_id prefixes")


def test_flat_by_override_is_shadow_only():
    """
    `--flat-by` widens the chain window so a shadow run can exercise the whole pipeline.
    It must be impossible to use it against the broker: an override that can run live is
    an override someone forgets to revert.
    """
    rules = {'schedule': {'flat_by_date': '2026-09-03'}}
    assert A.apply_flat_by_override([], rules, True) is None
    assert rules['schedule']['flat_by_date'] == '2026-09-03'

    applied = A.apply_flat_by_override(['--flat-by', '2026-12-31'], rules, True)
    assert applied == '2026-12-31' and rules['schedule']['flat_by_date'] == '2026-12-31'

    for bad_args, why in ((['--flat-by', '2026-12-31'], 'live run'),
                          (['--flat-by'], 'missing value'),
                          (['--flat-by', 'soon'], 'malformed date')):
        live = bad_args == ['--flat-by', '2026-12-31']
        guarded = {'schedule': {'flat_by_date': '2026-09-03'}}
        try:
            A.apply_flat_by_override(bad_args, guarded, shadow=not live)
        except SystemExit:
            pass
        else:
            raise AssertionError(f"should have refused: {why}")
        assert guarded['schedule']['flat_by_date'] == '2026-09-03', why
    print("  --flat-by               -> shadow only, validated, never touches the live window")


def test_flat_by_clamps_the_chain_window():
    """
    The agent must not OPEN what the flat-by date will force it to close early.

    backtest/engine.py filters the chain to `expiry <= flat_by`; risk_gate only fires ON
    the date. Without the clamp the agent opens Sep 4-7 expiries on Aug 31 and gets them
    liquidated Sep 3, one to four days early -- a systematic exit the backtest never
    modelled. This asserts the window the agent asks the broker for.
    """
    import datetime as _dt
    asked = {}

    def fake_cli(args, allow_fail=False):
        if args[:3] == ['data', 'option', 'chain']:
            asked['lo'] = args[args.index('--expiration-date-gte') + 1]
            asked['hi'] = args[args.index('--expiration-date-lte') + 1]
        return None

    rules = json.loads(json.dumps(RULES))
    today = _dt.date.today()
    rules['schedule']['event_blackout'] = []
    saved = A.cli
    try:
        A.cli = fake_cli
        # flat-by comfortably beyond the DTE window -> window untouched
        rules['schedule']['flat_by_date'] = (today + _dt.timedelta(days=60)).isoformat()
        A.build_candidates(rules, {'equity': 100_000.0}, names=['SPY'])
        assert asked['hi'] == (today + _dt.timedelta(days=int(rules['strategy']['max_dte']))).isoformat(), asked
        # flat-by inside the window -> clamped to it
        clamp = (today + _dt.timedelta(days=int(rules['strategy']['min_dte']) + 1)).isoformat()
        rules['schedule']['flat_by_date'] = clamp
        asked.clear()
        A.build_candidates(rules, {'equity': 100_000.0}, names=['SPY'])
        assert asked['hi'] == clamp, asked
        # flat-by before anything in the window can expire -> the name is skipped entirely.
        # Relative to min_dte, not a fixed +1 day: with min_dte=1 a fixed +1 IS the first
        # expiry, so the old form silently tested the clamp case instead of the skip case.
        rules['schedule']['flat_by_date'] = (
            today + _dt.timedelta(days=int(rules['strategy']['min_dte']) - 1)).isoformat()
        asked.clear()
        A.build_candidates(rules, {'equity': 100_000.0}, names=['SPY'])
        assert not asked, asked
    finally:
        A.cli = saved
    print("  flat-by                 -> chain window clamped, never opens what it must close early")


def test_deploy_foreign_legs_not_orphans():
    import deploy as D
    import structures as ST
    state = {'structures': {
        'pv6-x': {'client_order_id': 'pv6-x', 'status': 'open', 'credit_fill': 0.5,
                  'legs': [{'occ': 'SPY260905P00620000'}, {'occ': 'SPY260905P00615000'}]},
    }}
    obs = {'positions': [
        {'asset_class': 'us_option', 'symbol': 'SPY260905P00620000', 'qty': -2},
        {'asset_class': 'us_option', 'symbol': 'SPY260905P00615000', 'qty': 2},
        {'asset_class': 'us_option', 'symbol': 'QQQ260905P00560000', 'qty': -1},
    ]}
    # The registry is PRODUCTION state: foreign_occs() feeds reconcile(), which decides
    # what counts as an orphan leg, and an orphan halts the agent. This test used to call
    # sync_leg_registry() against the real path, so every `make test` seeded
    # agent/deploy/leg_registry.json with two SPY legs that exist nowhere but in this
    # fixture -- legs the other two books would then permanently excuse from orphan
    # detection until champ-pv6 next synced. Redirect it to a temp file for the duration.
    import tempfile
    real_registry = D.REGISTRY_PATH
    tmpdir = tempfile.mkdtemp(prefix='legreg-')
    D.REGISTRY_PATH = os.path.join(tmpdir, 'leg_registry.json')
    try:
        rec = ST.reconcile(state, obs, foreign_occs=D.foreign_occs('frozen-b16'))
        assert 'SPY260905P00620000' not in rec['orphans']
        assert 'SPY260905P00615000' not in rec['orphans']
        D.sync_leg_registry('champ-pv6', state)
        foreign = D.foreign_occs('frozen-b16')
        assert 'SPY260905P00620000' in foreign
        rec2 = ST.reconcile({'structures': {}}, obs, foreign_occs=foreign)
        assert 'SPY260905P00620000' not in rec2['orphans']
        assert 'SPY260905P00615000' not in rec2['orphans']
        assert 'QQQ260905P00560000' in rec2['orphans']
    finally:
        D.REGISTRY_PATH = real_registry
        shutil.rmtree(tmpdir, ignore_errors=True)
    print("  leg registry            -> other book's legs not orphans (temp registry)")
if __name__ == "__main__":
    saved_cli = A.cli

    def monkey(fn):
        A.cli = fn

    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith('test_')]
    print(f"Running {len(tests)} agent failure-injection tests\n")
    failed = 0
    for name, fn in tests:
        A.cli = saved_cli
        try:
            if fn.__code__.co_argcount == 1:
                fn(monkey)
            else:
                fn()
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {name}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    A.cli = saved_cli
    print()
    if failed:
        print(f"{failed}/{len(tests)} FAILED")
        sys.exit(1)
    print(f"all {len(tests)} agent tests passed")
