"""
Pre-competition readiness check.

One command that verifies every dependency the agent needs, so a missing piece is
found on a quiet morning rather than at 09:30 on the first trading day. Each check
is independent and reports pass / warn / fail; the exit code is non-zero if anything
FAILED, so it can gate a launchd run or a CI step.

Usage:
    python3 agent/preflight.py [--verbose]

Checks, roughly in dependency order: credentials -> CLI -> broker -> account shape ->
market data -> LLM -> local artifacts -> scheduling -> safety invariants.
"""
import datetime as dt
import json
import os
import subprocess
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, 'agent'))

import agent_loop as A
from alpaca.env import load_env_var, env_flag

PASS, WARN, FAIL = 'PASS', 'WARN', 'FAIL'
results = []


def check(name, fn):
    try:
        status, detail = fn()
    except SystemExit:
        status, detail = FAIL, "check exited (a dependency called die())"
    except Exception as e:
        status, detail = FAIL, f"{type(e).__name__}: {e}"
    results.append((status, name, detail))
    icon = {PASS: 'ok  ', WARN: 'warn', FAIL: 'FAIL'}[status]
    print(f"  [{icon}] {name:<34} {detail}")
    return status


# ------------------------------------------------------------------- checks

def c_credentials():
    # Read the slot this BOOK uses, not the default pair -- main() has already pointed
    # A.KEY_ENV at whatever the rulebook names.
    key = load_env_var(A.KEY_ENV, required=False)
    sec = load_env_var(A.SECRET_ENV, required=False)
    if not key or not sec:
        return FAIL, f"{A.KEY_ENV} / {A.SECRET_ENV} not set"
    if not key.startswith('PK'):
        return FAIL, f"key does not look like a PAPER key (starts {key[:2]}, expected PK)"
    return PASS, f"paper key {key[:6]}...{key[-4:]} from {A.KEY_ENV}"


def c_cli():
    path = A._resolve_cli()
    out = subprocess.run([path, 'version'], capture_output=True, text=True, timeout=20)
    ver = out.stdout.strip().splitlines()[0] if out.stdout else '?'
    return PASS, f"{path} (v{ver})"


def c_cli_from_minimal_path():
    """
    The failure that actually bit: launchd runs with a minimal PATH, so a bare
    `alpaca` is unresolvable even though it works in a shell.
    """
    env = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': os.path.expanduser('~')}
    found = subprocess.run(['which', 'alpaca'], capture_output=True, text=True, env=env)
    if found.returncode == 0:
        return PASS, "resolvable even on a minimal PATH"
    if os.path.isfile(A._resolve_cli()):
        return PASS, "not on minimal PATH, but resolved absolutely by agent_loop"
    return FAIL, "unresolvable from a scheduled run"


def c_broker():
    acct = A.cli(['account', 'get'])
    if acct.get('status') != 'ACTIVE':
        return FAIL, f"account status is {acct.get('status')}"
    return PASS, f"{acct.get('account_number')} ACTIVE"


def c_paper_host():
    """The CLI must be pointed at paper, not live."""
    doc = subprocess.run([A._resolve_cli(), 'doctor'], capture_output=True, text=True,
                         env={**os.environ,
                              'ALPACA_API_KEY': load_env_var('ALPACA_API_KEY'),
                              'ALPACA_SECRET_KEY': load_env_var('ALPACA_SECRET_KEY')},
                         timeout=30).stdout
    if 'paper-api.alpaca.markets' in doc:
        return PASS, "paper-api.alpaca.markets"
    if 'api.alpaca.markets' in doc:
        return FAIL, "CLI is pointed at the LIVE host"
    return WARN, "could not determine host from `alpaca doctor`"


def c_options_level():
    acct = A.cli(['account', 'get'])
    lvl = acct.get('options_trading_level')
    if lvl is None:
        return WARN, "options level not reported"
    if lvl < 3:
        return FAIL, f"level {lvl} -- iron condors need level 3 (multi-leg)"
    return PASS, f"level {lvl} (multi-leg enabled)"


def c_equity():
    acct = A.cli(['account', 'get'])
    eq = float(acct.get('equity', 0))
    if abs(eq - 100_000) < 1:
        return PASS, f"${eq:,.2f} (matches the required starting balance)"
    return WARN, f"${eq:,.2f} -- the rules require a $100,000 start"


