"""
The verified-effect layer: every side-effecting broker call declares what it intends, and
the echo is checked against that intent before the agent believes it.

Two mechanisms, both architectural rather than incremental:

1. **Deterministic idempotency keys.** An order's `client_order_id` is a pure function of
   (cycle bucket, underlying, structure kind, expiry, strikes, qty). Two consequences:

     * a retry of the *same* intent -- after a timeout, a lost response, a cron overlap, a
       restart inside the same cadence bucket -- reuses the same key, and the broker refuses
       the duplicate. Before this, `client_order_id` was `uuid4()`, so a submission whose
       response was lost and then retried opened a SECOND condor on the same strikes;
     * a replayed cycle produces the same key as the recorded one, which is what makes
       `agent/replay.py` possible at all.

   A *changed* intent (a different limit after a walk, a different qty) is a different
   order and deliberately gets a different key -- with the previous one cancelled first,
   never left resting.

2. **Declared vs observed.** `verify_submission` compares what the broker echoed -- legs,
   sides, quantity, limit sign and magnitude -- against what we asked for. A mismatch is not
   logged and shrugged off; it returns a reason the caller turns into a halt. This is the
   generalisation of the structure registry's reconcile: the agent may only believe an
   effect it has confirmed.

Neither mechanism changes a trading decision. Both remove ways for the agent's model of the
account to drift from the account.

Self-check: `python3 agent/effects.py`
"""
import datetime as dt
import hashlib

KEY_MAX = 48                      # Alpaca's client_order_id limit


def cycle_bucket(now=None, cadence_minutes=15):
    """
    The cadence slot a cycle belongs to. Two runs inside one slot are the SAME attempt at
    the same opportunity (a cron overlap, a manual re-run, a restart), so they must produce
    the same order key; the next slot is a new opportunity.
    """
    now = now or dt.datetime.now()
    cadence = max(1, int(cadence_minutes))
    minute = (now.hour * 60 + now.minute) // cadence * cadence
    return f"{now:%Y%m%d}-{minute // 60:02d}{minute % 60:02d}"


def _digest(parts):
    return hashlib.sha1('|'.join(str(p) for p in parts).encode()).hexdigest()[:10]


def intent_parts(cand):
    """The identity of an opening intent: everything that makes it a different order."""
    if cand.get('structure') == 'condor':
        strikes = [cand['put_short_strike'], cand['put_long_strike'],
                   cand['call_short_strike'], cand['call_long_strike']]
    elif cand.get('structure') in ('short_strangle', 'short_straddle'):
        strikes = [cand['put_short_strike'], cand['call_short_strike']]
    else:
        strikes = [cand['short_strike'], cand['long_strike']]
    return [cand['underlying'], cand.get('structure') or cand.get('kind'),
            str(cand['expiry'])[:10], cand['qty']] + [f"{float(s):.2f}" for s in strikes]


def open_key(cand, now=None, cadence_minutes=15, bucket=None, strategy_tag=None):
    """Deterministic `client_order_id` for opening `cand` in this cadence slot."""
    b = bucket or cycle_bucket(now, cadence_minutes)
    tag = (strategy_tag or '').strip()
    prefix = f"{tag}-" if tag else ''
    return f"{prefix}agent-{b}-{_digest(intent_parts(cand))}"[:KEY_MAX]


def close_key(struct, attempt=0):
    """
    Close key. Attempt 0 reuses the structure's own id, so a re-run of the same close is
    idempotent. A later attempt walks the limit -- a genuinely different order -- and gets
    its own key; the caller must cancel the previous one rather than leave it resting.
    """
    base = f"close-{struct['client_order_id']}"
    return (base if attempt == 0 else f"{base}-a{attempt}")[:KEY_MAX]


def _legs_of(cand):
    if cand.get('structure') == 'condor':
        return [(cand['put_short_occ'], 'sell'), (cand['put_long_occ'], 'buy'),
                (cand['call_short_occ'], 'sell'), (cand['call_long_occ'], 'buy')]
    if cand.get('structure') in ('short_strangle', 'short_straddle'):
        return [(cand['put_short_occ'], 'sell'), (cand['call_short_occ'], 'sell')]
    return [(cand['short_occ'], 'sell'), (cand['long_occ'], 'buy')]


