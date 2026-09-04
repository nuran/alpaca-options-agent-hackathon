"""
Structure-level position management for the live agent.

Why this module exists (fix D1 of the US-ADAPT review): Alpaca reports positions per
option contract, so a four-leg iron condor arrives as four independent rows. The
original risk gate applied take-profit / stop-loss to each row's own
``unrealized_pl / cost_basis``. That is not the condor's P&L: a short leg that has
decayed 60% would be closed alone while the other wing was losing, and the stop could
fire on one leg leaving three behind. The backtest engine (``engine.manage``) has
always managed the SPREAD as one object -- ``debit <= credit * (1 - tp)`` and
``debit >= credit * sl`` -- so the live agent diverged from the backtest that
justified it. This module makes the live agent use the same arithmetic.

Design:
  * every order the agent submits is registered in ``state['structures']`` under its
    ``client_order_id`` with its legs, quantity and credit;
  * fills are synced from the broker on the next cycle (``sync_fills``);
  * exits are evaluated on the structure's executable close cost -- short legs bought
    back at the ask, long legs sold at the bid -- against the credit actually filled;
  * a close is ONE mleg order with ``*_to_close`` intents, never leg-by-leg, so a
    partial close cannot strand a naked short (fix for the 2026-08-27 unbalanced-close
    incident);
  * broker positions that belong to no registered structure are ``orphans``; stock
    positions (assignment) are reported for reconciliation (fix D3).

Nothing here decides anything a model could influence: it is arithmetic on quotes.
"""
import datetime as dt
import json

CONTRACT_MULTIPLIER = 100

# States in which the structure still ties up capital and still owns its legs at the broker.
# 'closing' MUST be here: a submitted close is not an instant close, and between the submit
# and the fill the legs are still held. Leaving it out made every take-profit look like an
# orphan on the next cycle and halted the agent (audit 2026-08-28, bug B2).
OPEN_STATES = ('pending', 'open', 'closing')
CLAIM_STATES = ('pending', 'open', 'closing')


# ------------------------------------------------------------------ registry

def register_open(state, cand, submitted):
    """Record a just-submitted structure so later cycles can manage it as a unit."""
    structs = state.setdefault('structures', {})
    if cand.get('structure') == 'condor':
        legs = [
            {'occ': cand['put_short_occ'], 'side': 'sell'},
            {'occ': cand['put_long_occ'], 'side': 'buy'},
            {'occ': cand['call_short_occ'], 'side': 'sell'},
            {'occ': cand['call_long_occ'], 'side': 'buy'},
        ]
    elif cand.get('structure') in ('short_strangle', 'short_straddle'):
        legs = [
            {'occ': cand['put_short_occ'], 'side': 'sell'},
            {'occ': cand['call_short_occ'], 'side': 'sell'},
        ]
    else:
        legs = [
            {'occ': cand['short_occ'], 'side': 'sell'},
            {'occ': cand['long_occ'], 'side': 'buy'},
        ]
    cid = submitted['client_order_id']
    debit_side = (cand.get('side') or 'credit') == 'debit'
    structs[cid] = {
        'client_order_id': cid,
        'order_id': submitted.get('order_id'),
        'candidate_id': cand['id'],
        'underlying': cand['underlying'],
        'kind': cand['kind'],
        'structure': cand.get('structure', 'vertical'),
        'side': 'debit' if debit_side else 'credit',
        'expiry': str(cand['expiry']),
        'far_expiry': str(cand['far_expiry'])[:10] if cand.get('far_expiry') else None,
        'legs': legs,
        'qty': int(cand['qty']),
        'width': float(cand['width']),
        'credit_limit': 0.0 if debit_side else float(cand['credit']),
        'debit_limit': float(cand.get('debit') or 0) if debit_side else 0.0,
        'credit_fill': None,
        'debit_fill': None,
        'status': 'pending' if not submitted.get('shadow') else 'shadow',
        'opened_at': dt.datetime.now().isoformat(timespec='seconds'),
        'iv_rv_ratio': cand.get('iv_rv_ratio'),
    }
    return structs[cid]


def open_structures(state):
    return [s for s in state.get('structures', {}).values() if s['status'] in OPEN_STATES]


