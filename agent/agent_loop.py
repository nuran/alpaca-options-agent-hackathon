"""
The autonomous trading loop: one decision cycle per invocation.

Design principle, and the reason this file is shaped the way it is:
**the LLM proposes, deterministic code disposes.**

Risk limits, position sizing, order validation and exits live in Python with hard
numeric bounds. The model's only job is to choose among candidates that have ALREADY
passed every gate, or to pass entirely. It cannot raise a limit, skip a check, size a
position, or talk itself out of an exit. That is a direct response to the documented
failure mode in the LLM-trading literature -- FinAgent's analysis of FinMem shows it
"ignored the long-term downward trend and provided a buying rationale" off two
headlines. A model that can only pick from a risk-checked list cannot make that class
of mistake however confident its prose.

Order of operations (steps 0, 2 and 4 contain no model):
    0. Market gate      -- is the market open? enough time before close?
    1. Observe          -- account, positions, orders, chain, news (via Alpaca CLI)
    2. Risk gate        -- halts, exits, capacity. Forced closes happen here.
    3. Decide           -- ONE LLM call, strict JSON, PASS on any doubt
    4. Validate         -- re-check the model's pick against every rule
    5. Execute          -- marketable limit via `alpaca order submit`
    6. Journal          -- append every input, rationale, order and fill

Usage:
    python3 agent/agent_loop.py [--shadow] [--once] [--rules PATH] [--strategy ID] [--verbose]

    --shadow   run the full loop but pass --dry-run to every order (no positions taken)
    --once     single cycle then exit (the launchd timer's normal mode)
    --verbose  print the full observation payload

Live trading is structurally impossible here: no code path constructs `--live`, and
the paper host cannot touch real money.

Full instructions: see RUNBOOK.md.
"""
import datetime as dt
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import time

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, 'backtest'))
sys.path.insert(0, os.path.join(REPO_ROOT, 'agent'))

import blackscholes as bs
import structures as ST                  # before strategy: strategy→canon puts us_adaptive on path
import strategy as S                     # shared with the backtest: atm_iv, the vol gate
from engine import event_inside_horizon  # shared with the backtest: macro blackout
import effects as EFF                    # idempotency keys + declared-vs-observed verification
import skills as SK                      # P5/P8/V5 gates + ranked-universe loader
import canon as CANON                    # S1–S5 ranking, HAR forecast, Stage 5
import deploy as DEPLOY                  # multi-strategy paper deploy
from alpaca.env import load_env_var, env_flag

# The default book is account C's -- the submission book. In the development repo this
# pointed at agent/agent_rules.json, a retired rulebook that is not part of this
# submission; leaving it would have shipped a default that names a file nobody can read.
# Both shipped books are still reached explicitly with --profile, which is what the
# Makefile and the README use.
RULES_PATH = os.path.join(REPO_ROOT, 'agent', 'profiles', 'putcr-core6-d47',
                          'rules.json')
JOURNAL_DIR = os.path.join(REPO_ROOT, 'agent', 'decisions')

CONTRACT_MULTIPLIER = 100

# launchd runs with a minimal PATH (typically /usr/bin:/bin:/usr/sbin:/sbin), so a bare
# "alpaca" is NOT resolvable from a scheduled run even though it works in a shell. This
# killed the first scheduled cycle with FileNotFoundError. Resolve it once, absolutely.
CLI_CANDIDATES = [
    os.environ.get('ALPACA_CLI'),
    '/opt/homebrew/bin/alpaca',
    '/usr/local/bin/alpaca',
    os.path.expanduser('~/go/bin/alpaca'),
]


def _resolve_cli():
    for path in CLI_CANDIDATES:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    found = shutil.which('alpaca')
    if found:
        return found
    die("Alpaca CLI not found.",
        "Install it with `brew install alpacahq/tap/cli`, or set ALPACA_CLI to its "
        "absolute path. Note that a scheduled (launchd) run has a minimal PATH, so the "
        "binary must be resolvable without one.")


ALPACA_BIN = None
_EXCHANGE_DATE = None


def exchange_date():
    """
    Today's date AT THE EXCHANGE, not locally.

    This matters more than it looks. The Basic plan refuses SIP data for the current
    exchange day, so any historical request has to end strictly before it. Deriving
    that bound from the local clock breaks whenever the machine is ahead of New York:
    at 07:35 local (ET+8) the exchange is still on YESTERDAY, so `local_today - 1`
    lands on the current exchange day, the request is refused, and -- because the
    caller allows failure -- realized vol silently becomes None and the vol gate
    blocks every trade. Preflight caught exactly that.
    """
    global _EXCHANGE_DATE
    if _EXCHANGE_DATE is None:
        clock = cli(['clock'], allow_fail=True)
        stamp = (clock or {}).get('timestamp')
        if stamp:
            _EXCHANGE_DATE = dt.date.fromisoformat(stamp[:10])
        else:
            # Clock unreachable. Fall back to local minus one day, which is never
            # AHEAD of the exchange date from any timezone -- conservative in the
            # direction that keeps requests inside what the data plan will serve.
            _EXCHANGE_DATE = dt.date.today() - dt.timedelta(days=1)
    return _EXCHANGE_DATE


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}")


def die(msg, detail=None):
    print(f"ERROR: {msg}")
    if detail:
        print(detail)
    sys.exit(1)


# ------------------------------------------------------------------ Alpaca CLI

# Which dotenv entries hold this book's credentials. Defaults are the single-account
# names every existing invocation already uses, so nothing changes unless a rulebook
# names an account. A rules file that does gets BOTH halves: its own key pair, and the
# assert_account() check below that refuses to trade if those keys reach a different
# account than the one named. Naming env VARS rather than holding secrets keeps the
# rulebook committable -- this repo is public.
PROFILE_DIR = os.path.join(REPO_ROOT, 'agent', 'profiles')

KEY_ENV, SECRET_ENV = 'ALPACA_API_KEY', 'ALPACA_SECRET_KEY'

# Per-cycle analysis trace: what the agent SAW for each name and which gate ended its
# candidacy. The journal already records what was decided; it does not record why a
# name produced nothing, and "0 candidates passed the gates" is indistinguishable
# between an agent being selective and an agent that is quietly broken. Every entry is
# append-only and read by nothing that makes a decision -- recording must never be able
# to change what the agent does.
TRACE = []


def trace(underlying, stage, **fields):
    TRACE.append({'underlying': underlying, 'stage': stage, **fields})


def cli(args, allow_fail=False):
    """One Alpaca CLI call -> parsed JSON. Credentials go via env, never argv."""
    env = dict(os.environ)
    env['ALPACA_API_KEY'] = load_env_var(KEY_ENV)
    env['ALPACA_SECRET_KEY'] = load_env_var(SECRET_ENV)

    global ALPACA_BIN
    if ALPACA_BIN is None:
        ALPACA_BIN = _resolve_cli()

    proc = subprocess.run([ALPACA_BIN] + args + ['--quiet'],
                          capture_output=True, text=True, env=env, timeout=120)
    if proc.returncode == 2:
        die("Alpaca CLI auth failed.", proc.stderr.strip())
    if proc.returncode != 0 or not proc.stdout.strip():
        if allow_fail:
            return None
        die(f"alpaca {' '.join(args)} failed", (proc.stderr or proc.stdout)[:400])
    try:
        body = json.loads(proc.stdout)
    except json.JSONDecodeError:
        if allow_fail:
            return None
        die("unparseable CLI output", proc.stdout[:400])
    if isinstance(body, dict) and body.get('error'):
        if allow_fail:
            return None
        die(f"Alpaca: {body['error']}")
    return body


# ------------------------------------------------------------- 0. market gate

def market_gate(rules):
    clock = cli(['clock'])
    if not clock.get('is_open'):
        return False, f"market closed (next open {clock.get('next_open', '?')[:16]})"

    close_at = clock.get('next_close')
    if close_at:
        try:
            closes = dt.datetime.fromisoformat(close_at.replace('Z', '+00:00'))
            now = dt.datetime.now(dt.timezone.utc)
            minutes = (closes - now).total_seconds() / 60
            limit = rules['schedule']['no_new_positions_within_minutes_of_close']
            if minutes < limit:
                return True, f"only {minutes:.0f}min to close -- exits only"
        except ValueError:
            pass
    return True, "open"


# ----------------------------------------------------------------- 1. observe

def observe(rules, verbose=False):
    account = cli(['account', 'get'])
    positions = cli(['position', 'list'], allow_fail=True) or []
    orders = cli(['order', 'list', '--status', 'open'], allow_fail=True) or []

    equity = float(account.get('equity', 0))
    last_equity = float(account.get('last_equity', equity) or equity)

    obs = {
        'ts': dt.datetime.now().astimezone().isoformat(timespec='seconds'),
        'equity': equity,
        'last_equity': last_equity,
        'day_pnl': equity - last_equity,
        'day_pnl_pct': (equity - last_equity) / last_equity if last_equity else 0.0,
        'cash': float(account.get('cash', 0)),
        'options_buying_power': float(account.get('options_buying_power', 0) or 0),
        'options_level': account.get('options_trading_level'),
        'positions': positions,
        'open_orders': orders,
        'account_blocked': bool(account.get('account_blocked') or account.get('trading_blocked')),
    }
    if verbose:
        log(json.dumps({k: v for k, v in obs.items() if k != 'positions'}, indent=2))
    return obs


def option_positions(obs):
    return [p for p in obs['positions'] if p.get('asset_class') == 'us_option']


# --------------------------------------------------------------- 2. risk gate

def risk_gate(obs, rules, state, quotes=None, foreign_occs=None):
    """
    Deterministic. Returns (may_open, forced_actions, reasons).

    Runs BEFORE the model sees anything, so a halt is never a matter of persuasion.

    Order: broker block -> reconciliation (assignment / orphan legs -> flatten + halt)
    -> daily-loss halt -> drawdown halt -> structure-level exits -> flat-by date
    -> event blackout (condor book only; adaptive ranks S3) -> position cap.

    `quotes` maps OCC -> {bid, ask, ...} for the legs of every open structure; when
    None it is fetched from the live chain. Exits are evaluated on the structure's
    executable close cost, never on a leg's own unrealized P&L (see structures.py).
    """
    reasons, forced = [], []
    r = rules['risk']

    if obs['account_blocked']:
        return False, forced, ['account blocked by broker']

    # ---- reconciliation (fix D3): an assignment or a broken structure halts the agent
    rec = ST.reconcile(state, obs, pending_timeout_min=rules['execution'].get('pending_order_timeout_min', 30),
                       foreign_occs=foreign_occs)
    for stk in rec['stock']:
        forced.append({'action': 'flatten_stock', 'symbol': stk['symbol'], 'qty': stk['qty'],
                       'reason': 'stock position found (assignment?) -- flatten and halt'})
    for occ in rec['orphans']:
        forced.append({'action': 'close_leg', 'symbol': occ,
                       'reason': 'option leg belongs to no registered structure -- close and halt'})
    if rec['stock'] or rec['orphans']:
        state['halted'] = True
        state['halt_reason'] = 'RECONCILE_INCIDENT'
        reasons.append(f"reconcile incident: stock={len(rec['stock'])} orphan_legs={len(rec['orphans'])} "
                       f"-- flattening, agent halted, restart is a human decision")
        return False, forced, reasons
    for cid in rec['missing']:
        reasons.append(f"structure {cid} retired from the registry (closed, expired or never filled)")
    for cid in rec.get('unmanageable', []):
        reasons.append(f"ALERT structure {cid} has no recorded credit -- exits cannot be evaluated; "
                       f"close it by hand")

    if obs['day_pnl_pct'] <= -abs(r['daily_loss_halt_pct']):
        reasons.append(f"daily loss {obs['day_pnl_pct']:.2%} breached "
                       f"{r['daily_loss_halt_pct']:.0%} halt -- no new positions today")
        return False, forced, reasons

    peak = max(state.get('peak_equity', obs['equity']), obs['equity'])
    state['peak_equity'] = peak
    drawdown = (obs['equity'] - peak) / peak if peak else 0.0
    if drawdown <= -abs(r['max_drawdown_halt_pct']):
        reasons.append(f"drawdown {drawdown:.2%} breached "
                       f"{r['max_drawdown_halt_pct']:.0%} halt -- agent stopped, "
                       f"restart is a human decision")
        state['halted'] = True
        return False, forced, reasons

    # ---- exits, per STRUCTURE (fix D1). Forced here, not proposed to the model.
    open_structs = [st for st in ST.open_structures(state) if st['status'] == 'open']
    if open_structs and quotes is None:
        quotes = quotes_for_structures(open_structs, rules)
    for st in open_structs:
        action, why, detail = ST.evaluate_exit(st, quotes or {}, rules)
        st['last_eval'] = {'ts': obs.get('ts'), 'action': action, 'why': why, **detail}
        if action in ('close', 'close_urgent'):
            forced.append({'action': 'close_structure', 'client_order_id': st['client_order_id'],
                           'urgent': action == 'close_urgent', 'reason': why, 'detail': detail})
        elif why.startswith('STALE_MARK'):
            reasons.append(f"{st['client_order_id']}: {why}")

    flat_by = rules['schedule'].get('flat_by_date')
    if flat_by and dt.date.today() >= dt.date.fromisoformat(flat_by):
        for st in open_structs:
            if not any(f.get('client_order_id') == st['client_order_id'] for f in forced):
                forced.append({'action': 'close_structure', 'client_order_id': st['client_order_id'],
                               'urgent': False, 'reason': f"flat-by date {flat_by} reached"})
        reasons.append(f"flat-by date {flat_by} -- closing everything, no new positions")
        return False, forced, reasons

    # ---- macro events: the condor book still blackouts; portrait ranks S3
    events = [e['date'] if isinstance(e, dict) else e
              for e in rules['schedule'].get('event_blackout', [])]
    kind = rules['strategy'].get('structure') or rules['strategy'].get('kind')
    adaptive = kind in ('adaptive', 'best', 'picker', 'portrait')
    horizon = max(int(rules['strategy']['max_dte']),
                  int(rules['strategy'].get('max_dte_s3') or 12))
    if (not adaptive) and event_inside_horizon(events, dt.date.today(), horizon):
        reasons.append("macro event inside the entry horizon -- no new positions")
        return False, forced, reasons

    if len(open_structs) >= rules['risk']['max_concurrent_positions']:
        reasons.append(f"at position cap ({rules['risk']['max_concurrent_positions']})")
        return False, forced, reasons

    return True, forced, reasons