def verify_submission(cand, limit, result, tolerance=0.005):
    """
    Compare the broker's echo with the intent. Returns a list of mismatch strings; empty
    means the observed effect equals the declared one.

    An echo that carries no leg detail is NOT treated as agreement -- it is reported as
    unverifiable, so the caller can decide (we halt on the order path, tolerate in shadow).
    """
    if not isinstance(result, dict):
        return ['broker returned no order object']
    problems = []
    status = (result.get('status') or '').lower()
    if status in ('rejected', 'canceled', 'cancelled', 'expired'):
        return [f"order {status}: {result.get('reason') or 'no reason given'}"]

    want_qty = int(cand['qty'])
    got_qty = result.get('qty')
    if got_qty is not None and int(float(got_qty)) != want_qty:
        problems.append(f"qty: asked {want_qty}, broker echoed {got_qty}")

    got_limit = result.get('limit_price')
    if got_limit is not None:
        gl = float(got_limit)
        if gl > 0 and (cand.get('side') or 'credit') != 'debit':
            problems.append(f"limit sign: a credit spread must be submitted negative, echoed {gl}")
        elif gl < 0 and (cand.get('side') or 'credit') == 'debit':
            problems.append(f"limit sign: a debit spread must be submitted positive, echoed {gl}")
        elif abs(abs(gl) - abs(limit)) > tolerance:
            problems.append(f"limit: asked {limit}, broker echoed {gl}")

    legs = result.get('legs')
    if legs is None:
        problems.append('unverifiable: broker echo carries no legs')
    else:
        want = sorted(_legs_of(cand))
        got = sorted((l.get('symbol'), (l.get('side') or '').lower()) for l in legs)
        if want != got:
            problems.append(f"legs: asked {want}, broker echoed {got}")
    return problems


if __name__ == "__main__":
    cand = {'underlying': 'SPY', 'structure': 'condor', 'expiry': '2026-08-29', 'qty': 2,
            'put_short_strike': 640, 'put_long_strike': 635,
            'call_short_strike': 660, 'call_long_strike': 665,
            'put_short_occ': 'SPY260829P00640000', 'put_long_occ': 'SPY260829P00635000',
            'call_short_occ': 'SPY260829C00660000', 'call_long_occ': 'SPY260829C00665000'}
    t = dt.datetime(2026, 8, 28, 10, 7)

    # Same intent inside the slot -> same key (a retry cannot double-open).
    k1 = open_key(cand, t)
    assert k1 == open_key(cand, dt.datetime(2026, 8, 28, 10, 14)), "retry must reuse the key"
    # Next slot, or a different intent -> different key.
    assert k1 != open_key(cand, dt.datetime(2026, 8, 28, 10, 16))
    assert k1 != open_key(dict(cand, qty=3), t)
    assert k1 != open_key(dict(cand, put_short_strike=641), t)
    assert len(k1) <= KEY_MAX and k1.startswith('agent-20260828-1000')
    k_tag = open_key(cand, t, strategy_tag='pv6')
    assert k_tag.startswith('pv6-') and k_tag != k1

    # Close keys: idempotent first attempt, distinct walked attempts.
    st = {'client_order_id': k1}
    assert close_key(st) == f"close-{k1}"[:KEY_MAX] and close_key(st, 1).endswith('-a1')

    # Verification: a faithful echo passes...
    echo = {'status': 'accepted', 'qty': '2', 'limit_price': '-1.25',
            'legs': [{'symbol': o, 'side': s} for o, s in _legs_of(cand)]}
    assert verify_submission(cand, -1.25, echo) == []
    # ...a wrong quantity, a flipped sign, a missing leg and a silent rejection do not.
    assert verify_submission(cand, -1.25, dict(echo, qty='5'))[0].startswith('qty:')
    assert 'limit sign' in verify_submission(cand, -1.25, dict(echo, limit_price='1.25'))[0]
    debit_cand = dict(cand, structure='vertical', side='debit',
                      short_occ='SPY260829P00635000', long_occ='SPY260829P00640000',
                      short_strike=635, long_strike=640)
    debit_echo = {'status': 'accepted', 'qty': '2', 'limit_price': '0.80',
                  'legs': [{'symbol': o, 'side': s} for o, s in _legs_of(debit_cand)]}
    assert verify_submission(debit_cand, 0.80, debit_echo) == []
    assert 'limit sign' in verify_submission(debit_cand, 0.80, dict(debit_echo, limit_price='-0.80'))[0]
    assert verify_submission(cand, -1.25, dict(echo, legs=echo['legs'][:3]))[0].startswith('legs:')
    assert verify_submission(cand, -1.25, {'status': 'rejected', 'reason': 'no buying power'})[0] \
        .startswith('order rejected')
    # An echo with no legs is unverifiable, not "fine".
    assert verify_submission(cand, -1.25, {'status': 'accepted', 'qty': '2'})[0].startswith('unverifiable')
    print("effects.py self-check OK")