def sync_fills(state, cli):
    """
    Pull fill status for pending structures. A filled order fixes ``credit_fill`` (the
    absolute net price per spread); a cancelled/expired/rejected order retires the
    record so it never blocks the position cap.
    """
    changed = []
    for s in open_structures(state):
        if s['status'] != 'pending' or not s.get('order_id'):
            continue
        order = cli(['order', 'get', '--order-id', s['order_id']], allow_fail=True)
        if not isinstance(order, dict):
            continue
        status = (order.get('status') or '').lower()
        if status == 'filled':
            px = order.get('filled_avg_price')
            try:
                filled = abs(float(px)) if px is not None else None
            except (TypeError, ValueError):
                filled = None
            if s.get('side') == 'debit':
                s['debit_fill'] = filled if filled is not None else s.get('debit_limit')
            else:
                s['credit_fill'] = filled if filled is not None else s['credit_limit']
            s['status'] = 'open'
            s['filled_at'] = order.get('filled_at')
            changed.append(s['client_order_id'])
        elif status in ('canceled', 'cancelled', 'expired', 'rejected', 'done_for_day'):
            s['status'] = 'unfilled'
            changed.append(s['client_order_id'])
        elif status == 'partially_filled':
            # An mleg order fills atomically at Alpaca; a partial here is an anomaly
            # worth surfacing rather than modelling.
            s['anomaly'] = 'partially_filled mleg order'
    return changed


# -------------------------------------------------------------- reconciliation

def leg_positions(obs):
    """Broker option positions keyed by OCC symbol -> signed quantity."""
    out = {}
    for p in obs.get('positions', []):
        if p.get('asset_class') != 'us_option':
            continue
        try:
            out[p['symbol']] = float(p.get('qty', 0))
        except (TypeError, ValueError):
            continue
    return out


def stock_positions(obs):
    """Non-option positions with a non-zero quantity: the footprint of an assignment."""
    out = []
    for p in obs.get('positions', []):
        if p.get('asset_class') == 'us_option':
            continue
        try:
            q = float(p.get('qty', 0))
        except (TypeError, ValueError):
            continue
        if q != 0:
            out.append({'symbol': p.get('symbol'), 'qty': q,
                        'asset_class': p.get('asset_class')})
    return out


def reconcile(state, obs, pending_timeout_min=30, now=None, foreign_occs=None):
    """
    Compare the registry with the broker.

    Returns dict with:
      stock     -- stock positions (assignment happened; must be flattened, agent halts)
      orphans   -- option legs the broker holds that belong to no open structure
      missing   -- registered open structures with legs the broker no longer holds
                   (closed outside the agent, or expired); they are retired
      unmanageable -- open structures that cannot be evaluated (no credit recorded, or a
                   submission whose order id was lost); surfaced as an alert, never traded on
    """
    now = now or dt.datetime.now()
    held = leg_positions(obs)
    claimed = set()
    missing = []
    # First claim every leg that belongs to a registered structure, whatever its state.
    # A 'pending' structure may already be filled at the broker (the fill sync runs before
    # this, but a CLI hiccup can delay it), and a 'closing' one still holds its legs until
    # the closing order fills. Claiming first prevents both from being seen as orphans.
    for s in state.get('structures', {}).values():
        if s.get('status') in CLAIM_STATES:
            claimed.update(l['occ'] for l in s['legs'])
    for s in open_structures(state):
        if s['status'] == 'closing' and not any(held.get(l['occ']) for l in s['legs']):
            s['status'] = 'closed'          # the close filled; retire it and free the cap
            missing.append(s['client_order_id'])
            continue
        if s['status'] != 'open':
            continue
        legs_held = [l['occ'] in held and held[l['occ']] != 0 for l in s['legs']]
        if all(legs_held):
            pass                            # already claimed above
        elif not any(legs_held):
            s['status'] = 'closed_external'
            missing.append(s['client_order_id'])
        else:
            # Some legs gone, some remain: the structure is broken. Its remaining legs
            # become orphans below so they get closed and the agent halts.
            claimed.difference_update(l['occ'] for l in s['legs'])
            s['status'] = 'broken'
            missing.append(s['client_order_id'])
    # A day order that never filled is dead: retire stale 'pending' records whose legs are
    # not at the broker, so a lost order id cannot occupy the position cap forever.
    for s in list(state.get('structures', {}).values()):
        if s.get('status') != 'pending' or any(held.get(l['occ']) for l in s['legs']):
            continue
        try:
            age_min = (now - dt.datetime.fromisoformat(str(s.get('opened_at')))).total_seconds() / 60
        except (TypeError, ValueError):
            age_min = 0.0
        if age_min > pending_timeout_min:
            s['status'] = 'unfilled'
            missing.append(s['client_order_id'])
            claimed.difference_update(l['occ'] for l in s['legs'])

    unmanageable = [s['client_order_id'] for s in open_structures(state)
                    if s.get('status') == 'open'
                    and not _premium(s) > 0]
    skip = set(foreign_occs or ())
    orphans = [occ for occ, q in held.items()
               if q != 0 and occ not in claimed and occ not in skip]
    # Legs of a broken structure are orphans too.
    for s in state.get('structures', {}).values():
        if s.get('status') == 'broken':
            orphans.extend(l['occ'] for l in s['legs']
                           if held.get(l['occ']) and l['occ'] not in skip)
    return {'stock': stock_positions(obs), 'orphans': sorted(set(orphans)), 'missing': missing,
            'unmanageable': unmanageable}


