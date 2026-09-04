"""
Paper deploy: isolated strategy books on one Alpaca paper account.

PARKED as of 2026-08-31 -- read this before running the three books together.

  * `frozen-b16` is EXACTLY `champ-pv6` union `new10-holdout` (the two are disjoint).
    Running all three double-books every position: once by its own book, once by the
    union, each at a third of equity. That is not an A/B/C comparison, it is one book
    with duplicates.
  * The halts read `obs['day_pnl_pct']` and `obs['equity']`, which are ACCOUNT figures,
    while each book controls a third of the risk. A book's own loss must move the whole
    account 15% to trip its drawdown halt, so the labelled -15% is about -45% per book;
    and one book blowing up halts the other two.
  * Splitting a 16-name book in two changes the SELECTION, not just the accounting: the
    measured result came from one pooled ranking by credit/width across all names, and
    two books with separate caps admit candidates the pooled ranking would have bumped.

Until the halts are moved onto per-book P&L, run ONE book. The single-book path is
`agent/agent_rules.json` (FLOOR-12) via `make live`; this module's machinery -- per-book
journal and state, order tags, the shared leg registry -- stays for when there is more
than one book and more than four sessions to compare them over.

Each strategy has its own rules, journal and state under agent/strategies/<id>/.
Orders are tagged in client_order_id; open legs are recorded in a shared registry so
one book's reconcile does not treat another book's legs as orphans.

Usage:
    python3 agent/deploy.py list
    python3 agent/deploy.py preflight
    python3 agent/deploy.py gate
"""
from __future__ import annotations

import json
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
STRATEGIES_DIR = os.path.join(REPO, 'agent', 'strategies')
REGISTRY_PATH = os.path.join(REPO, 'agent', 'deploy', 'leg_registry.json')

PARKED_REASON = """\
DEPLOY PARKED — multi-book run blocked.

  Run ONE book: make preflight && make live  (agent/agent_rules.json, FLOOR-12).

  Why blocked: frozen-b16 duplicates champ-pv6 + new10-holdout; halts read account
  P&L while each book sizes at equity_fraction 1/3; split ranking pools != measured b16.

  To override (not for contest): DEPLOY_ALLOW=1 make deploy-live
"""


def require_allowed():
    """Refuse multi-book deploy unless the operator explicitly opts in."""
    if os.environ.get('DEPLOY_ALLOW') == '1':
        return
    print(PARKED_REASON, file=sys.stderr)
    raise SystemExit(2)

# Top three books from the research cycle (distinct universes, same frozen mechanics).
STRATEGY_IDS = ('champ-pv6', 'frozen-b16', 'new10-holdout')

CORE6 = ['SPY', 'QQQ', 'IWM', 'GLD', 'XLF', 'SMH']
B16 = CORE6 + ['USO', 'IEF', 'UNG', 'TLT', 'ASHR', 'XLE', 'XLU', 'IBIT', 'XLV', 'SLV']
NEW10 = ['USO', 'IEF', 'UNG', 'TLT', 'ASHR', 'XLE', 'XLU', 'IBIT', 'XLV', 'SLV']

STRATEGY_META = {
    'champ-pv6': {
        'title': 'CORE-6 champion put vertical',
        'backtest_run': 'runs/2026-08-30_book6_champ-pv-b6_1Day',
        'order_tag': 'pv6',
        'underlyings': CORE6,
        # Backtest headline is FULL equity at 2% risk per trade. Deployed here it is
        # equity_fraction x 1.25%, so the number a reader should carry is the measured
        # point on the risk ladder at that size, not the headline.
        'headline_ann_3pct': '+31.3% @ full equity, 2% risk',
        'deploy_ann_expectation': '~+7-8% @ 0.42% effective risk (measured ladder point)',
    },
    'frozen-b16': {
        'title': 'ALL-16 frozen put vertical',
        'backtest_run': 'runs/2026-08-30_book16_frozen-b16_1Day',
        'order_tag': 'b16',
        'underlyings': B16,
        'headline_ann_3pct': '+35.9% @ full equity, 2% risk',
        'deploy_ann_expectation': '~+7-8% @ 0.42% effective risk (measured ladder point)',
    },
    'new10-holdout': {
        'title': 'NEW-10 holdout put vertical',
        'backtest_run': 'runs/_grid/breadth.json (NEW-10 slice)',
        'order_tag': 'n10',
        'underlyings': NEW10,
        'headline_ann_3pct': '+10.0% @ full equity, 2% risk',
        'deploy_ann_expectation': '~+2-3% @ 0.42% effective risk',
    },
}


def strategy_dir(strategy_id):
    return os.path.join(STRATEGIES_DIR, strategy_id)


def rules_path(strategy_id):
    return os.path.join(strategy_dir(strategy_id), 'rules.json')


def journal_dir(strategy_id):
    return os.path.join(strategy_dir(strategy_id), 'decisions')


def resolve(strategy_id):
    """Return (rules_path, journal_dir) or raise."""
    if strategy_id not in STRATEGY_META:
        raise ValueError(f"unknown strategy {strategy_id!r}; choose from {STRATEGY_IDS}")
    rp, jd = rules_path(strategy_id), journal_dir(strategy_id)
    if not os.path.isfile(rp):
        raise FileNotFoundError(f"missing rules: {rp}")
    return rp, jd