def quotes_for_structures(structs, rules):
    """Live quotes for every leg of the given structures, from the chain of each expiry."""
    out = {}
    max_age = rules['execution'].get('max_quote_age_s')
    by_key = {}
    for st in structs:
        exps = [str(st['expiry'])[:10]]
        if st.get('far_expiry'):
            exps.append(str(st['far_expiry'])[:10])
        by_key.setdefault((st['underlying'], min(exps), max(exps)), []).extend(
            l['occ'] for l in st['legs'])
    for (underlying, lo, hi), occs in by_key.items():
        chain = cli(['data', 'option', 'chain', '--underlying-symbol', underlying,
                     '--feed', 'indicative', '--limit', '500',
                     '--expiration-date-gte', lo, '--expiration-date-lte', hi],
                    allow_fail=True)
        out.update(ST.quotes_from_chain(chain, occs, max_age_s=max_age))
    return out


def _underlying_ohlcv(underlying, n=400):
    """Daily OHLCV strictly before the current exchange day. Empty lists on failure."""
    end = exchange_date() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=int(n * 1.8) + 10)
    body = cli(['data', 'multi-bars', '--symbols', underlying, '--timeframe', '1Day',
                '--start', str(start), '--end', str(end), '--feed', 'sip',
                '--adjustment', 'split'], allow_fail=True)
    if not body:
        body = cli(['data', 'multi-bars', '--symbols', underlying, '--timeframe', '1Day',
                    '--start', str(start), '--end', str(end), '--feed', 'iex',
                    '--adjustment', 'split'], allow_fail=True)
    if not body:
        return [], [], [], [], []
    bars = (body.get('bars') or {}).get(underlying) or []
    o, h, l, c, v = [], [], [], [], []
    for b in bars:
        if not b.get('c'):
            continue
        close = b['c']
        o.append(b.get('o') or close)
        h.append(b.get('h') or close)
        l.append(b.get('l') or close)
        c.append(close)
        v.append(b.get('v') or 0)
    return o, h, l, c, v


def _underlying_closes(underlying, window=21):
    """Daily closes strictly before the current exchange day. Empty list on failure."""
    o, h, l, c, v = _underlying_ohlcv(underlying, window * 2 + 10)
    return c[-window * 2:] if c else []


def realized_vol(underlying, window=21):
    """
    Annualized trailing realized volatility of the underlying, from daily closes
    strictly before the current exchange day.

    The live counterpart of Store.realized_vol. Returns None when the history is
    unavailable -- and the caller treats None as "do not trade", never as "trade
    without the gate".
    """
    closes = _underlying_closes(underlying, window)[-(window + 1):]
    if len(closes) < 3:
        return None
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 2:
        return None
    return statistics.stdev(rets) * math.sqrt(252)


# ------------------------------------------------- universe + structure picker

def _ranked_symbols(doc, key, n):
    out = []
    for r in (doc or {}).get(key) or []:
        s = r.get('symbol') if isinstance(r, dict) else r
        if s and s not in out:
            out.append(s)
        if len(out) >= n:
            break
    return out


def resolve_universe(rules):
    """
    Names this cycle will snapshot.

    Selector ShortVol is the interesting-asset path. LongConvex names are included
    so cheap-IV sessions can express debit convexity (S4–S7 mapping). Selling
    premium on an LC name still requires the HAR gate to say IV is rich.
    `selector.always` pins a calibrated floor (SPY) without dumping the CORE six.
    """
    u = rules.get('universe') or {}
    fallback = list(u.get('underlyings') or ['SPY'])
    mode = u.get('mode') or 'fixed'
    sel = u.get('selector') or {}
    top_n = int(sel.get('top_n') or 3)
    always = [s for s in (sel.get('always') or []) if s]
    branches = {}
    info = {'mode': mode, 'ranked': False, 'skipped_long_convex': [], 'dropped': []}

    names = []
    if mode == 'fixed':
        names = list(fallback)
        for n in names:
            branches[n] = 'short_vol'
    else:
        ranked = SK.load_ranked(rules)
        if ranked:
            info['ranked'] = True
            info['as_of'] = ranked.get('as_of')
            for s in _ranked_symbols(ranked, 'shortlist_sv', top_n):
                names.append(s)
                branches[s] = 'short_vol'
            for s in _ranked_symbols(ranked, 'shortlist_lc', top_n):
                if s in branches:
                    continue
                names.append(s)
                branches[s] = 'long_convex'
        else:
            names = list(fallback)
            for n in names:
                branches[n] = 'short_vol'
            info['fallback'] = 'no_ranked_file'

    for s in reversed(always):
        if s not in names:
            names.insert(0, s)
        branches.setdefault(s, 'short_vol')

    names, dropped = SK.filter_underlyings(names, rules)
    info['dropped'] = [{'symbol': a, 'reason': b} for a, b in dropped]
    if not names:
        names = list(fallback) or ['SPY']
        for n in names:
            branches.setdefault(n, 'short_vol')
        info['fallback'] = 'empty_after_skill_filter'
    return names, branches, info


def watch_symbols(rules, candidates=None):
    names, _, _ = resolve_universe(rules)
    seen = list(names)
    for c in candidates or []:
        u = c.get('underlying')
        if u and u not in seen:
            seen.append(u)
    return seen


def infer_spot(rows):
    """ATM proxy from the live chain: strike nearest 50-delta."""
    side = [r for r in rows if r.get('right') == 'C' and r.get('delta') is not None]
    if not side:
        side = [r for r in rows if r.get('right') == 'P' and r.get('delta') is not None]
    if not side:
        return None
    return min(side, key=lambda r: abs(abs(r['delta']) - 0.50))['strike']


def dollar_width(spot, s):
    return S.dollar_width(spot, s)


def choose_short(condor, put, floor=0.90):
    return S.choose_short(condor, put, floor)


def _rows_as_chain(rows, today):
    """Map live chain rows onto the backtest contract shape (mid as close)."""
    out = []
    for r in rows:
        exp = r['expiry']
        if not isinstance(exp, dt.date):
            exp = dt.date.fromisoformat(str(exp)[:10])
        out.append({
            'occ': r['occ'], 'strike': r['strike'],
            'opt_right': r.get('opt_right') or r['right'],
            'right': r.get('right') or r.get('opt_right'),
            'expiry': exp, 'close': r.get('close') or r['mid'],
            'delta': r['delta'], 'iv': r.get('iv'),
            'dte': (exp - today).days, 'volume': 1,
            'spread_pct': r.get('spread_pct') or 0,
            'underlying': r.get('underlying'),
        })
    return out


def _worst_spread(rows_by_occ, occs):
    xs = [rows_by_occ[o].get('spread_pct') or 0 for o in occs if o in rows_by_occ]
    return round(max(xs), 4) if xs else 0.0


def risk_scale(state, rules, equity):
    """
    Vol-target overlay matching backtest/engine.Engine.risk_scale.

    Live has no marked equity curve between fills, so we keep a short ring of
    cycle-to-cycle equity returns on state and scale the per-trade budget by
    target / trailing vol. Off when risk.vol_target_pct is null/0.
    """
    target = (rules.get('risk') or {}).get('vol_target_pct')
    if not target:
        return 1.0
    hist = list(state.get('equity_history') or [])
    if not (hist and hist[-1].get('equity') == equity):
        if hist:
            prev = hist[-1]['equity']
            if prev and prev > 0:
                hist[-1] = dict(hist[-1], ret=(equity / prev) - 1.0)
        hist.append({'equity': equity, 'ret': None})
        win = int((rules.get('risk') or {}).get('vol_window') or 21)
        state['equity_history'] = hist[-(win + 5):]

    rets = [h['ret'] for h in (state.get('equity_history') or [])
            if h.get('ret') is not None]
    if len(rets) < 15:
        return 1.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    vol = (var ** 0.5) * (252 ** 0.5)
    risk = rules['risk']
    if vol <= 1e-9:
        return float(risk.get('vol_scale_max', 2.0))
    scale = float(target) / vol
    return max(float(risk.get('vol_scale_min', 0.25)),
               min(float(risk.get('vol_scale_max', 2.0)), scale))