# ------------------------------------------------------------- quotes and P&L

def quotes_from_chain(chain, occs, max_age_s=None, now=None):
    """Extract bid/ask (and age) for the requested OCC symbols from a chain payload."""
    snaps = (chain or {}).get('snapshots') or {}
    now = now or dt.datetime.now(dt.timezone.utc)
    out = {}
    for occ in occs:
        s = snaps.get(occ) or {}
        q = s.get('latestQuote') or {}
        bid, ask = q.get('bp'), q.get('ap')
        age = None
        ts = q.get('t')
        if ts:
            try:
                t = dt.datetime.fromisoformat(ts.replace('Z', '+00:00'))
                age = (now - t).total_seconds()
            except ValueError:
                age = None
        stale = max_age_s is not None and age is not None and age > max_age_s
        crossed = bid is not None and ask is not None and ask < bid   # locked/crossed book is not a price
        if bid is None or ask is None or ask <= 0 or stale or crossed:
            out[occ] = None
        else:
            out[occ] = {'bid': float(bid), 'ask': float(ask), 'age_s': age,
                        'delta': (s.get('greeks') or {}).get('delta')}
    return out


def close_cost(struct, quotes):
    """
    Executable cost to close the structure, per spread, in dollars per share.
    Short legs are bought back at the ask; long legs are sold at the bid.
    Returns None if any leg has no usable quote (fail closed: no action, alert).
    """
    total = 0.0
    for leg in struct['legs']:
        q = quotes.get(leg['occ'])
        if not q:
            return None
        total += q['ask'] if leg['side'] == 'sell' else -q['bid']
    return total


def _premium(struct):
    if struct.get('side') == 'debit':
        return struct.get('debit_fill') or struct.get('debit_limit') or 0
    return struct.get('credit_fill') or struct.get('credit_limit') or 0


def evaluate_exit(struct, quotes, rules, now_et=None):
    """
    Same arithmetic as ``backtest/engine.py::Engine.manage``:
        take profit  when close_cost <= credit * (1 - take_profit_pct)
        stop loss    when close_cost >= credit * stop_loss_mult
        dte exit     when DTE <= close_at_dte and time >= close_at_dte_time
    Returns (action, reason, detail) with action in {'close', 'close_urgent', None}.
    """
    ex = rules['exits']
    if struct.get('side') == 'debit':
        prem = _premium(struct)
        if not prem or prem <= 0:
            return None, 'no debit recorded', {}
        cost = close_cost(struct, quotes)
        detail = {'debit': prem, 'close_cost': cost}
        if cost is None:
            return None, 'STALE_MARK: a leg has no usable quote -- no action', detail
        proceeds = -cost
        pnl_pct = (proceeds - prem) / prem
        detail['pnl_pct'] = round(pnl_pct, 4)
        if proceeds >= prem * (1.0 + ex['take_profit_pct']):
            return 'close', f"take profit ({pnl_pct:+.0%} of debit)", detail
        if proceeds <= prem * max(0.0, 1.0 - ex['take_profit_pct']):
            return 'close_urgent', f"stop loss ({pnl_pct:+.0%} of debit)", detail
        if 'close_at_dte_time' in ex:
            close_at = int(ex.get('close_at_dte', 0))
            now_et = now_et or _now_et()
            expiry = dt.date.fromisoformat(str(struct['expiry'])[:10])
            dte = (expiry - now_et.date()).days
            cutoff = dt.time.fromisoformat(ex.get('close_at_dte_time', '15:00'))
            opened = str(struct.get('opened_at', ''))[:10]
            opened_today = opened == now_et.date().isoformat()
            if dte <= close_at and now_et.time() >= cutoff and (dte <= 0 or not opened_today):
                return 'close', f"dte exit (DTE {dte} <= {close_at}, after {cutoff:%H:%M} ET)", detail
        return None, f"hold ({pnl_pct:+.0%} of debit)", detail
    credit = struct.get('credit_fill') or struct.get('credit_limit')
    if not credit or credit <= 0:
        return None, 'no credit recorded', {}
    cost = close_cost(struct, quotes)
    detail = {'credit': credit, 'close_cost': cost}
    if cost is None:
        return None, 'STALE_MARK: a leg has no usable quote -- no action', detail
    pnl_pct = (credit - cost) / credit
    detail['pnl_pct'] = round(pnl_pct, 4)
    if cost <= credit * (1.0 - ex['take_profit_pct']):
        return 'close', f"take profit ({pnl_pct:+.0%} of credit)", detail
    if cost >= credit * ex['stop_loss_mult']:
        return 'close_urgent', f"stop loss ({pnl_pct:+.0%} of credit)", detail
    # Time exit: on the day DTE reaches close_at_dte, after close_at_dte_time ET.
    # close_at_dte = 0 means "on expiry day" -- the 1-DTE condor is closed an hour
    # before it would settle. Never on the opening day unless that day is the expiry.
    if 'close_at_dte_time' in ex:
        close_at = int(ex.get('close_at_dte', 0))
        now_et = now_et or _now_et()
        expiry = dt.date.fromisoformat(str(struct['expiry'])[:10])
        dte = (expiry - now_et.date()).days
        cutoff = dt.time.fromisoformat(ex.get('close_at_dte_time', '15:00'))
        opened = str(struct.get('opened_at', ''))[:10]
        opened_today = opened == now_et.date().isoformat()
        if dte <= close_at and now_et.time() >= cutoff and (dte <= 0 or not opened_today):
            return 'close', f"dte exit (DTE {dte} <= {close_at}, after {cutoff:%H:%M} ET)", detail
    return None, f"hold ({pnl_pct:+.0%} of credit)", detail