def load_registry():
    if not os.path.isfile(REGISTRY_PATH):
        return {}
    with open(REGISTRY_PATH) as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def save_registry(reg):
    os.makedirs(os.path.dirname(REGISTRY_PATH), exist_ok=True)
    with open(REGISTRY_PATH, 'w') as f:
        json.dump(reg, f, indent=2, sort_keys=True)


def sync_leg_registry(strategy_id, state):
    """Rewrite this strategy's leg claims from its structure registry."""
    reg = load_registry()
    for occ, sid in list(reg.items()):
        if sid == strategy_id:
            del reg[occ]
    for s in (state.get('structures') or {}).values():
        if s.get('status') not in ('pending', 'open', 'closing', 'shadow'):
            continue
        for leg in s.get('legs') or []:
            occ = leg.get('occ')
            if occ:
                reg[occ] = strategy_id
    save_registry(reg)


def foreign_occs(strategy_id):
    return {occ for occ, sid in load_registry().items() if sid != strategy_id}


def deploy_cfg(rules):
    return rules.get('deploy') or {}


def equity_scale(rules):
    d = deploy_cfg(rules)
    return float(d.get('equity_fraction') or 1.0)


def order_tag(rules):
    d = deploy_cfg(rules)
    return d.get('order_tag') or d.get('strategy_id') or 'agent'


def list_strategies():
    for sid in STRATEGY_IDS:
        meta = STRATEGY_META[sid]
        print(f"  {sid:<16} {meta['title']}")
        print(f"    universe: {len(meta['underlyings'])} names  backtest: {meta['backtest_run']}")
        print(f"    backtest headline: {meta['headline_ann_3pct']}")
        print(f"    expected at deploy size: {meta.get('deploy_ann_expectation', 'n/a')}")
        print(f"    rules: {rules_path(sid)}")
        print(f"    journal: {journal_dir(sid)}/")


def preflight():
    import agent_loop as A
    from alpaca.env import load_env_var

    ok = True
    key = load_env_var('ANTHROPIC_API_KEY', required=False)
    if not key:
        print('  [FAIL] ANTHROPIC_API_KEY not set — veto step will PASS on every cycle')
        ok = False
    else:
        print(f'  [ok  ] Anthropic key …{key[-4:]}')
    pk = load_env_var('ALPACA_API_KEY', required=False)
    if not pk or not pk.startswith('PK'):
        print('  [FAIL] paper ALPACA_API_KEY missing or not PK…')
        ok = False
    else:
        print(f'  [ok  ] Alpaca paper key {pk[:6]}…')
    try:
        A._resolve_cli()
        print('  [ok  ] alpaca CLI')
    except SystemExit as e:
        print(f'  [FAIL] alpaca CLI: {e}')
        ok = False
    for sid in STRATEGY_IDS:
        rp, jd = resolve(sid)
        with open(rp) as f:
            rules = json.load(f)
        d = deploy_cfg(rules)
        if d.get('strategy_id') != sid:
            print(f'  [FAIL] {sid}: deploy.strategy_id mismatch')
            ok = False
            continue
        if rules.get('llm', {}).get('deterministic_fallback'):
            print(f'  [FAIL] {sid}: deterministic_fallback must be false for Anthropic veto')
            ok = False
            continue
        print(f'  [ok  ] {sid}: {len(rules["universe"]["underlyings"])} names, '
              f'tag={order_tag(rules)}, equity_fraction={equity_scale(rules):.2f}')
    return 0 if ok else 1


def bootstrap_rules():
    """Write agent/strategies/*/rules.json from agent_rules.json template."""
    src = os.path.join(REPO, 'agent', 'agent_rules.json')
    with open(src) as f:
        base = json.load(f)
    n = len(STRATEGY_IDS)
    frac = round(1.0 / n, 4)
    for sid in STRATEGY_IDS:
        meta = STRATEGY_META[sid]
        rules = json.loads(json.dumps(base))
        rules['_comment'] = [
            f"Paper deploy book: {meta['title']}.",
            f"Backtest reference: {meta['backtest_run']}.",
            'Shared mechanics: put vertical DTE 4-7, delta 0.20, IV/RV>=1.0, TP 50%, stop off.',
            f"One of {n} parallel books; equity_fraction={frac} per book on the shared paper account.",
        ]
        rules['universe']['underlyings'] = list(meta['underlyings'])
        rules['universe']['_why'] = (
            f"Deploy universe for {sid}. See {meta['backtest_run']}."
        )
        rules['risk']['max_concurrent_positions'] = max(6, int(20 / n))
        rules['deploy'] = {
            'strategy_id': sid,
            'order_tag': meta['order_tag'],
            'equity_fraction': frac,
            'backtest_run': meta['backtest_run'],
            'title': meta['title'],
        }
        rules['llm']['deterministic_fallback'] = False
        out_dir = strategy_dir(sid)
        os.makedirs(out_dir, exist_ok=True)
        os.makedirs(journal_dir(sid), exist_ok=True)
        with open(rules_path(sid), 'w') as f:
            json.dump(rules, f, indent=2)
            f.write('\n')
        print(f'wrote {rules_path(sid)}')


if __name__ == '__main__':
    cmd = (sys.argv[1] if len(sys.argv) > 1 else 'list').lower()
    if cmd == 'list':
        list_strategies()
    elif cmd == 'preflight':
        raise SystemExit(preflight())
    elif cmd == 'bootstrap':
        bootstrap_rules()
    elif cmd == 'gate':
        require_allowed()
    else:
        print(f'usage: {sys.argv[0]} [list|preflight|bootstrap|gate]')
        raise SystemExit(2)
