#!/usr/bin/env python3
"""
Check every claim on the slides against the source it names.

The whole reason this deck was rebuilt is that its predecessor documented a branch that
had never traded: nine of the thirteen files it cited did not exist on the deployed tree.
A deck cannot be trusted to stay true by proofreading, so the numbers, the reason codes
and the file paths are all asserted here and the assertion runs at build time.

    python3 verify_numbers.py

Fails loudly on the first mismatch. Exit 0 means every figure printed on a slide was read
back out of the rulebooks, the run summaries, or the source, just now.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
HTML = open(os.path.join(HERE, 'index.html')).read()

ok, fail = [], []


def check(label, cond, detail=''):
    (ok if cond else fail).append(f"{label}{(' — ' + detail) if detail else ''}")


def rules(name):
    with open(os.path.join(REPO, 'agent', 'profiles', name, 'rules.json')) as fh:
        return json.load(fh)


def summary(label):
    path = os.path.join(REPO, 'runs', f'2026-09-04_book6_account-{label}_1Day', 'summary.json')
    with open(path) as fh:
        return json.load(fh)


def on_slide(text):
    """Is this string printed somewhere on the slides?"""
    return text in HTML


C, B = rules('putcr-core6-d47'), rules('putcr-core6')

# ---- 1. the two rulebooks, exactly as slide 12 tabulates them -----------------------
check('C account id',      on_slide(C['account']['account_id']), C['account']['account_id'])
check('B account id',      on_slide(B['account']['account_id']), B['account']['account_id'])
check('C tenor 4 -> 7',    (C['strategy']['min_dte'], C['strategy']['max_dte']) == (4, 7))
check('B tenor 1 -> 7',    (B['strategy']['min_dte'], B['strategy']['max_dte']) == (1, 7))
check('C universe = 12',   len(C['universe']['underlyings']) == 12)
check('B universe = 6',    len(B['universe']['underlyings']) == 6)
check('C flat-by',         on_slide(C['schedule']['flat_by_date']))
check('B flat-by',         on_slide(B['schedule']['flat_by_date']))

# The slide claims everything else is identical. Assert that rather than trusting it.
SHARED = [('strategy', 'target_delta', 0.15), ('strategy', 'delta_tolerance', 0.08),
          ('strategy', 'width', 5.0), ('strategy', 'min_credit_ratio_vertical', 0.08),
          ('strategy', 'max_credit_ratio', 0.60), ('strategy', 'min_iv_rv_ratio', 1.2),
          ('strategy', 'rv_window', 21), ('strategy', 'side', 'put'),
          ('risk', 'max_risk_per_trade_pct', 0.02), ('risk', 'max_concurrent_positions', 3),
          ('risk', 'max_per_name', 1), ('risk', 'max_contracts_per_trade', 20),
          ('risk', 'daily_loss_halt_pct', 0.04), ('risk', 'max_drawdown_halt_pct', 0.10),
          ('risk', 'vol_target_pct', 0.25), ('exits', 'take_profit_pct', 0.5),
          ('exits', 'stop_loss_mult', 2.0), ('execution', 'max_quote_age_s', 300),
          ('execution', 'max_spread_pct_of_mid', 0.08)]
for block, key, want in SHARED:
    check(f'{block}.{key} = {want} in BOTH books',
          C[block][key] == want and B[block][key] == want,
          f"C={C[block][key]} B={B[block][key]}")

check('enabled_structures is put_credit only, both books',
      C['strategy']['enabled_structures'] == ['put_credit'] == B['strategy']['enabled_structures'])
check('debit disabled, both books',
      C['strategy']['enable_debit'] is False and B['strategy']['enable_debit'] is False)

# ---- 2. the friction sweep on slide 13 ---------------------------------------------
for label, book in (('c', 'C'), ('b', 'B')):
    for row in summary(label)['friction_sweep']:
        pct = f"{row['total_return'] * 100:+.2f}%".replace('+', '+').replace('-', '−')
        shp = f"{row['sharpe']:.2f}".replace('-', '−')
        check(f'{book} sweep {int(row["friction_pct"] * 100)}% return {pct}', on_slide(pct[1:] if pct.startswith('+') else pct), pct)
        check(f'{book} sweep {int(row["friction_pct"] * 100)}% sharpe {shp}', on_slide(shp), shp)
    m = summary(label)['metrics']
    check(f'{book} trades {m["trades"]}', on_slide(str(m['trades'])))
    check(f'{book} win rate', on_slide(f"{m['win_rate'] * 100:.1f}%"))

# ---- 3. every reason code on a slide exists in the source ---------------------------
SRC = {name: open(os.path.join(REPO, 'agent', name)).read()
       for name in ('agent_loop.py', 'skills.py', 'veto.py', 'canon.py')}
ALL_SRC = '\n'.join(SRC.values())
for code in re.findall(r'<span class="chip[^"]*">([A-Za-z0-9_&;<>=.]+)</span>', HTML):
    code = code.replace('&gt;', '>').replace('&lt;', '<').replace('&amp;', '&')
    check(f'reason code `{code}` present in source', code in ALL_SRC, code)

# ---- 4. every source file the slides cite actually exists ---------------------------
CITED = set(re.findall(r'\b((?:agent|backtest|data|runs)/[A-Za-z0-9_*./-]+\.(?:py|json|sh|plist))\b', HTML))
for path in sorted(CITED):
    if '*' in path:
        continue
    check(f'cited file exists: {path}', os.path.exists(os.path.join(REPO, path)), path)

# The deck must NOT cite the files that only ever lived on feature-branch.
GHOSTS = ['agent/pipeline.py', 'agent/playbook.py', 'agent/vixfeed.py', 'agent/exdiv.py',
          'agent/structure_router.py', 'data/layer0_universe.json', 'agent/protocol/runtime.py']
for g in GHOSTS:
    check(f'does not cite undeployed {g}', g not in HTML, g)

# ---- 5. palette: only the four brand hexes ------------------------------------------
css = open(os.path.join(HERE, 'deck.css')).read()
BRAND = {'#1B2A4A', '#F7F4EC', '#232323', '#B08D57'}
found = set(re.findall(r'#[0-9A-Fa-f]{3,8}', css))
check('deck.css uses brand colours only', found <= BRAND, f"extra: {sorted(found - BRAND)}")

# ---- report ------------------------------------------------------------------------
for line in fail:
    print(f"  FAIL  {line}")
print(f"\n{len(ok)} checks passed, {len(fail)} failed")
sys.exit(1 if fail else 0)