def _now_et():
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo('America/New_York'))
    except Exception:  # pragma: no cover
        return dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=4)


# ------------------------------------------------------------------- orders

def close_order_args(struct, cost, rules, shadow, urgent=False, attempt=0):
    """
    One mleg order that closes every leg. A closing debit is a POSITIVE limit price.
    Non-urgent closes pay the executable cost plus the configured slippage; urgent
    closes (stop loss) pay a further concession so the order is marketable.
    """
    ex = rules['execution']
    slip = ex.get('limit_slippage_pct', 0.02) * (2.0 if urgent else 1.0)
    if cost < 0:
        limit = -abs(round(abs(cost) * (1.0 - slip), 2))
        if limit == 0:
            limit = -0.01
    else:
        limit = round(max(cost, 0.01) * (1.0 + slip), 2)
    # The id must do two opposite jobs. Across CYCLES it has to repeat, so that a
    # close accepted by the broker just before this process died is rejected as a
    # duplicate on the next cycle instead of closing the structure twice. Within one
    # cycle it has to differ, so that attempt 2's higher limit is judged on its price
    # rather than refused as a duplicate of attempt 1 -- which used to push every
    # re-price straight into the leg-by-leg fallback. Attempt number does both.
    suffix = f"-r{attempt}" if attempt else ""
    client_id = f"close-{struct['client_order_id']}"[:48 - len(suffix)] + suffix
    legs = json.dumps([
        {'symbol': l['occ'], 'side': 'buy' if l['side'] == 'sell' else 'sell',
         'ratio_qty': '1',
         'position_intent': 'buy_to_close' if l['side'] == 'sell' else 'sell_to_close'}
        for l in struct['legs']
    ])
    args = ['order', 'submit', '--order-class', 'mleg',
            '--qty', str(struct['qty']), '--type', 'limit',
            '--limit-price', str(limit), '--time-in-force', ex.get('time_in_force', 'day'),
            '--client-order-id', client_id,
            '--legs', legs]
    if shadow:
        args.append('--dry-run')
    return args, limit