def _qty_from_risk(per_unit, obs, risk):
    scale = float(obs.get('risk_scale') or 1.0)
    eq_frac = float(obs.get('equity_fraction') or 1.0)
    budget = obs['equity'] * risk['max_risk_per_trade_pct'] * scale * eq_frac
    return min(int(budget // max(per_unit, 0.01)), risk['max_contracts_per_trade'])


def _spread_to_live(sp, rows, obs, risk, underlying):
    """Size a backtest spread against live equity and attach quote-spread checks."""
    by = {r['occ']: r for r in rows}
    st = sp.get('structure')
    exp = str(sp['expiry'])[:10]
    dte = sp.get('dte')
    if st == 'condor':
        credit = sp['raw_credit']
        risk_width = max(sp['put_width'], sp['call_width'])
        per = max(risk_width - credit, 0.01) * CONTRACT_MULTIPLIER
        qty = _qty_from_risk(per, obs, risk)
        if qty <= 0:
            return None
        occs = [sp['short_occ'], sp['long_occ'], sp['call_short_occ'], sp['call_long_occ']]
        return {
            'id': f"{underlying}-IC-{exp}-{sp['short_strike']:.0f}/{sp['call_short_strike']:.0f}",
            'underlying': underlying, 'kind': 'iron_condor', 'structure': 'condor',
            'side': 'credit', 'expiry': exp, 'dte': dte,
            'put_short_occ': sp['short_occ'], 'put_long_occ': sp['long_occ'],
            'put_short_strike': sp['short_strike'], 'put_long_strike': sp['long_strike'],
            'call_short_occ': sp['call_short_occ'], 'call_long_occ': sp['call_long_occ'],
            'call_short_strike': sp['call_short_strike'], 'call_long_strike': sp['call_long_strike'],
            'put_short_delta': sp['short_delta'], 'call_short_delta': sp.get('call_short_delta'),
            'net_delta': round(sp.get('net_delta') or 0, 4),
            'short_iv': sp.get('short_iv'), 'iv_atm': sp.get('iv_atm'),
            'width': risk_width, 'put_width': sp['put_width'], 'call_width': sp['call_width'],
            'credit': round(credit, 3), 'credit_ratio': round(sp['credit_ratio'], 3),
            'qty': qty, 'max_loss': round(per * qty, 2),
            'max_profit': round(credit * CONTRACT_MULTIPLIER * qty, 2),
            'worst_leg_spread_pct': _worst_spread(by, occs),
        }
    if st in ('short_strangle', 'short_straddle'):
        credit = sp['raw_credit']
        per = sp.get('max_loss_unit') or 2.0 * credit * CONTRACT_MULTIPLIER
        qty = _qty_from_risk(per, obs, risk)
        if qty <= 0:
            return None
        occs = [sp['put_short_occ'], sp['call_short_occ']]
        return {
            'id': f"{underlying}-{st}-{exp}-{sp['put_short_strike']:.0f}/{sp['call_short_strike']:.0f}",
            'underlying': underlying, 'kind': sp['kind'], 'structure': st, 'side': 'credit',
            'expiry': exp, 'dte': dte,
            'short_occ': sp['put_short_occ'], 'long_occ': sp['call_short_occ'],
            'short_strike': sp['put_short_strike'], 'long_strike': sp['call_short_strike'],
            'put_short_occ': sp['put_short_occ'], 'put_short_strike': sp['put_short_strike'],
            'call_short_occ': sp['call_short_occ'], 'call_short_strike': sp['call_short_strike'],
            'short_delta': sp.get('short_delta'), 'short_iv': sp.get('short_iv'),
            'iv_atm': sp.get('iv_atm'), 'width': sp['width'],
            'credit': round(credit, 3), 'credit_ratio': round(sp.get('credit_ratio') or 0, 3),
            'qty': qty, 'max_loss': round(per * qty, 2), 'max_loss_unit': per,
            'max_profit': round(credit * CONTRACT_MULTIPLIER * qty, 2),
            'worst_leg_spread_pct': _worst_spread(by, occs),
        }
    if st == 'calendar':
        side = sp.get('side') or 'credit'
        if side == 'debit':
            prem = sp.get('raw_debit') or 0
            per = sp.get('max_loss_unit') or 1.5 * prem * CONTRACT_MULTIPLIER
            qty = _qty_from_risk(per, obs, risk)
            if qty <= 0:
                return None
            occs = [sp['short_occ'], sp['long_occ']]
            return {
                'id': f"{underlying}-CAL-{exp}-{sp['short_strike']:.0f}",
                'underlying': underlying, 'kind': 'calendar', 'structure': 'calendar',
                'side': 'debit', 'right': sp.get('right'), 'expiry': exp,
                'far_expiry': str(sp.get('far_expiry') or '')[:10], 'dte': dte,
                'short_occ': sp['short_occ'], 'long_occ': sp['long_occ'],
                'short_strike': sp['short_strike'], 'long_strike': sp['long_strike'],
                'short_delta': sp.get('short_delta'), 'short_iv': sp.get('short_iv'),
                'width': sp['width'], 'debit': round(prem, 3), 'credit': 0.0,
                'credit_ratio': round(sp.get('credit_ratio') or 0, 3),
                'qty': qty, 'max_loss': round(per * qty, 2), 'max_loss_unit': per,
                'max_profit': round(max(sp['width'] - prem, 0) * CONTRACT_MULTIPLIER * qty, 2),
                'worst_leg_spread_pct': _worst_spread(by, occs),
            }
        credit = sp['raw_credit']
        per = sp.get('max_loss_unit') or 1.5 * (sp.get('long_close') or credit) * CONTRACT_MULTIPLIER
        qty = _qty_from_risk(per, obs, risk)
        if qty <= 0:
            return None
        occs = [sp['short_occ'], sp['long_occ']]
        return {
            'id': f"{underlying}-CAL-{exp}-{sp['short_strike']:.0f}",
            'underlying': underlying, 'kind': 'calendar', 'structure': 'calendar',
            'side': 'credit', 'right': sp.get('right'), 'expiry': exp,
            'far_expiry': str(sp.get('far_expiry') or '')[:10], 'dte': dte,
            'short_occ': sp['short_occ'], 'long_occ': sp['long_occ'],
            'short_strike': sp['short_strike'], 'long_strike': sp['long_strike'],
            'short_delta': sp.get('short_delta'), 'short_iv': sp.get('short_iv'),
            'width': sp['width'], 'credit': round(credit, 3),
            'credit_ratio': round(sp.get('credit_ratio') or 0, 3),
            'qty': qty, 'max_loss': round(per * qty, 2), 'max_loss_unit': per,
            'max_profit': round(credit * CONTRACT_MULTIPLIER * qty, 2),
            'worst_leg_spread_pct': _worst_spread(by, occs),
        }
    if st == 'vertical_debit':
        debit = sp['raw_debit']
        per = debit * CONTRACT_MULTIPLIER
        qty = _qty_from_risk(per, obs, risk)
        if qty <= 0:
            return None
        occs = [sp['short_occ'], sp['long_occ']]
        return {
            'id': f"{underlying}-{sp['right']}D-{exp}-{sp['long_strike']:.0f}",
            'underlying': underlying, 'kind': sp['kind'], 'structure': 'vertical_debit',
            'side': 'debit', 'right': sp['right'], 'expiry': exp, 'dte': dte,
            'short_occ': sp['short_occ'], 'long_occ': sp['long_occ'],
            'short_strike': sp['short_strike'], 'long_strike': sp['long_strike'],
            'short_delta': sp.get('short_delta'), 'short_iv': sp.get('short_iv'),
            'iv_atm': sp.get('iv_atm'), 'width': sp['width'],
            'debit': round(debit, 3), 'credit': 0.0,
            'credit_ratio': round(sp.get('credit_ratio') or 0, 3),
            'qty': qty, 'max_loss': round(per * qty, 2),
            'max_profit': round((sp['width'] - debit) * CONTRACT_MULTIPLIER * qty, 2),
            'worst_leg_spread_pct': _worst_spread(by, occs),
        }
    credit = sp['raw_credit']
    per = max(sp['width'] - credit, 0.01) * CONTRACT_MULTIPLIER
    qty = _qty_from_risk(per, obs, risk)
    if qty <= 0:
        return None
    occs = [sp['short_occ'], sp['long_occ']]
    return {
        'id': f"{underlying}-{sp.get('right')}-{exp}-{sp['short_strike']:.0f}",
        'underlying': underlying, 'kind': sp['kind'], 'structure': 'vertical',
        'side': 'credit', 'right': sp.get('right'), 'expiry': exp, 'dte': dte,
        'short_occ': sp['short_occ'], 'long_occ': sp['long_occ'],
        'short_strike': sp['short_strike'], 'long_strike': sp['long_strike'],
        'short_delta': sp.get('short_delta'), 'short_iv': sp.get('short_iv'),
        'iv_atm': sp.get('iv_atm'), 'width': sp['width'],
        'credit': round(credit, 3), 'credit_ratio': round(sp.get('credit_ratio') or 0, 3),
        'qty': qty, 'max_loss': round(per * qty, 2),
        'max_profit': round(credit * CONTRACT_MULTIPLIER * qty, 2),
        'worst_leg_spread_pct': _worst_spread(by, occs),
    }


def pick_short_structure(rows, s, obs, risk, underlying, vix=None, branch=None, out=None):
    """One structure per name, chosen by S1–S5 ranking — not by credit/width."""
    spot = infer_spot(rows)
    cfg = dict(s)
    cfg['width'] = dollar_width(spot, s)
    cfg['underlying'] = underlying
    cfg['spot'] = spot
    today = dt.date.today()
    cfg['asof'] = today
    o, h, l, c, vol = _underlying_ohlcv(underlying, 400)
    closes = c
    rv = None
    slice_ = closes[-(s.get('rv_window', 21) + 1):]
    if len(slice_) >= 3:
        rets = [math.log(b / a) for a, b in zip(slice_, slice_[1:]) if a > 0 and b > 0]
        if len(rets) >= 2:
            rv = statistics.stdev(rets) * math.sqrt(252)
    rv_f = CANON.har_forecast_rv(o, h, l, c, s.get('har_horizon_days') or CANON.HAR_HORIZON) if o else None
    mets = CANON.ohlcv_metrics(o, h, l, c, vol) if o else None
    cfg['rv_forecast'] = rv_f
    if mets:
        cfg['ohlcv_metrics'] = mets
        cfg['event_mode'] = bool((mets.get('earnings_sig') or 0) >= CANON.EARNINGS_SIG)
    cfg['vix_context'] = vix or CANON.vix_context(SK.load_ranked())
    events = [e['date'] if isinstance(e, dict) else e
              for e in (s.get('event_blackout') or [])]
    horizon = max(int(s.get('max_dte') or 7), int(s.get('max_dte_s3') or 12))
    cfg['earnings_in_window'] = event_inside_horizon(events, today, horizon)
    if branch == 'long_convex' and mets:
        cfg['event_mode'] = cfg.get('event_mode') or False
    chain = _rows_as_chain(rows, today)
    if (cfg.get('structure') or cfg.get('kind')) == 'playbook':
        import playbook as PB
        trend, _ms = CANON.path_shape(closes, rv_f or rv, cfg)
        picked, portrait = PB.select(
            chain, cfg, spot, rv_f or rv, trend,
            metrics=cfg.get('ohlcv_metrics'),
            event=bool(cfg.get('event_mode') or cfg.get('earnings_in_window')))
        portrait = dict(portrait); portrait['trend'] = trend
        if out is not None:
            # The router's binding reason and both side edges, so a decline is
            # readable. Without this the trace can only say "the builder declined",
            # which is the exact non-answer the analysis log exists to replace.
            out.update(portrait)
    else:
        portrait = S.classify_setup(spot, chain, rv, closes, cfg)
        picked = S.pick_for_setup(chain, cfg, portrait)
    chosen = _spread_to_live(picked[0], rows, obs, risk, underlying) if picked else None
    if chosen:
        chosen = dict(chosen)
        chosen['width_used'] = cfg['width']
        chosen['portrait'] = portrait.get('reason')
        chosen['setup_trend'] = portrait.get('trend')
        chosen['setup_iv_rv'] = portrait.get('iv_rv')
        chosen['setup_rank'] = portrait.get('rank')
        chosen['rv_forecast'] = round(rv_f, 4) if rv_f else None
        size_mult = (cfg.get('vix_context') or {}).get('size_mult') or 1.0
        if size_mult < 1.0 and chosen.get('qty'):
            old_qty = chosen['qty']
            chosen['qty'] = max(1, int(old_qty * size_mult))
            scale = chosen['qty'] / old_qty
            chosen['max_loss'] = round(chosen['max_loss'] * scale, 2)
            chosen['max_profit'] = round(chosen['max_profit'] * scale, 2)
    return chosen


# ------------------------------------------------- candidate generation (code)

def resolve_profile(args):
    """
    Work out which rulebook to load and where its journal lives.

    Three ways in, and the earlier one wins so existing invocations keep working:

      --strategy ID   -> agent/strategies/ID/   (deploy.py, gated on DEPLOY_ALLOW)
      --rules PATH    -> that file, journal in <dirname>/decisions
      --profile NAME  -> agent/profiles/NAME/rules.json, journal alongside it
      (nothing)       -> agent/profiles/putcr-core6-d47/ (account C, the default book)

    Returning the journal directory BESIDE the rules file rather than from a second
    flag is what makes a profile atomic: there is no way to load one profile's rules
    and write another profile's journal. With the `account` block in the rulebook,
    each profile then owns its rules, journal, state AND account.

    Note the default: a bare invocation loads account C's rulebook, so an
    invocation with no flags trades the submission book rather than failing. Profiles
    are still the supported path -- the Makefile always passes one.
    """
    if '--profile' not in args:
        return None, None
    name = args[args.index('--profile') + 1]
    if os.sep in name or name in ('.', '..'):
        die(f"--profile takes a name, not a path: {name}")
    base = os.path.join(PROFILE_DIR, name)
    return os.path.join(base, 'rules.json'), os.path.join(base, 'decisions')


def assert_account(rules):
    """
    Refuse to trade unless the broker agrees we are on the account this book names.

    Without it, pointing two books at one credential pair is undetectable: both trade
    the same account while writing two separate, healthy-looking journals, and any
    comparison between them is meaningless. Absent an account_id, skip rather than
    guess -- a rulebook written before this field existed is not an error.
    """
    want = (rules.get('account') or {}).get('account_id')
    if not want:
        return None
    got = cli(['account', 'get'], allow_fail=True) or {}
    have = got.get('account_number') or got.get('id')
    if not have:
        die("cannot read the account to verify it -- refusing to trade")
    if have != want:
        die(f"WRONG ACCOUNT: this book expects {want}, credentials reach {have}. "
            f"Check {KEY_ENV}/{SECRET_ENV} in the dotenv file.")
    return have


def build_candidates(rules, obs, names=None, branches=None):
    """
    Every tradable credit spread right now, already risk-checked.

    Uses the LIVE chain, which unlike the historical bars carries Alpaca's own Greeks
    and IV -- no Black-Scholes inversion needed here. The backtest recovers delta by
    inversion precisely so the two agree on what a "0.20 delta short" means.

    Contest default in agent_rules.json is SPY iron condor, DTE 4-7. Adaptive/S1-S5
    ranking still exists when strategy.structure is adaptive; it is not the live book.
    """
    s = dict(rules['strategy'])
    ex, risk = rules['execution'], rules['risk']
    s['event_blackout'] = [e['date'] if isinstance(e, dict) else e
                           for e in rules['schedule'].get('event_blackout', [])]
    today = dt.date.today()
    out = []
    if names is None:
        names, branches, _ = resolve_universe(rules)
    branches = branches or {}
    vix = CANON.vix_context(SK.load_ranked(rules), rules)

    for underlying in names:
        lo = (today + dt.timedelta(days=s['min_dte'])).isoformat()
        hi_days = int(s['max_dte'])
        if (s.get('structure') or s.get('kind')) in ('adaptive', 'best', 'picker', 'portrait'):
            hi_days = max(hi_days, int(s.get('max_dte_s3') or 12))
        hi = (today + dt.timedelta(days=hi_days)).isoformat()

        # Live/backtest parity on the flat-by date. `risk_gate` treats flat_by as a
        # trigger -- on that date it closes everything -- but until now nothing stopped
        # the agent OPENING a structure that expires after it. backtest/engine.py:491
        # filters the chain to `expiry <= flat_by`, so the backtest never books a trade
        # it cannot hold to expiry; live would have opened Sep 4-7 expiries on Aug 31 and
        # had them force-liquidated on Sep 3, one to four days early. Closing a short put
        # spread ahead of expiry hands back the spread on positions that would mostly
        # have expired worthless -- a systematic exit that appears in no backtest number.
        # Clamping the chain window here is the same rule the engine applies, in the same
        # place in the pipeline.
        flat_by = rules['schedule'].get('flat_by_date')
        if flat_by and flat_by < hi:
            hi = flat_by
        if hi < lo:
            trace(underlying, 'no_expiry_before_flat_by', dte_window=[lo, hi],
                  flat_by=flat_by)
            continue          # nothing in the DTE window expires on or before flat-by

        chain = cli(['data', 'option', 'chain', '--underlying-symbol', underlying,
                     '--feed', 'indicative', '--limit', '500',
                     '--expiration-date-gte', lo, '--expiration-date-lte', hi],
                    allow_fail=True)
        if not chain:
            trace(underlying, 'no_chain', dte_window=[lo, hi])
            continue

        snaps = chain.get('snapshots') or {}
        rows = []
        # Why contracts fall out before any structure is considered. Counted rather
        # than listed: 500 contracts per name would bury the signal.
        drops = {'unpriced': 0, 'stale_quote': 0, 'too_cheap': 0, 'spread_too_wide': 0,
                 'unparsable': 0}
        now_utc = dt.datetime.now(dt.timezone.utc)
        max_age = ex.get('max_quote_age_s')
        for occ, snap in snaps.items():
            g = snap.get('greeks') or {}
            q = snap.get('latestQuote') or {}
            bid, ask = q.get('bp'), q.get('ap')
            delta = g.get('delta')
            if not bid or not ask or delta is None or ask <= 0:
                drops['unpriced'] += 1
                continue
            if max_age and q.get('t'):
                try:
                    age = (now_utc - dt.datetime.fromisoformat(q['t'].replace('Z', '+00:00'))).total_seconds()
                except ValueError:
                    age = None
                if age is not None and age > max_age:
                    drops['stale_quote'] += 1
                    continue  # stale quote -- not priced, never a candidate (fix D7)
            mid = (bid + ask) / 2
            if mid <= 0.02:
                drops['too_cheap'] += 1
                continue
            spread_pct = (ask - bid) / mid
            if spread_pct > ex['max_spread_pct_of_mid']:
                drops['spread_too_wide'] += 1
                continue  # untrustworthy quote -- reject before the model sees it
            try:
                _, expiry, right, strike = parse_occ(occ, underlying)
            except ValueError:
                drops['unparsable'] += 1
                continue
            rows.append({'occ': occ, 'underlying': underlying, 'expiry': expiry,
                         'strike': strike, 'right': right, 'bid': bid, 'ask': ask,
                         'mid': mid, 'delta': delta, 'iv': snap.get('impliedVolatility'),
                         'spread_pct': spread_pct})

        # Last close from the same OHLCV helper the gates use, so the traced spot is
        # the number the agent actually reasoned about rather than a second lookup.
        spot_px = None
        try:
            _o, _h, _l, _c, _v = _underlying_ohlcv(underlying, 5)
            spot_px = round(_c[-1], 2) if _c else None
        except Exception:
            pass
        trace(underlying, 'chain', spot=spot_px, contracts=len(snaps),
              priced=len(rows), dropped=drops, dte_window=[lo, hi])

        before = len(out)
        kind = s.get('structure') or s.get('kind')
        if kind == 'playbook':
            # The vertical-vs-condor router. Same module the backtest uses, reached
            # through strategy.generate_candidates, so live and backtest cannot
            # disagree about which family a given tape routes to.
            route = {}
            cand = pick_short_structure(rows, s, obs, risk, underlying, vix=vix,
                                        branch=branches.get(underlying), out=route)
            if route:
                pe = (route.get('put') or {}); ce = (route.get('call') or {})
                trace(underlying, 'router', family=route.get('family'),
                      reason=route.get('reason'), trend=route.get('trend'),
                      put_edge=pe.get('edge'), put_reason=pe.get('reason'),
                      call_edge=ce.get('edge'), call_reason=ce.get('reason'),
                      required=pe.get('required'))
            if cand:
                out.append(cand)
        elif kind in ('adaptive', 'best', 'picker'):
            cand = pick_short_structure(rows, s, obs, risk, underlying, vix=vix,
                                        branch=branches.get(underlying))
            if cand:
                cand['selector_branch'] = branches.get(underlying, 'short_vol')
                out.append(cand)
        elif kind == 'condor' or s.get('kind') == 'iron_condor':
            cand = pick_condor(rows, s, obs, risk, underlying)
            if cand:
                out.append(cand)
        else:
            rights = (['P'] if s.get('side') == 'put' else
                      ['C'] if s.get('side') == 'call' else ['P'])
            # 'both' used to rank call vs put by credit/width. That path is closed.
            for right in rights:
                spread = pick_vertical(rows, right, s, obs, risk)
                if spread:
                    out.append(spread)

        if len(out) == before:
            # The structure builder found nothing tradable in a chain it could price.
            # It does not report which of its own gates bound (strike band, credit
            # ratio, width, matching expiry), so this says only that it declined --
            # honest about the limit rather than inventing a reason.
            trace(underlying, 'no_structure', kind=kind, priced=len(rows))
        else:
            for c in out[before:]:
                # A condor names its legs put_short_strike / call_short_strike; a
                # vertical names its one leg short_strike. Reading only the vertical
                # keys printed "short None delta None" over a perfectly good condor.
                if c.get('structure') == 'condor':
                    strikes = f"{c.get('put_short_strike')}/{c.get('call_short_strike')}"
                    dlt = c.get('net_delta')
                    dlabel = 'net delta'
                else:
                    strikes = c.get('short_strike')
                    dlt = c.get('short_delta')
                    dlabel = 'delta'
                trace(underlying, 'structure_built', kind=kind, id=c.get('id'),
                      expiry=str(c.get('expiry'))[:10], short_strike=strikes,
                      credit=c.get('credit'), credit_ratio=c.get('credit_ratio'),
                      short_delta=dlt, delta_label=dlabel, short_iv=c.get('short_iv'))

    # The vol gate: sell only when implied is rich against trailing realized. Applied
    # to the finished structure, matching backtest/strategy.py exactly -- if these two
    # ever diverge, the backtest stops describing the live agent. strategy.vol_gate_iv
    # selects the implied side: 'short_leg' (short put IV, the backtested incumbent) or
    # 'atm' (at-the-forward IV via strategy.atm_iv; tested 2026-08-28, not better).
    # Portrait already gated on ATM IV vs RV. Do not also require the short-put 1.2x
    # bar on the finished trade — that is the always-condor book.
    if s.get('min_iv_rv_ratio') and (s.get('structure') or s.get('kind')) not in (
            'adaptive', 'best', 'picker', 'portrait'):
        gated = []
        for c in out:
            rv = realized_vol(c['underlying'], s.get('rv_window', 21))
            if not rv or rv <= 0:
                trace(c['underlying'], 'gate_vol', id=c.get('id'), passed=False,
                      reason='no realized vol -- cannot verify richness')
                continue   # cannot verify richness -> do not trade
            c['realized_vol'] = round(rv, 4)
            gate_iv = c.get('iv_atm') if s.get('vol_gate_iv', 'short_leg') == 'atm' else c.get('short_iv')
            c['gate_iv'] = round(gate_iv, 4) if gate_iv else None
            c['iv_rv_ratio'] = round(gate_iv / rv, 3) if gate_iv else None
            ok = bool(c['iv_rv_ratio'] and c['iv_rv_ratio'] >= s['min_iv_rv_ratio'])
            trace(c['underlying'], 'gate_vol', id=c.get('id'), passed=ok,
                  iv=c['gate_iv'], rv=c['realized_vol'], ratio=c['iv_rv_ratio'],
                  required=s['min_iv_rv_ratio'],
                  which_iv=s.get('vol_gate_iv', 'short_leg'))
            if ok:
                gated.append(c)
        out = gated

    # Second gate: overnight variance share (feature-lab candidate). null = off.
    max_on = s.get('max_overnight_share')
    if max_on is not None and out:
        max_on = float(max_on)
        kept = []
        for c in out:
            o, h, l, cl, vol = _underlying_ohlcv(c['underlying'], 400)
            mets = CANON.ohlcv_metrics(o, h, l, cl, vol) if o else None
            share = (mets or {}).get('overnight_share')
            if share is None:
                trace(c['underlying'], 'gate_overnight', id=c.get('id'), passed=False,
                      reason='no overnight-share estimate')
                continue  # cannot verify -> skip
            c['overnight_share'] = round(share, 4)
            trace(c['underlying'], 'gate_overnight', id=c.get('id'),
                  passed=bool(share <= max_on), share=c['overnight_share'],
                  max_allowed=max_on)
            if share <= max_on:
                kept.append(c)
        out = kept

    # Cycle-2 gate: IV / HAR forecast. null = off.
    min_ih = s.get('min_iv_har_ratio')
    if min_ih is not None and out:
        min_ih = float(min_ih)
        kept = []
        for c in out:
            o, h, l, cl, vol = _underlying_ohlcv(c['underlying'], 400)
            har = CANON.har_forecast_rv(o, h, l, cl, CANON.HAR_HORIZON) if o else None
            iv = c.get('gate_iv') or c.get('short_iv')
            if not har or har <= 0 or not iv:
                trace(c['underlying'], 'gate_iv_har', id=c.get('id'), passed=False,
                      reason='no HAR forecast')
                continue
            ratio = iv / har
            c['iv_har_ratio'] = round(ratio, 3)
            trace(c['underlying'], 'gate_iv_har', id=c.get('id'),
                  passed=bool(ratio >= min_ih), iv=round(iv, 4),
                  har_forecast=round(har, 4), ratio=c['iv_har_ratio'],
                  required=min_ih)
            if ratio >= min_ih:
                kept.append(c)
        out = kept

    out.sort(key=lambda c: c['credit_ratio'], reverse=True)
    return out


def drop_duplicate_kinds(candidates, state, rules=None):
    """
    Cap open structures per underlying (and never stack the same expiry twice).

    Default max_per_name=1 matches the old live/backtest audit B4 behaviour
    (one structure per name). Champion book6 uses max_per_name=3 so DTE 4–7
    can run overlapping expiries — same key as engine: (underlying, expiry).
    """
    max_per = 1
    if rules:
        max_per = max(1, int((rules.get('risk') or {}).get('max_per_name') or 1))
    open_structs = ST.open_structures(state)
    open_count = {}
    open_exp = set()
    for x in open_structs:
        u = x['underlying']
        open_count[u] = open_count.get(u, 0) + 1
        exp = str(x.get('expiry') or '')[:10]
        if exp:
            open_exp.add((u, exp))
    kept, dropped = [], []
    cycle_count = {}
    cycle_exp = set()
    for c in candidates:
        u = c['underlying']
        exp = str(c.get('expiry') or '')[:10]
        n = open_count.get(u, 0) + cycle_count.get(u, 0)
        if (u, exp) in open_exp or (u, exp) in cycle_exp:
            dropped.append({'candidate_id': c['id'],
                            'reason': f'already holding {u} {exp}'})
            continue
        if n >= max_per:
            dropped.append({'candidate_id': c['id'],
                            'reason': f'at max_per_name={max_per} for {u}'})
            continue
        cycle_count[u] = cycle_count.get(u, 0) + 1
        if exp:
            cycle_exp.add((u, exp))
        kept.append(c)
    return kept, dropped


def pick_condor(rows, s, obs, risk, underlying):
    """
    Build an iron condor: a put credit spread and a call credit spread, one expiry.

    Wings are selected WITHOUT the per-wing credit filter -- a condor's economics
    depend on the combined credit against one wing's width, and judging each wing
    independently rejects sound structures (it produced literally zero trades across
    2.5 years of backtest).
    """
    wing_cfg = dict(s)
    wing_cfg['min_credit_ratio'] = 0.0
    wing_cfg['max_credit_ratio'] = 1.0

    # Both wings MUST come from the same expiry. The backtest gets this for free --
    # the engine hands strategy.py one expiry's chain at a time -- but the live agent
    # pulls a whole DTE range, so without this the two wings can land on different
    # expiries and no condor is ever built. That divergence silently produced zero
    # SPY candidates until it was traced.
    expiries = sorted({r['expiry'] for r in rows})
    best = None
    for expiry in expiries:
        same_exp = [r for r in rows if r['expiry'] == expiry]
        put = pick_vertical(same_exp, 'P', wing_cfg, obs, risk, size=False)
        call = pick_vertical(same_exp, 'C', wing_cfg, obs, risk, size=False)
        if not put or not call:
            continue
        if put['short_strike'] >= call['short_strike']:
            continue   # wings must straddle the underlying
        ratio = (put['credit'] + call['credit']) / max(put['width'], call['width'])
        if best is None or ratio > best[0]:
            best = (ratio, put, call, S.atm_iv(same_exp))

    if best is None:
        return None
    _, put, call, iv_atm = best

    total_credit = put['credit'] + call['credit']
    risk_width = max(put['width'], call['width'])
    ratio = total_credit / risk_width
    if not (s['min_credit_ratio'] <= ratio <= s['max_credit_ratio']):
        return None

    # Only one wing can finish in the money, so risk is the wider wing less TOTAL credit.
    per_unit_risk = max(risk_width - total_credit, 0.01) * CONTRACT_MULTIPLIER
    qty = _qty_from_risk(per_unit_risk, obs, risk)
    if qty <= 0:
        return None

    return {
        'id': f"{underlying}-IC-{put['expiry']}-{put['short_strike']:.0f}/{call['short_strike']:.0f}",
        'underlying': underlying,
        'kind': 'iron_condor', 'structure': 'condor',
        'side': 'credit',
        'expiry': put['expiry'], 'dte': put['dte'],
        'put_short_occ': put['short_occ'], 'put_long_occ': put['long_occ'],
        'put_short_strike': put['short_strike'], 'put_long_strike': put['long_strike'],
        'call_short_occ': call['short_occ'], 'call_long_occ': call['long_occ'],
        'call_short_strike': call['short_strike'], 'call_long_strike': call['long_strike'],
        'put_short_delta': put['short_delta'], 'call_short_delta': call['short_delta'],
        'net_delta': round(put['short_delta'] + call['short_delta'], 4),
        'short_iv': put['short_iv'],
        'iv_atm': round(iv_atm, 4) if iv_atm else None,
        'width': risk_width, 'put_width': put['width'], 'call_width': call['width'],
        'credit': round(total_credit, 3), 'credit_ratio': round(ratio, 3),
        'qty': qty,
        'max_loss': round(per_unit_risk * qty, 2),
        'max_profit': round(total_credit * CONTRACT_MULTIPLIER * qty, 2),
        'worst_leg_spread_pct': max(put['worst_leg_spread_pct'], call['worst_leg_spread_pct']),
    }


def parse_occ(symbol, underlying):
    tail = symbol[len(underlying):]
    if len(tail) < 15:
        raise ValueError(symbol)
    expiry = dt.date(2000 + int(tail[0:2]), int(tail[2:4]), int(tail[4:6]))
    return underlying, expiry, tail[6], int(tail[7:15]) / 1000.0


def pick_vertical(rows, right, s, obs, risk, size=True):
    # Single-wing floor: when the live book is a vertical, use min_credit_ratio_vertical
    # (condor's combined 0.15 is the wrong bar on one wing). Condor path zeros this
    # itself before combining wings.
    s = dict(s)
    if (s.get('structure') or s.get('kind')) in ('vertical', 'put_credit', 'call_credit'):
        s['min_credit_ratio'] = s.get('min_credit_ratio_vertical',
                                      s.get('min_credit_ratio', 0.08))
    same = [r for r in rows if r['right'] == right]
    band = [r for r in same
            if abs(abs(r['delta']) - s['target_delta']) <= s['delta_tolerance']]
    if not band:
        return None
    short = min(band, key=lambda r: abs(abs(r['delta']) - s['target_delta']))

    target = short['strike'] - s['width'] if right == 'P' else short['strike'] + s['width']
    by_strike = {r['strike']: r for r in same if r['expiry'] == short['expiry']}
    long = by_strike.get(target)
    if long is None:
        further = [r for r in same if r['expiry'] == short['expiry'] and
                   (r['strike'] < short['strike'] if right == 'P' else r['strike'] > short['strike'])]
        if not further:
            return None
        long = min(further, key=lambda r: abs(abs(r['strike'] - short['strike']) - s['width']))

    width = abs(short['strike'] - long['strike'])
    credit = short['mid'] - long['mid']
    if width <= 0 or credit <= 0:
        return None
    ratio = credit / width
    if not (s['min_credit_ratio'] <= ratio <= s['max_credit_ratio']):
        return None

    per_spread_risk = max(width - credit, 0.01) * CONTRACT_MULTIPLIER
    qty = _qty_from_risk(per_spread_risk, obs, risk)
    if size and qty <= 0:
        return None
    if not size:
        qty = max(qty, 1)

    return {
        'id': f"{short['underlying']}-{right}-{short['expiry']}-{short['strike']:.0f}",
        'underlying': short['underlying'],
        'kind': 'put_credit' if right == 'P' else 'call_credit',
        'structure': 'vertical',
        'side': 'credit',
        'right': right, 'expiry': str(short['expiry']),
        'dte': (short['expiry'] - dt.date.today()).days,
        'short_occ': short['occ'], 'short_strike': short['strike'],
        'short_delta': round(short['delta'], 4), 'short_iv': short.get('iv'),
        'iv_atm': (lambda v: round(v, 4) if v else None)(
            S.atm_iv([r for r in same if r['expiry'] == short['expiry']])),
        'long_occ': long['occ'], 'long_strike': long['strike'],
        'width': width, 'credit': round(credit, 3), 'credit_ratio': round(ratio, 3),
        'qty': qty,
        'max_loss': round(per_spread_risk * qty, 2),
        'max_profit': round(credit * CONTRACT_MULTIPLIER * qty, 2),
        'worst_leg_spread_pct': round(max(short['spread_pct'], long['spread_pct']), 4),
        'short_occ': short['occ'], 'long_occ': long['occ'],
        'dte': (short['expiry'] - dt.date.today()).days,
    }


def pick_vertical_debit(rows, right, s, obs, risk, size=True):
    """Buy the target-delta option, sell further OTM. Max loss = debit paid."""
    same = [r for r in rows if r['right'] == right]
    band = [r for r in same
            if abs(abs(r['delta']) - s['target_delta']) <= s['delta_tolerance']]
    if not band:
        return None
    long = min(band, key=lambda r: abs(abs(r['delta']) - s['target_delta']))
    target = long['strike'] - s['width'] if right == 'P' else long['strike'] + s['width']
    by_strike = {r['strike']: r for r in same if r['expiry'] == long['expiry']}
    short = by_strike.get(target)
    if short is None:
        further = [r for r in same if r['expiry'] == long['expiry'] and
                   (r['strike'] < long['strike'] if right == 'P' else r['strike'] > long['strike'])]
        if not further:
            return None
        short = min(further, key=lambda r: abs(abs(r['strike'] - long['strike']) - s['width']))
    width = abs(long['strike'] - short['strike'])
    debit = long['mid'] - short['mid']
    if width <= 0 or debit <= 0:
        return None
    ratio = debit / width
    lo = s.get('min_credit_ratio_vertical', s.get('min_credit_ratio', 0.08))
    if not (lo <= ratio <= s['max_credit_ratio']):
        return None
    per_spread_risk = debit * CONTRACT_MULTIPLIER
    qty = _qty_from_risk(per_spread_risk, obs, risk)
    if size and qty <= 0:
        return None
    if not size:
        qty = max(qty, 1)
    return {
        'id': f"{long['underlying']}-{right}D-{long['expiry']}-{long['strike']:.0f}",
        'underlying': long['underlying'],
        'kind': 'put_debit' if right == 'P' else 'call_debit',
        'structure': 'vertical_debit',
        'side': 'debit',
        'right': right, 'expiry': str(long['expiry']),
        'dte': (long['expiry'] - dt.date.today()).days,
        'short_occ': short['occ'], 'short_strike': short['strike'],
        'short_delta': round(short['delta'], 4), 'short_iv': short.get('iv'),
        'iv_atm': (lambda v: round(v, 4) if v else None)(
            S.atm_iv([r for r in same if r['expiry'] == long['expiry']])),
        'long_occ': long['occ'], 'long_strike': long['strike'],
        'width': width, 'credit': 0.0, 'debit': round(debit, 3), 'credit_ratio': round(ratio, 3),
        'qty': qty,
        'max_loss': round(per_spread_risk * qty, 2),
        'max_profit': round((width - debit) * CONTRACT_MULTIPLIER * qty, 2),
        'worst_leg_spread_pct': round(max(short['spread_pct'], long['spread_pct']), 4),
    }


# ------------------------------------------------------------------ 3. decide

# The decision step, v2.3: the model CLASSIFIES the news context (agent/veto.py), code
# DECIDES. Output of this function keeps the historical shape {action, candidate_id,
# confidence, rationale} so validate(), the journal and the tests are unchanged, and adds
# 'veto' (the classification and the candidates it removed) for the counterfactual ledger.

_VETO_CACHE = {}   # context_hash -> classification, for the life of the process


def _anthropic_post(api_key, body):
    import requests
    try:
        resp = requests.post("https://api.anthropic.com/v1/messages",
                             headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                                      "content-type": "application/json"},
                             json=body, timeout=60)
    except Exception as e:  # transport failure -> classifier_failed -> PASS
        return False, f"transport: {e}"
    if not resp.ok:
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    return True, resp.json()


def decide(candidates, obs, news, rules, post_fn=None, now=None):
    """
    Returns (choice_dict, raw_text).

    Pipeline: filter headlines -> classify context (LLM, cached per headline set, skipped
    when the filtered tape is empty) -> deterministic per-candidate veto -> every
    surviving candidate (code still ranks by credit/width; the model does not pick).
    Any failure -- no key, transport, schema, refusal -- becomes an UNKNOWN/UNCLEAR
    context, which decide() turns into PASS.
    """
    import veto as V
    if not candidates:
        return {'action': 'pass', 'rationale': 'no candidates passed the gates'}, None

    llm = rules.get('llm', {})
    api_key = load_env_var('ANTHROPIC_API_KEY', required=False)

    # A news feed that returned nothing at all is not a quiet tape -- it is an unverifiable
    # one. Fail closed: an empty or missing payload from the feed resolves to PASS, while a
    # payload whose items are all irrelevant (filtered out) is genuinely quiet and tradable.
    if not news:
        return {'action': 'pass', 'candidate_id': None, 'candidate_ids': [], 'confidence': 0.0,
                'rationale': 'news feed returned no items -- tape unverifiable, PASS',
                'pass_reason': 'unclear_news',
                'veto': {'context': dict(V.FAILED, source='news_feed_empty'), 'headlines': [],
                         'vetoed': [c['id'] for c in candidates],
                         'counterfactual_candidate_id': candidates[0]['id']}}, None

    headlines = V.filter_headlines(news, tuple(watch_symbols(rules, candidates)), now=now)
    as_of = (now or dt.datetime.now()).strftime('%Y-%m-%dT%H:%M')

    if not api_key and not post_fn:
        # Fix D4: a missing decision step is a doubt, and doubt resolves to PASS --
        # unless the operator explicitly runs the agent as a pure rules engine.
        if llm.get('deterministic_fallback'):
            ctx = dict(V.QUIET, source='deterministic_fallback')
        else:
            return {'action': 'pass', 'candidate_id': None, 'candidate_ids': [], 'confidence': 0.0,
                    'rationale': 'no LLM key configured -> PASS (set llm.deterministic_fallback to override)',
                    'veto': {'context': None, 'headlines': headlines, 'vetoed': [c['id'] for c in candidates]}}, None
    else:
        post = post_fn or (lambda body: _anthropic_post(api_key, body))
        ctx = V.classify_context(headlines, candidates, as_of, rules, _VETO_CACHE, post)

    allowed, vetoed = V.apply_veto(candidates, ctx)
    veto_log = {'context': {k: ctx.get(k) for k in ('catalyst_scope', 'jump_severity', 'news_clarity',
                                                    'catalyst', 'confidence', 'source', 'error')},
                'headlines': headlines, 'vetoed': vetoed,
                'counterfactual_candidate_id': candidates[0]['id'] if vetoed and not allowed else None}
    raw = ctx.get('raw')
    if not allowed:
        reason = vetoed[0]['pass_reason'] if vetoed else 'no candidate'
        return {'action': 'pass', 'candidate_id': None, 'candidate_ids': [],
                'confidence': float(ctx.get('confidence') or 0.0),
                'rationale': f"veto ({reason}): {ctx.get('catalyst') or 'context ' + ctx.get('source', '?')}"[:240],
                'pass_reason': reason, 'veto': veto_log}, raw
    ids = [c['id'] for c in allowed]
    best = allowed[0]
    names = [c['underlying'] for c in allowed]
    return {'action': 'open', 'candidate_id': best['id'], 'candidate_ids': ids,
            'confidence': float(ctx.get('confidence') or 0.0),
            'rationale': (f"no jump catalyst ({ctx.get('source')}): {names}" if not vetoed else
                          f"{ctx.get('catalyst')}; vetoed {[v['underlying'] for v in vetoed]}, allowed {names}")[:240],
            'veto': veto_log}, raw


def extract_json(text):
    """Pull the first JSON object out of a model response. None if there isn't one."""
    if not text:
        return None
    start = text.find('{')
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find('{', start + 1)
    return None


# ---------------------------------------------------------------- 4. validate

def validate(choice, candidates, obs, rules, state=None):
    """
    Re-check the model's pick against every rule. Returns (candidate, errors).

    The model is not trusted, so nothing it said is taken on faith -- not the id, not
    the sizing, not that the trade was in the list at all.
    """
    errs = []
    if not isinstance(choice, dict):
        return None, ['decision was not a JSON object']
    if choice.get('action') != 'open':
        return None, []

    cid = choice.get('candidate_id')
    match = next((c for c in candidates if c['id'] == cid), None)
    if match is None:
        return None, [f"candidate_id {cid!r} was not in the offered list"]

    risk = rules['risk']
    scale = float(obs.get('risk_scale') or 1.0)
    eq_frac = float(obs.get('equity_fraction') or 1.0)
    budget = obs['equity'] * risk['max_risk_per_trade_pct'] * scale * eq_frac
    if match['max_loss'] > budget * 1.001:
        errs.append(f"max_loss ${match['max_loss']:,.2f} exceeds budget ${budget:,.2f}")
    if match['qty'] > risk['max_contracts_per_trade']:
        errs.append(f"qty {match['qty']} exceeds cap {risk['max_contracts_per_trade']}")
    if (match.get('side') or 'credit') == 'debit':
        if match.get('debit', 0) <= 0:
            errs.append("not a defined-risk debit structure")
        expected = match['debit'] * CONTRACT_MULTIPLIER * match['qty']
        if match.get('structure') not in ('calendar',) and abs(match['max_loss'] - expected) > 1.0:
            errs.append("debit max_loss does not match premium paid")
    elif match.get('structure') in ('short_strangle', 'short_straddle'):
        if match.get('credit', 0) <= 0:
            errs.append("naked short has no credit")
        if match['put_short_strike'] >= match['call_short_strike']:
            errs.append("strangle wings do not straddle")
    elif match.get('structure') == 'calendar':
        if match.get('credit', 0) <= 0:
            errs.append("calendar has no credit")
    elif match['width'] <= 0 or match['credit'] <= 0:
        errs.append("not a defined-risk credit structure")
    if match.get('structure') == 'condor':
        # A condor whose wings do not straddle is two overlapping bets, not a condor,
        # and its risk does not net the way max_loss assumes.
        if match['put_short_strike'] >= match['call_short_strike']:
            errs.append("condor wings do not straddle the underlying")
        # Explicit None check, NOT `or 1.0`: net_delta of exactly 0.0 is falsy in
        # Python, so `or` would substitute the sentinel and reject the *most*
        # delta-neutral condors -- precisely the ones we want most.
        net_delta = match.get('net_delta')
        if net_delta is None:
            errs.append("condor has no net_delta to verify")
        elif abs(net_delta) > 0.25:
            errs.append(f"condor is not delta-neutral (net delta {net_delta})")
        expected = max(0.0, match['width'] - match['credit']) * CONTRACT_MULTIPLIER * match['qty']
        if abs(match['max_loss'] - expected) > 1.0:
            errs.append("condor max_loss does not match one-wing arithmetic")
    if match['max_loss'] > obs['options_buying_power']:
        errs.append("insufficient options buying power")
    if match['worst_leg_spread_pct'] > rules['execution']['max_spread_pct_of_mid']:
        errs.append("leg spread too wide")
    # The cap counts STRUCTURES, not legs (audit B3): four legs of one condor are one
    # position. Counting legs here made a single open condor reject every later candidate
    # with "position cap reached", while risk_gate -- which counts structures -- had
    # already allowed the cycle to proceed. The two gates must agree.
    open_now = (len([x for x in ST.open_structures(state)]) if state is not None
                else len(option_positions(obs)))
    if open_now >= risk['max_concurrent_positions']:
        errs.append("position cap reached between generation and execution")
    if state is not None:
        max_per = max(1, int(risk.get('max_per_name') or 1))
        same_name = [x for x in ST.open_structures(state)
                     if x['underlying'] == match['underlying']]
        exp = str(match.get('expiry') or '')[:10]
        if any(str(x.get('expiry') or '')[:10] == exp for x in same_name):
            errs.append(f"already holding {match['underlying']} {exp}")
        elif len(same_name) >= max_per:
            errs.append(f"at max_per_name={max_per} for {match['underlying']}")

    return (match if not errs else None), errs


# ----------------------------------------------------------------- 5. execute

def submit_spread(cand, rules, shadow):
    """Submit the multi-leg spread as a marketable limit order."""
    ex = rules['execution']
    # Credit: Alpaca wants a NEGATIVE limit. Debit: a POSITIVE limit.
    debit_side = (cand.get('side') or 'credit') == 'debit'
    if debit_side:
        start = cand.get('limit_debit') or cand['debit'] * (1 + ex['limit_slippage_pct'])
        limit = abs(round(start, 2))
    else:
        start = cand.get('limit_credit') or cand['credit'] * (1 - ex['limit_slippage_pct'])
        limit = -abs(round(start, 2))
    # Deterministic idempotency key: a retry of this same intent inside the same cadence
    # slot reuses it and the broker refuses the duplicate. A uuid here (the previous
    # behaviour) turned a lost response into a second identical condor.
    client_id = EFF.open_key(cand, cadence_minutes=rules.get('schedule', {}).get('cadence_minutes', 15),
                             strategy_tag=(rules.get('deploy') or {}).get('order_tag'))

    # The CLI takes --legs as a single JSON ARRAY string, not repeated --leg flags.
    # Getting this wrong fails silently if the call is allowed to fail, so it isn't.
    if cand.get('structure') == 'condor':
        # Four legs: short both wings, long both protective strikes. Alpaca's mleg
        # order class caps at 4 legs, which an iron condor exactly fills.
        leg_specs = [
            (cand['put_short_occ'], 'sell', 'sell_to_open'),
            (cand['put_long_occ'], 'buy', 'buy_to_open'),
            (cand['call_short_occ'], 'sell', 'sell_to_open'),
            (cand['call_long_occ'], 'buy', 'buy_to_open'),
        ]
    elif cand.get('structure') in ('short_strangle', 'short_straddle'):
        leg_specs = [
            (cand['put_short_occ'], 'sell', 'sell_to_open'),
            (cand['call_short_occ'], 'sell', 'sell_to_open'),
        ]
    else:
        leg_specs = [
            (cand['short_occ'], 'sell', 'sell_to_open'),
            (cand['long_occ'], 'buy', 'buy_to_open'),
        ]
    legs = json.dumps([
        {'symbol': occ, 'side': side, 'ratio_qty': '1', 'position_intent': intent}
        for occ, side, intent in leg_specs
    ])

    args = ['order', 'submit', '--order-class', 'mleg',
            '--qty', str(cand['qty']), '--type', ex['order_type'],
            '--limit-price', str(limit), '--time-in-force', ex['time_in_force'],
            '--client-order-id', client_id, '--legs', legs]
    if shadow:
        args.append('--dry-run')

    # Deliberately NOT allow_fail: a rejected or malformed order must be loud. Silently
    # swallowing it would let the agent believe it holds a position it never opened.
    result = cli(args)

    submitted = {'client_order_id': client_id, 'limit_price': limit,
                 'shadow': shadow, 'result': result}
    if not shadow and isinstance(result, dict):
        submitted['order_id'] = result.get('id')
        submitted['status'] = result.get('status')
        if result.get('status') == 'rejected':
            # A rejection arrives as HTTP 201 with a reason, not as an error code.
            submitted['rejected_reason'] = result.get('reason')
        # Declared effect vs observed effect. We may only register a structure the broker
        # confirms is the one we asked for; anything else is refused here rather than
        # discovered later by the reconciler.
        mismatches = EFF.verify_submission(cand, limit, result)
        if mismatches:
            submitted['effect_mismatch'] = mismatches
            submitted.setdefault('rejected_reason', 'effect_mismatch: ' + '; '.join(mismatches))
    return submitted


def close_structure(struct, rules, shadow, urgent=False, attempts=2):
    """
    Close a registered structure with ONE mleg order (all legs, *_to_close intents).

    Leg-by-leg closing partially failed live on 2026-08-27 (long legs held as
    collateral were refused while the short closes were pending). An atomic mleg
    close cannot leave a naked short behind. If the mleg path itself fails twice, fall
    back to leg-by-leg with shorts first and report FAILED so the cycle halts.
    """
    quotes = quotes_for_structures([struct], rules)
    cost = ST.close_cost(struct, quotes)
    if cost is None:
        return {'client_order_id': struct['client_order_id'], 'shadow': shadow,
                'FAILED': True, 'note': 'STALE_MARK: cannot price the close -- not submitted'}
    last = None
    for attempt in range(attempts):
        args, limit = ST.close_order_args(struct, cost, rules, shadow, urgent=urgent or attempt > 0)
        result = cli(args, allow_fail=True)
        ok = result and not (isinstance(result, dict) and (result.get('error') or result.get('status') == 'rejected'))
        if ok or shadow:
            if not shadow:
                struct['status'] = 'closing'
                struct['close_order_id'] = (result or {}).get('id') if isinstance(result, dict) else None
            return {'client_order_id': struct['client_order_id'], 'shadow': shadow,
                    'limit_debit': limit, 'attempt': attempt + 1, 'result': result}
        last = result
        time.sleep(2)
    # Fallback: legs one by one, shorts first, so an interruption never leaves a naked short.
    legs = sorted(struct['legs'], key=lambda l: l['side'] != 'sell')
    per_leg = [close_position(l['occ'], shadow) for l in legs]
    failed = any(r.get('FAILED') for r in per_leg)
    struct['status'] = 'broken' if failed else 'closing'
    return {'client_order_id': struct['client_order_id'], 'shadow': shadow,
            'mleg_result': last, 'leg_results': per_leg, 'FAILED': failed,
            'note': 'mleg close rejected twice; fell back to leg-by-leg (shorts first)'}


def flatten_stock(symbol, qty, shadow):
    """Assignment left us with stock: flatten it. The only place a market order is allowed."""
    if shadow:
        return {'symbol': symbol, 'qty': qty, 'shadow': True, 'result': 'would flatten'}
    result = cli(['position', 'close', '--symbol-or-asset-id', symbol], allow_fail=True)
    return {'symbol': symbol, 'qty': qty, 'shadow': False, 'result': result,
            'FAILED': not result or (isinstance(result, dict) and bool(result.get('error')))}


def close_position(symbol, shadow, attempts=3):
    """
    Close one option leg, retrying on a transient rejection.

    Closing a multi-leg position leg-by-leg partially fails in practice: while the
    short legs' closing orders are still pending, the long legs are held as collateral
    and their close is rejected 403. Observed live on 2026-08-27 -- two legs closed,
    two were refused, leaving an unbalanced position. A retry a moment later succeeded.

    An unbalanced condor is strictly worse than either holding it or closing it: the
    protective long can be gone while the naked short remains. So this retries rather
    than reporting success on a partial close.
    """
    if shadow:
        return {'symbol': symbol, 'shadow': True, 'result': 'would close'}

    last = None
    for attempt in range(attempts):
        result = cli(['position', 'close', '--symbol-or-asset-id', symbol], allow_fail=True)
        if result and not (isinstance(result, dict) and result.get('error')):
            return {'symbol': symbol, 'shadow': False, 'attempt': attempt + 1,
                    'result': result}
        last = result
        time.sleep(2)

    return {'symbol': symbol, 'shadow': False, 'attempt': attempts,
            'result': last, 'FAILED': True,
            'note': 'position may be unbalanced -- a protective leg could be missing'}


# ------------------------------------------------------------------ 6. journal

def journal(entry):
    os.makedirs(JOURNAL_DIR, exist_ok=True)
    path = os.path.join(JOURNAL_DIR, f"{dt.date.today():%Y-%m-%d}.jsonl")
    if TRACE:
        entry['analysis'] = list(TRACE)
    with open(path, 'a') as f:
        f.write(json.dumps(entry, default=str) + "\n")
    write_analysis_log(entry)
    return path


def write_analysis_log(entry):
    """
    The same cycle as `<date>.jsonl`, written for a person instead of a parser.

    The JSONL is the record of what happened; this is the record of how it was
    reached -- every name the agent looked at, what it saw, and which gate ended
    that name's candidacy. Appended, never rewritten, so a day reads as a sequence
    of cycles.
    """
    if not TRACE and entry.get('stage') in ('halted', 'skill'):
        return
    os.makedirs(JOURNAL_DIR, exist_ok=True)
    path = os.path.join(JOURNAL_DIR, f"{dt.date.today():%Y-%m-%d}-analysis.log")
    obs = entry.get('observation') or {}
    L = []
    W = 78
    L.append("=" * W)
    L.append(f"CYCLE {entry.get('ts')}   {'SHADOW (nothing traded)' if entry.get('shadow') else 'LIVE (paper)'}")
    L.append("=" * W)
    if entry.get('account') or entry.get('book'):
        L.append(f"  book      {entry.get('book') or '(default)'}   "
                 f"account {entry.get('account') or '(unbound)'}")
    if obs:
        L.append(f"  account   equity ${obs.get('equity', 0):,.2f}   "
                 f"day P&L ${obs.get('day_pnl', 0):+,.2f} ({obs.get('day_pnl_pct', 0):+.2%})")
        L.append(f"            options BP ${obs.get('options_buying_power', 0):,.2f}   "
                 f"open positions {entry.get('position_count', 0)}   "
                 f"structures {entry.get('structure_count', 0)}")
    rg = entry.get('risk_gate') or {}
    L.append(f"  risk gate may_open={rg.get('may_open')}")
    for r in (rg.get('reasons') or []):
        L.append(f"            - {r}")

    by_name = {}
    for t in TRACE:
        by_name.setdefault(t['underlying'], []).append(t)
    if by_name:
        L.append("")
        L.append("  MARKET ANALYSIS -- every name the agent looked at")
        L.append("  " + "-" * (W - 2))
    for name, rows in by_name.items():
        chain = next((r for r in rows if r['stage'] == 'chain'), None)
        if chain:
            d = chain.get('dropped') or {}
            drops = ", ".join(f"{k} {v}" for k, v in d.items() if v) or "none"
            L.append(f"  {name:<6} spot {chain.get('spot')}   "
                     f"chain {chain.get('contracts')} contracts -> {chain.get('priced')} priced"
                     f"   (dropped: {drops})")
        for r in rows:
            st = r['stage']
            if st == 'chain':
                continue
            if st == 'no_chain':
                L.append(f"  {name:<6} NO CHAIN for {r.get('dte_window')}")
            elif st == 'no_expiry_before_flat_by':
                L.append(f"  {name:<6} no expiry on or before flat-by {r.get('flat_by')}")
            elif st == 'router':
                L.append(f"  {name:<6}    router     {r.get('family') or 'NO_TRADE'}"
                         f"  ({r.get('reason')})  trend={r.get('trend')}")
                L.append(f"  {name:<6}      put  edge {r.get('put_edge')} "
                         f"({r.get('put_reason')})   call edge {r.get('call_edge')} "
                         f"({r.get('call_reason')})   need >= {r.get('required')}")
            elif st == 'no_structure':
                L.append(f"  {name:<6} -> no structure built from {r.get('priced')} priced "
                         f"contracts ({r.get('kind')} builder declined)")
            elif st == 'structure_built':
                L.append(f"  {name:<6} -> BUILT {r.get('kind')} exp {r.get('expiry')} "
                         f"short {r.get('short_strike')} "
                         f"{r.get('delta_label', 'delta')} {r.get('short_delta')} "
                         f"credit {r.get('credit')} ratio {r.get('credit_ratio')}")
            elif st == 'gate_vol':
                if r.get('ratio') is not None:
                    L.append(f"  {name:<6}    vol gate   {'PASS' if r['passed'] else 'FAIL'}  "
                             f"IV {r.get('iv')} / RV {r.get('rv')} = {r.get('ratio')}x  "
                             f"(need >= {r.get('required')}x, using {r.get('which_iv')})")
                else:
                    L.append(f"  {name:<6}    vol gate   FAIL  {r.get('reason')}")
            elif st == 'gate_overnight':
                L.append(f"  {name:<6}    overnight  {'PASS' if r['passed'] else 'FAIL'}  "
                         f"share {r.get('share')} (max {r.get('max_allowed')})"
                         if r.get('share') is not None else
                         f"  {name:<6}    overnight  FAIL  {r.get('reason')}")
            elif st == 'gate_iv_har':
                if r.get('ratio') is not None:
                    L.append(f"  {name:<6}    IV/HAR     {'PASS' if r['passed'] else 'FAIL'}  "
                             f"IV {r.get('iv')} / HAR {r.get('har_forecast')} = {r.get('ratio')}x "
                             f"(need >= {r.get('required')}x)")
                else:
                    L.append(f"  {name:<6}    IV/HAR     FAIL  {r.get('reason')}")

    cands = entry.get('candidates') or []
    L.append("")
    L.append(f"  SURVIVING CANDIDATES: {len(cands)}")
    for c in cands:
        L.append(f"    {c.get('id')}  credit {c.get('credit')} ratio {c.get('credit_ratio')} "
                 f"qty {c.get('qty')} max_loss {c.get('max_loss')}")
    for d in (entry.get('dropped') or []):
        L.append(f"    dropped {d.get('candidate_id')}: {d.get('reason')}")

    dec = entry.get('decision') or {}
    L.append("")
    L.append(f"  DECISION: {str(dec.get('action', entry.get('action', 'none'))).upper()}"
             + (f" -> {dec.get('candidate_id')}" if dec.get('candidate_id') else ""))
    if dec.get('rationale'):
        L.append(f"    rationale: {dec['rationale']}")
    v = entry.get('validation')
    if v:
        L.append(f"    validation: {json.dumps(v, default=str)[:300]}")
    for f in (entry.get('forced_closes') or []):
        L.append(f"    forced close {f.get('symbol')}: {f.get('reason')} -> {f.get('result')}")
    if entry.get('order'):
        L.append(f"    order: {json.dumps(entry['order'], default=str)[:400]}")
    L.append("")

    with open(path, 'a') as f:
        f.write("\n".join(L) + "\n")
    return path


def load_state():
    path = os.path.join(JOURNAL_DIR, 'state.json')
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def save_state(state):
    os.makedirs(JOURNAL_DIR, exist_ok=True)
    with open(os.path.join(JOURNAL_DIR, 'state.json'), 'w') as f:
        json.dump(state, f, indent=2, default=str)


# ---------------------------------------------------------------------- cycle

def cycle(rules, shadow, verbose, strategy_id=None):
    TRACE.clear()
    # Stamp the account on every entry. Without it two books' journals are
    # indistinguishable once their files sit in one directory -- which is exactly what
    # happened when the condor profile was rebound from account A to account C and
    # 83 account-A entries stayed behind in what became account C's journal.
    _acct = rules.get('account') or {}
    deploy = rules.get('deploy') or {}
    sid = strategy_id or deploy.get('strategy_id')
    entry = {'ts': dt.datetime.now().astimezone().isoformat(timespec='seconds'),
             'shadow': shadow,
             'account': _acct.get('account_id'), 'book': _acct.get('label')}
    if sid:
        entry['strategy_id'] = sid
        entry['deploy'] = {k: deploy.get(k) for k in ('title', 'backtest_run', 'order_tag', 'equity_fraction')
                           if deploy.get(k) is not None}
    state = load_state()

    if state.get('halted'):
        log("HALTED by a previous drawdown breach -- restart is a human decision.")
        log("Clear agent/decisions/state.json to resume.")
        entry.update({'stage': 'halted', 'action': 'none'})
        journal(entry)
        return

    missing = SK.missing_required(rules)
    refs = SK.missing_refs()
    if missing or refs:
        log(f"required skill missing: {missing or refs} -- fail closed")
        entry.update({'stage': 'skill', 'action': 'none',
                      'reason': f'missing_skill:{missing or refs}'})
        journal(entry)
        return

    open_ok, gate_reason = market_gate(rules)
    log(f"market gate: {gate_reason}")
    if not open_ok:
        entry.update({'stage': 'market_gate', 'action': 'none', 'reason': gate_reason})
        journal(entry)
        return
    exits_only = 'exits only' in gate_reason

    obs = observe(rules, verbose)
    obs['equity_fraction'] = DEPLOY.equity_scale(rules)
    obs['risk_scale'] = risk_scale(state, rules, obs['equity'])
    log(f"equity ${obs['equity']:,.2f}  day P&L ${obs['day_pnl']:+,.2f} "
        f"({obs['day_pnl_pct']:+.2%})  positions {len(option_positions(obs))}"
        + (f"  risk_scale {obs['risk_scale']:.2f}" if obs['risk_scale'] != 1.0 else ""))
    entry['observation'] = {k: v for k, v in obs.items() if k != 'positions'}
    entry['position_count'] = len(option_positions(obs))          # legs at the broker
    entry['structure_count'] = len(ST.open_structures(state))      # what the cap counts

    # Sync fills of structures submitted on earlier cycles before judging anything.
    entry['fill_sync'] = ST.sync_fills(state, cli)
    entry['structures_open'] = [st['client_order_id'] for st in ST.open_structures(state)]

    foreign = DEPLOY.foreign_occs(sid) if sid else None
    may_open, forced, reasons = risk_gate(obs, rules, state, foreign_occs=foreign)
    for r in reasons:
        log(f"risk gate: {r}")
    entry['risk_gate'] = {'may_open': may_open, 'reasons': reasons,
                          'forced_actions': forced}

    # Close SHORT legs first. If the sequence is interrupted, being long a leftover
    # protective option is a bounded, tiny loss; being short a naked one is not.
    def is_short(sym):
        for p in obs['positions']:
            if p.get('symbol') == sym:
                try:
                    return float(p.get('qty', 0)) < 0
                except (TypeError, ValueError):
                    return False
        return False

    executed = []
    structs = state.get('structures', {})
    # Structures first (atomic mleg closes), then stock, then orphan legs (shorts first).
    order = {'close_structure': 0, 'flatten_stock': 1, 'close_leg': 2, 'close': 2}
    for action in sorted(forced, key=lambda a: (order.get(a['action'], 3),
                                                not is_short(a.get('symbol', '')))):
        kind = action['action']
        if kind == 'close_structure':
            st = structs.get(action['client_order_id'])
            log(f"forced close {action['client_order_id']}: {action['reason']}")
            result = close_structure(st, rules, shadow, urgent=action.get('urgent', False)) if st else \
                {'FAILED': True, 'note': 'structure not in registry'}
        elif kind == 'flatten_stock':
            log(f"FLATTEN STOCK {action['symbol']} qty {action['qty']}: {action['reason']}")
            result = flatten_stock(action['symbol'], action['qty'], shadow)
        else:
            log(f"forced close {action['symbol']}: {action['reason']}")
            result = close_position(action['symbol'], shadow)
        if result.get('FAILED'):
            log(f"  !! CLOSE FAILED ({kind}) -- position may be unbalanced; agent halts")
            state['halted'] = True
            state['halt_reason'] = f"CLOSE_FAILED:{kind}"
        executed.append({'action': kind, **result})
    entry['forced_closes'] = executed

    if not may_open or exits_only:
        entry.update({'stage': 'no_open', 'action': 'none'})
        journal(entry)
        if sid:
            DEPLOY.sync_leg_registry(sid, state)
        save_state(state)
        return

    names, branches, uni_info = resolve_universe(rules)
    entry['universe'] = {'names': names, 'branches': branches, **uni_info}
    log(f"universe {uni_info.get('mode')}: {names}")

    if rules['universe'].get('mode') == 'funnel':
        # Horizon B: three-stage US-ADAPT pipeline (assets -> dynamics -> structure).
        from usadapt_bridge import build_candidates_funnel
        candidates, funnel_info = build_candidates_funnel(rules, obs, mode='PAPER' if not shadow else 'SHADOW')
        entry['funnel'] = funnel_info
    else:
        candidates = build_candidates(rules, obs, names=names, branches=branches)
    candidates, skill_dropped, skill_info = SK.annotate_candidates(candidates, rules)
    entry['skills'] = {'dropped': skill_dropped, **skill_info}
    if skill_dropped:
        log(f"{len(skill_dropped)} candidate(s) dropped by skill gates")
    candidates, duplicates = drop_duplicate_kinds(candidates, state, rules)
    if duplicates:
        entry['duplicates_dropped'] = duplicates
        log(f"{len(duplicates)} candidate(s) dropped by per-name/expiry cap")
    log(f"{len(candidates)} candidate(s) passed the gates")
    entry['candidates'] = candidates

    news = cli(['data', 'news', '--symbols', ','.join(names) or 'SPY',
                '--limit', '12'], allow_fail=True)
    news_items = (news or {}).get('news', [])

    choice, raw = decide(candidates, obs, news_items, rules)
    log(f"decision: {choice.get('action')} -- {choice.get('rationale', '')[:120]}")
    entry['decision'] = choice
    entry['llm_raw'] = (raw or '')[:2000]

    ids = []
    if choice.get('action') == 'open':
        ids = list(choice.get('candidate_ids') or [])
        cid0 = choice.get('candidate_id')
        if cid0 and cid0 not in ids:
            ids.insert(0, cid0)

    cap = rules['risk']['max_concurrent_positions']
    orders, opened, all_errs = [], [], []
    for cid in ids:
        if len(ST.open_structures(state)) >= cap:
            break
        one = dict(choice)
        one['action'] = 'open'
        one['candidate_id'] = cid
        cand, errs = validate(one, candidates, obs, rules, state)
        if errs:
            all_errs.extend(errs)
            for e in errs:
                log(f"VALIDATION REJECTED {cid}: {e}")
            continue
        if not cand:
            continue
        if cand.get('structure') == 'condor':
            desc = (f"iron_condor {cand['underlying']} "
                    f"P{cand['put_short_strike']:.0f}/{cand['put_long_strike']:.0f} "
                    f"C{cand['call_short_strike']:.0f}/{cand['call_long_strike']:.0f} "
                    f"net delta {cand['net_delta']:+.3f} "
                    f"IV/RV {cand.get('iv_rv_ratio')}")
        elif cand.get('structure') in ('short_strangle', 'short_straddle'):
            desc = (f"{cand['kind']} {cand['underlying']} "
                    f"P{cand['put_short_strike']:.0f}/C{cand['call_short_strike']:.0f}")
        else:
            desc = (f"{cand['kind']} {cand['underlying']} "
                    f"{cand['short_strike']:.0f}/{cand['long_strike']:.0f}")
        prem = (f"debit {cand['debit']:.2f}" if (cand.get('side') or 'credit') == 'debit'
                else f"credit {cand['credit']:.2f}")
        log(f"submitting {desc} exp {cand['expiry']} x{cand['qty']}  "
            f"{prem}  max loss ${cand['max_loss']:,.0f}"
            + ("  [SHADOW]" if shadow else ""))
        submitted = submit_spread(cand, rules, shadow)
        orders.append(submitted)
        if not submitted.get('rejected_reason'):
            ST.register_open(state, cand, submitted)
            opened.append(cand['id'])

    entry['orders'] = orders
    entry['opened'] = opened
    entry['order'] = orders[0] if len(orders) == 1 else (orders or None)
    entry['validation'] = {'passed': bool(opened), 'errors': all_errs}
    entry['action'] = 'open' if opened else 'pass'

    path = journal(entry)
    if sid:
        DEPLOY.sync_leg_registry(sid, state)
    save_state(state)
    log(f"journalled -> {os.path.relpath(path, REPO_ROOT)}")


def apply_flat_by_override(args, rules, shadow):
    """
    `--flat-by`: a SHADOW-ONLY override of schedule.flat_by_date. Returns the value applied,
    or None.

    It exists for exactly one job. With DTE 4-7 and a flat-by of 2026-09-03 no expiry in
    the window survives the clamp, so `decide()` returns on `if not candidates` BEFORE the
    model is ever called -- and candidate generation, the news filter, the classifier, the
    veto, validate and order construction all go untested. Those are precisely the parts
    that have never once run in production.

    Refusing to work without --shadow is the point. Editing flat_by_date in
    agent_rules.json to get a wiring test is how a book ships with the wrong flat-by: the
    edit has to be remembered and reverted before anything touches the broker. An override
    that cannot run live cannot be forgotten.
    """
    if '--flat-by' not in args:
        return None
    if not shadow:
        die("--flat-by is a shadow-only override; it must not change what the live book is "
            "allowed to hold. Edit agent_rules.json if you mean it for real.")
    i = args.index('--flat-by')
    if i + 1 >= len(args):
        die("--flat-by requires a date (YYYY-MM-DD)")
    override = args[i + 1]
    try:
        dt.date.fromisoformat(override)
    except ValueError:
        die(f"--flat-by: not a date: {override!r}")
    rules['schedule']['flat_by_date'] = override
    log(f"SHADOW OVERRIDE -- flat_by_date := {override} (rules file untouched)")
    return override


def main():
    global JOURNAL_DIR
    sys.stdout.reconfigure(line_buffering=True)
    args = sys.argv[1:]
    shadow = '--shadow' in args or env_flag('SHADOW')
    verbose = '--verbose' in args
    rules_path = RULES_PATH
    strategy_id = None
    if '--rules' in args:
        rules_path = args[args.index('--rules') + 1]
    if '--strategy' in args:
        DEPLOY.require_allowed()
        strategy_id = args[args.index('--strategy') + 1]
        rules_path, JOURNAL_DIR = DEPLOY.resolve(strategy_id)
    elif '--profile' in args and '--rules' not in args:
        rules_path, JOURNAL_DIR = resolve_profile(args)

    record_to = args[args.index('--record') + 1] if '--record' in args else None

    for a in args:
        if a.startswith('--') and a not in ('--shadow', '--once', '--verbose', '--rules',
                                            '--record', '--strategy', '--profile',
                                            '--flat-by'):
            die(f"unrecognized argument: {a}")

    if not os.path.exists(rules_path):
        die(f"rules file not found: {rules_path}")
    with open(rules_path) as f:
        rules = json.load(f)

    apply_flat_by_override(args, rules, shadow)

    global ALPACA_BIN, KEY_ENV, SECRET_ENV
    # A retired book must not reach a live account. Its account_id is null, so
    # assert_account() SKIPS rather than refuses, and the loop would fall back to the
    # default credentials -- which another book now owns. Refusing here is what
    # actually prevents that; the null account_id alone does not.
    if (rules.get('_status') or {}).get('retired') and '--shadow' not in args:
        die(f"{os.path.relpath(rules_path, REPO_ROOT)} is RETIRED and unbound from any "
            f"live account. Run it with --shadow, or use one of the live profiles.")

    account = rules.get('account') or {}
    KEY_ENV = account.get('key_env', KEY_ENV)
    SECRET_ENV = account.get('secret_env', SECRET_ENV)
    ALPACA_BIN = _resolve_cli()
    tag = DEPLOY.order_tag(rules)
    log(f"alpaca CLI: {ALPACA_BIN}")
    if '--profile' in args:
        log(f"profile: {args[args.index('--profile') + 1]} -> "
            f"{os.path.relpath(rules_path, REPO_ROOT)}  "
            f"journal={os.path.relpath(JOURNAL_DIR, REPO_ROOT)}")
    if account:
        log(f"book: {account.get('label') or '(unnamed)'} -> account {account.get('account_id')} "
            f"via {KEY_ENV}")
        on = assert_account(rules)
        if on:
            log(f"account verified: {on}")
    if strategy_id or rules.get('deploy'):
        log(f"strategy: {strategy_id or rules['deploy'].get('strategy_id')}  "
            f"tag={tag}  journal={os.path.relpath(JOURNAL_DIR, REPO_ROOT)}")

    if shadow:
        log("SHADOW MODE -- every order carries --dry-run, no positions will be taken")

    if not record_to:
        cycle(rules, shadow, verbose, strategy_id=strategy_id)
        return

    # Record every crossing of the broker/model seams so this cycle can be replayed later
    # with no account and no API key (agent/tape.py, `make replay`).
    import tape as TAPE
    rec = TAPE.Recorder(record_to)
    restore = TAPE.install(sys.modules[__name__], rec)
    try:
        cycle(rules, shadow, verbose, strategy_id=strategy_id)
    finally:
        restore()
        rec.close()
        log(f"tape written -> {record_to} ({rec.seq} interactions)")


if __name__ == "__main__":
    main()