def c_flat_and_clean():
    pos = A.cli(['position', 'list'], allow_fail=True) or []
    orders = A.cli(['order', 'list', '--status', 'open'], allow_fail=True) or []
    if pos or orders:
        return WARN, f"{len(pos)} position(s), {len(orders)} open order(s)"
    return PASS, "no positions, no open orders"


def c_market_data():
    body = A.cli(['data', 'option', 'chain', '--underlying-symbol', 'SPY',
                  '--feed', 'indicative', '--limit', '5'], allow_fail=True)
    n = len((body or {}).get('snapshots') or {})
    if not n:
        return FAIL, "no option chain returned"
    return PASS, f"SPY chain reachable ({n} snapshots sampled)"


def c_realized_vol():
    rv = A.realized_vol('SPY')
    if rv is None:
        return FAIL, "cannot compute trailing realized vol -- the vol gate would block all trades"
    return PASS, f"SPY 21-session realized vol {rv:.1%}"


def c_llm():
    """Live check of the news-veto classifier: one real call on a benign synthetic tape."""
    rules = _rules()
    if not load_env_var('ANTHROPIC_API_KEY', required=False):
        return WARN, ("no ANTHROPIC_API_KEY -- every cycle with headlines resolves to PASS "
                      "(set llm.deterministic_fallback to trade rules-only)")
    obs = {'equity': 100_000.0, 'day_pnl': 0.0, 'day_pnl_pct': 0.0, 'positions': []}
    cand = [{'id': 'PREFLIGHT-1', 'underlying': 'SPY', 'kind': 'iron_condor', 'structure': 'condor',
             'dte': 1, 'put_short_strike': 750.0, 'call_short_strike': 780.0}]
    news = [{'headline': 'Stocks open little changed; ETF flows steady',
             'symbols': ['SPY'],
             'created_at': dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')}]
    choice, raw = A.decide(cand, obs, news, rules)
    ctx = (choice.get('veto') or {}).get('context') or {}
    if ctx.get('source') not in ('llm', 'cache'):
        return WARN, f"classifier not reached ({ctx.get('source')}: {choice.get('rationale', '')[:60]})"
    if choice.get('action') not in ('open', 'pass'):
        return FAIL, f"unexpected decision shape: {choice}"
    if choice['action'] == 'pass':
        return WARN, (f"benign tape classified {ctx.get('catalyst_scope')}/{ctx.get('jump_severity')} "
                      f"-> PASS; the veto may be over-firing")
    return PASS, (f"{rules['llm']['model']} classified benign tape as "
                  f"{ctx.get('catalyst_scope')}/{ctx.get('jump_severity')} -> open")


def c_rules():
    r = _rules()
    s = r['strategy']
    n = len(r['universe'].get('underlyings') or [])
    # Name the book from its own account label where it has one. "FLOOR-n" is
    # FLOOR-12's identity, not a generic label -- printing it over a condor book
    # made preflight describe a strategy that was not loaded.
    label = (r.get('account') or {}).get('label') or f"FLOOR-{n}"
    return PASS, (f"{label} [{n} names] {s['kind']} {s['target_delta']}d "
                  f"{'width '+str(s.get('width_pct'))+' of spot' if s.get('width_pct') else str(s['width'])+'-wide'}, "
                  f"IV/RV>={s.get('min_iv_rv_ratio')}, risk "
                  f"{r['risk']['max_risk_per_trade_pct']:.0%}/trade")


def c_live_risk_policy():
    import live_book as LB
    r = _rules()
    got = float(r['risk']['max_risk_per_trade_pct'])
    if got != LB.LIVE_RISK_PCT:
        return FAIL, (f"{os.path.basename(RULES_PATH)} {got:.2%} != locked "
                      f"LIVE_RISK_PCT {LB.LIVE_RISK_PCT:.0%}")
    return PASS, f"{LB.LIVE_RISK_PCT:.0%}/trade (operator-locked, agent/live_book.py)"


def c_flat_by_date():
    r = _rules()
    d = r['schedule'].get('flat_by_date')
    if not d:
        return FAIL, "no flat_by_date -- the agent would carry positions into judging"
    left = (dt.date.fromisoformat(d) - dt.date.today()).days
    if left < 0:
        return WARN, f"{d} has passed -- the agent will close everything and not open"
    return PASS, f"{d} ({left} day(s) away)"


def c_backtest_artifacts():
    runs = os.path.join(REPO_ROOT, 'runs')
    if not os.path.isdir(runs) or not os.listdir(runs):
        return WARN, "no backtest runs on disk"
    latest = max((os.path.join(runs, d) for d in os.listdir(runs)), key=os.path.getmtime)
    sm = os.path.join(latest, 'summary.json')
    if not os.path.exists(sm):
        return WARN, f"{os.path.basename(latest)} has no summary.json"
    m = json.load(open(sm))['metrics']
    return PASS, (f"{os.path.basename(latest)}: {m['total_return']:.2%}, "
                  f"Sharpe {m['sharpe']:.2f}")


def c_market_store():
    db = os.path.join(REPO_ROOT, 'data', 'market.duckdb')
    if not os.path.exists(db):
        return WARN, "no market.duckdb (needed for backtests, not for live trading)"
    return PASS, f"{os.path.getsize(db) / 1e6:.0f} MB"


def c_launchd():
    out = subprocess.run(['launchctl', 'list'], capture_output=True, text=True).stdout
    line = next((l for l in out.splitlines() if 'alpaca-agent' in l), None)
    if not line:
        return WARN, "timer not loaded (run the RUNBOOK scheduling steps)"
    status = line.split()[1]
    if status not in ('0', '-'):
        return FAIL, f"last scheduled run exited {status} -- check /tmp/nuka-alpaca-agent.log"
    return PASS, f"loaded, last exit {status}"


def _dotenv_raw(name):
    """Read `name` from the dotenv file ONLY -- load_env_var falls back to the
    environment, which makes it useless for telling the two sources apart."""
    from alpaca.env import DOTENV_PATH
    if not os.path.exists(DOTENV_PATH):
        return None
    for line in open(DOTENV_PATH):
        line = line.strip()
        if line.startswith(f'{name}='):
            return line.split('=', 1)[1].strip().strip('"\'') or None
    return None


def _truthy(raw):
    return None if raw is None else raw.strip().lower() in ('1', 'true', 'yes', 'on')


def c_shadow_setting():
    """
    Report the EFFECTIVE shadow setting for BOTH contexts the agent runs in.

    This check used to read os.environ directly and then describe the plist, which
    was two lies in one line: it never asked the function the agent actually asks,
    and until env_flag was fixed the dotenv silently beat the plist -- so a timer
    configured SHADOW=1 traded for real while this line printed a reassuring "set to
    0 before the competition run".

    The two contexts genuinely differ and both are worth printing. Run by hand, the
    flag resolves from this shell (environment, else dotenv). Run by launchd, the
    plist supplies the environment, and since env_flag now prefers the environment
    the plist wins. Reporting only one of them is how this went wrong the first time.
    """
    from alpaca.env import env_flag
    shell = env_flag('SHADOW')
    in_env = os.environ.get('SHADOW') or None
    in_dotenv = _dotenv_raw('SHADOW')
    plist_path = os.path.expanduser('~/Library/LaunchAgents/com.nuka.alpaca-agent.plist')
    in_plist = None
    if os.path.exists(plist_path):
        body = open(plist_path).read()
        if '<key>SHADOW</key>' in body:
            seg = body.split('<key>SHADOW</key>', 1)[1]
            in_plist = seg.split('<string>', 1)[1].split('</string>', 1)[0] or None

    # launchd exports the plist value, and the environment now wins, so the plist
    # decides a scheduled run whenever it sets the key at all.
    timer = _truthy(in_plist)
    if timer is None:
        timer = _truthy(in_env) if in_env else bool(_truthy(in_dotenv))

    src = f"env={in_env or 'unset'}, dotenv={in_dotenv or 'unset'}, plist={in_plist or 'unset'}"
    say = lambda b: 'DRY-RUN' if b else 'REAL orders'
    if shell != timer:
        return WARN, (f"this shell -> {say(shell)}, launchd -> {say(timer)}; "
                      f"the scheduled run is what trades ({src})")
    if shell:
        return WARN, f"SHADOW on -- every order is --dry-run in both contexts ({src})"
    return PASS, f"shadow off -- orders are REAL on the paper account, both contexts ({src})"


def c_skills():
    """options-core-patterns must be on disk; the agent fails closed without it."""
    import skills as SK
    have = SK.load_skills()
    miss = SK.missing_required(_rules(), have)
    if miss:
        return FAIL, f"required skills missing: {miss}"
    refs = SK.missing_refs()
    if refs:
        return FAIL, f"skill references missing: {refs}"
    path = have['options-core-patterns']['path']
    return PASS, f"{', '.join(sorted(have))} + refs ({os.path.relpath(path, REPO_ROOT)})"


def c_no_live_path():
    """Structural safety: the agent must never be able to construct --live."""
    agent_dir = os.path.join(REPO_ROOT, 'agent')
    for fn in os.listdir(agent_dir):
        if not fn.endswith('.py') or fn.startswith('test_') or fn == 'preflight.py':
            continue
        with open(os.path.join(agent_dir, fn)) as f:
            for i, line in enumerate(f, 1):
                if "'--live'" in line or '"--live"' in line:
                    return FAIL, f"{fn}:{i} constructs --live"
    return PASS, "no --live path in agent/"


# Which rulebook every check below reads. Set once in main() from --profile, because a
# preflight that verifies a DIFFERENT book than the operator is about to run is worse
# than no preflight -- it reports green on the wrong account. That exact bug was fixed
# once already for the credential slot; --profile is the same bug wearing a new flag.
# The default book is account C's -- the submission book. In the development repo this
# pointed at agent/agent_rules.json, a retired rulebook that is not part of this
# submission; leaving it would have shipped a default that names a file nobody can read.
# Both shipped books are still reached explicitly with --profile, which is what the
# Makefile and the README use.
RULES_PATH = os.path.join(REPO_ROOT, 'agent', 'profiles', 'putcr-core6-d47',
                          'rules.json')


def _rules():
    with open(RULES_PATH) as f:
        return json.load(f)


def c_structure_registry():
    """Registry (state.json) and broker positions must agree, or the agent halts on its first cycle."""
    import structures as ST
    state = A.load_state()
    if state.get('halted'):
        return FAIL, f"agent is HALTED ({state.get('halt_reason', 'drawdown')}) -- clear state.json deliberately"
    positions = A.cli(['position', 'list'], allow_fail=True) or []
    rec = ST.reconcile(state, {'positions': positions})
    if rec['stock']:
        return FAIL, f"stock at broker: {[s['symbol'] for s in rec['stock']]} (assignment?) -- flatten first"
    if rec['orphans']:
        return FAIL, f"{len(rec['orphans'])} option leg(s) not in the registry: {rec['orphans'][:4]}"
    n_open = len([x for x in ST.open_structures(state) if x['status'] == 'open'])
    n_pending = len([x for x in ST.open_structures(state) if x['status'] == 'pending'])
    return PASS, f"{n_open} open structure(s), {n_pending} pending, no orphans, no stock"


def c_event_calendar():
    """The blackout list must exist and cover the entry horizon; approximate dates warn."""
    rules = _rules()
    events = rules['schedule'].get('event_blackout') or []
    if not events:
        return FAIL, "schedule.event_blackout is empty -- the agent would sell gamma into FOMC/CPI/NFP"
    today = dt.date.today()
    future = [e for e in events if dt.date.fromisoformat(e['date']) >= today]
    if not future:
        return WARN, "every listed event is in the past -- refresh the calendar"
    approx = []
    csv_path = os.path.join(REPO_ROOT, 'data', 'events.csv')
    if os.path.exists(csv_path):
        import csv
        for row in csv.DictReader(open(csv_path)):
            d = dt.date.fromisoformat(row['event_date'])
            if 'APPROX' in row.get('source', '') and today <= d <= today + dt.timedelta(days=30):
                approx.append(row['event_date'])
    nxt = min(future, key=lambda e: e['date'])
    if approx:
        return WARN, f"next {nxt['event']} {nxt['date']}; APPROX rows within 30d in data/events.csv: {approx}"
    return PASS, f"next {nxt['event']} on {nxt['date']} ({len(future)} upcoming)"


def c_exit_policy():
    """Exits must be structure-level and close before expiry."""
    rules = _rules()
    ex = rules['exits']
    if 'close_at_dte_time' not in ex:
        return FAIL, "exits.close_at_dte_time missing -- structures would settle at expiry (pin/assignment risk)"
    cutoff = dt.time.fromisoformat(ex['close_at_dte_time'])
    if int(ex.get('close_at_dte', 0)) == 0 and cutoff > dt.time(15, 30):
        return FAIL, f"close_at_dte_time {cutoff:%H:%M} is too close to expiry -- use <= 15:30 ET"
    gate = rules['strategy'].get('vol_gate_iv', 'short_leg')
    if gate not in ('short_leg', 'atm'):
        return FAIL, f"unknown vol_gate_iv={gate}"
    cad = rules['schedule'].get('cadence_minutes', 30)
    if cad > 15:
        return WARN, f"cadence {cad} min -- stops are checked too rarely for DTE 1-7"
    return PASS, f"dte exit at DTE<={ex.get('close_at_dte', 0)} @ {cutoff:%H:%M} ET, gate={gate}, {cad}-min cadence"


def main():
    global RULES_PATH
    argv = sys.argv[1:]
    if '--profile' in argv:
        name = argv[argv.index('--profile') + 1]
        if os.sep in name or name in ('.', '..'):
            print(f"ERROR: --profile takes a name, not a path: {name}")
            sys.exit(1)
        RULES_PATH = os.path.join(A.PROFILE_DIR, name, 'rules.json')
        if not os.path.isfile(RULES_PATH):
            print(f"ERROR: no such profile: {os.path.relpath(RULES_PATH, REPO_ROOT)}")
            sys.exit(1)
    print(f"Alpaca Options Agent -- preflight  {dt.datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"  rulebook: {os.path.relpath(RULES_PATH, REPO_ROOT)}\n")
    # Point every check below at the SAME account the agent would trade. agent_loop
    # sets these from the rulebook inside main(), which preflight never calls, so
    # without this preflight silently verifies the default account while the agent
    # trades another one -- every check green, on the wrong book. That is a worse
    # failure than no preflight at all, because it manufactures confidence.
    _acct = (_rules().get('account') or {})
    if _acct:
        A.KEY_ENV = _acct.get('key_env', A.KEY_ENV)
        A.SECRET_ENV = _acct.get('secret_env', A.SECRET_ENV)
        print(f"  book: {_acct.get('label') or '(unnamed)'} -> expects account "
              f"{_acct.get('account_id')} via {A.KEY_ENV}\n")
    for name, fn in [
        ("credentials", c_credentials),
        ("alpaca CLI", c_cli),
        ("CLI on a minimal PATH", c_cli_from_minimal_path),
        ("broker reachable", c_broker),
        ("paper host (not live)", c_paper_host),
        ("options trading level", c_options_level),
        ("starting equity", c_equity),
        ("account flat & clean", c_flat_and_clean),
        ("option chain data", c_market_data),
        ("realized vol (vol gate)", c_realized_vol),
        ("LLM decision step", c_llm),
        ("strategy rules", c_rules),
        ("live risk policy", c_live_risk_policy),
        ("flat-by date", c_flat_by_date),
        ("structure registry", c_structure_registry),
        ("event blackout calendar", c_event_calendar),
        ("exit policy", c_exit_policy),
        ("backtest artifacts", c_backtest_artifacts),
        ("market data store", c_market_store),
        ("launchd timer", c_launchd),
        ("shadow setting", c_shadow_setting),
        ("no live-trading path", c_no_live_path),
        ("options-core-patterns skill", c_skills),
    ]:
        check(name, fn)

    fails = [r for r in results if r[0] == FAIL]
    warns = [r for r in results if r[0] == WARN]
    print(f"\n{len(results) - len(fails) - len(warns)} passed, "
          f"{len(warns)} warning(s), {len(fails)} failure(s)")
    if fails:
        print("\nMust fix before trading:")
        for _, name, detail in fails:
            print(f"  - {name}: {detail}")
        sys.exit(1)
    if warns:
        print("\nWorth a look:")
        for _, name, detail in warns:
            print(f"  - {name}: {detail}")
    print("\nReady." if not fails else "")


if __name__ == "__main__":
    main()