if __name__ == "__main__":
    # Self-check: structure P&L must match the engine's spread arithmetic.
    rules = {'exits': {'take_profit_pct': 0.5, 'stop_loss_mult': 2.0, 'close_at_dte': 0,
                       'close_at_dte_time': '15:00'},
             'execution': {'limit_slippage_pct': 0.02, 'time_in_force': 'day'}}
    s = {'client_order_id': 'agent-x', 'legs': [
        {'occ': 'PS', 'side': 'sell'}, {'occ': 'PL', 'side': 'buy'},
        {'occ': 'CS', 'side': 'sell'}, {'occ': 'CL', 'side': 'buy'}],
         'qty': 2, 'credit_fill': 1.00, 'expiry': '2099-01-01'}
    q = {'PS': {'bid': 0.20, 'ask': 0.22}, 'PL': {'bid': 0.05, 'ask': 0.06},
         'CS': {'bid': 0.18, 'ask': 0.20}, 'CL': {'bid': 0.04, 'ask': 0.05}}
    assert abs(close_cost(s, q) - (0.22 - 0.05 + 0.20 - 0.04)) < 1e-9
    a, r, d = evaluate_exit(s, q, rules)
    assert a == 'close' and r.startswith('take profit'), (a, r)
    q2 = {k: {'bid': v['bid'] * 8, 'ask': v['ask'] * 8} for k, v in q.items()}
    a, r, d = evaluate_exit(s, q2, rules)
    assert a == 'close_urgent', (a, r)
    q3 = dict(q); q3['CL'] = None
    a, r, d = evaluate_exit(s, q3, rules)
    assert a is None and r.startswith('STALE_MARK')
    # Audit regressions (2026-08-28): a pending-but-filled structure and a closing structure
    # must NOT look like orphans; a filled close must retire the structure.
    st = {'client_order_id': 'c1', 'status': 'pending', 'legs': s['legs'], 'qty': 1,
          'credit_limit': 1.0, 'credit_fill': None, 'expiry': '2099-01-01'}
    state = {'structures': {'c1': st}}
    obs = {'positions': [{'symbol': o, 'asset_class': 'us_option', 'qty': '-1'} for o in ('PS', 'PL', 'CS', 'CL')]}
    assert reconcile(state, obs)['orphans'] == [], "pending structure legs must be claimed"
    st['status'] = 'closing'
    assert reconcile(state, obs)['orphans'] == [], "closing structure legs must be claimed"
    assert st['status'] == 'closing'
    assert reconcile(state, {'positions': []})['missing'] == ['c1'] and st['status'] == 'closed'
    st.update(status='open')
    half = {'positions': [{'symbol': o, 'asset_class': 'us_option', 'qty': '-1'} for o in ('PS', 'PL')]}
    rec = reconcile(state, half)
    assert st['status'] == 'broken' and rec['orphans'] == ['PL', 'PS'], rec
    crossed = {'A': {'bid': 0.9, 'ask': 0.5, 't': None}}
    assert quotes_from_chain({'snapshots': {'A': {'latestQuote': {'bp': 0.9, 'ap': 0.5}}}}, ['A'])['A'] is None, \
        "a crossed book must not be treated as a price"
    zombie = {'client_order_id': 'z', 'status': 'pending', 'legs': s['legs'], 'qty': 1,
              'credit_limit': 1.0, 'expiry': '2099-01-01',
              'opened_at': (dt.datetime.now() - dt.timedelta(hours=2)).isoformat()}
    zs = {'structures': {'z': zombie}}
    assert reconcile(zs, {'positions': []})['missing'] == ['z'] and zombie['status'] == 'unfilled'
    unm = {'client_order_id': 'u', 'status': 'open', 'legs': s['legs'], 'qty': 1, 'credit_fill': None,
           'credit_limit': 0.0, 'expiry': '2099-01-01', 'opened_at': dt.datetime.now().isoformat()}
    held_all = {'positions': [{'symbol': l['occ'], 'asset_class': 'us_option', 'qty': '-1'} for l in s['legs']]}
    assert reconcile({'structures': {'u': unm}}, held_all)['unmanageable'] == ['u']
    args, lim = close_order_args(s, 0.33, rules, shadow=True)
    legs = json.loads(args[args.index('--legs') + 1])
    assert [l['position_intent'] for l in legs] == ['buy_to_close', 'sell_to_close',
                                                    'buy_to_close', 'sell_to_close']
    assert lim > 0 and '--dry-run' in args
    debit_s = {'client_order_id': 'd1', 'side': 'debit', 'legs': [
        {'occ': 'PS', 'side': 'sell'}, {'occ': 'PL', 'side': 'buy'}],
               'qty': 1, 'debit_fill': 0.80, 'expiry': '2099-01-01'}
    q_d = {'PS': {'bid': 0.10, 'ask': 0.12}, 'PL': {'bid': 1.40, 'ask': 1.45}}
    a, r, d = evaluate_exit(debit_s, q_d, rules)
    assert a == 'close' and 'take profit' in r, (a, r, d)
    print("structures.py self-check OK")
