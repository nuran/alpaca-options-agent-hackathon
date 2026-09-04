"""
Load project skills and enforce the ones the live agent must obey in code.

The LLM never reads a skill to pick a trade. Skills are data: missing required
canon fails closed, banned underlyings are dropped, VIX regime can block new
short vega. That is P1/P5/P8/P12/V2/V5 of options-core-patterns — mechanics
turned into gates, not prompt text.
"""
from __future__ import annotations

import json
import os
import re

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
SKILLS_DIR = os.path.join(REPO, 'skills')
REQUIRED = ('options-core-patterns',)
REQUIRED_REFS = ('patterns.md', 'vix.md', 'short-dte.md', 'asset-selector.md',
                 'asset_selection_spec.md')

# P5: European cash-settled index products appear in feeds but are untradable here.
EUROPEAN_INDEX = frozenset({'SPX', 'XSP', 'VIX', 'DJX', 'VIXW', 'CBTX'})
# V5: path-dependent vol ETPs are not a VRP warehouse. VXX is kept for S5
# (tactical put debit in backwardation), never as a short-vol name.
VOL_ETP = frozenset({'UVXY', 'SVXY', 'SVIX', 'VIXY', 'VIXM', 'VXZ'})
CRYPTO_PROXY = frozenset({
    'IBIT', 'ETHA', 'MSTR', 'COIN', 'MARA', 'RIOT', 'CLSK', 'HUT', 'BITF',
    'IREN', 'BTDR', 'BITO', 'GBTC', 'ARKB', 'FBTC', 'BITB', 'HODL', 'ETHE',
    'WULF', 'CORZ', 'BMNR', 'CIFR', 'APLD', 'SBET', 'CRCL', 'GLXY',
})
LEVERAGED = re.compile(
    r'^(TQQQ|SQQQ|SOXL|SOXS|UPRO|SPXU|TNA|TZA|UDOW|SDOW|QLD|QID|SPXL|SPXS|'
    r'TMF|TMV|UCO|SCO|LABU|LABD|NAIL|CURE|DFEN|FAS|FAZ|NUGT|DUST|JNUG|JDST|'
    r'TSLL|NVDL)$'
)
# News-veto scopes stay SPY/QQQ/BOTH; map onto factor clusters (P7 index vs name).
QQQ_CLUSTER = frozenset({'QQQ', 'QQQM', 'XLK', 'SMH', 'SOXX', 'IGV'})
SPY_CLUSTER = frozenset({'SPY', 'IVV', 'VOO', 'DIA', 'IWM', 'MDY', 'SPLG'})

LLM_ADDENDUM = (
    "Canon options-core-patterns is enforced in code, not by you. Venue has no "
    "SPX/XSP/VIX options: index exposure is American ETFs (assignment, ex-div, "
    "pin are live — P5/P12). VIX is regime context, never a catalyst and never "
    "a tradable. VXX is not a hedge warehouse. Gaps (overnight, earnings, FOMC) "
    "are the tail calendar. Do not treat IV or the VIX level as a reason to "
    "trade or to pass."
)


def _parse_frontmatter(text):
    if not text.startswith('---'):
        return {}, text
    end = text.find('\n---', 3)
    if end < 0:
        return {}, text
    head, body = text[3:end].strip(), text[end + 4:]
    meta = {}
    for line in head.splitlines():
        if ':' not in line:
            continue
        k, v = line.split(':', 1)
        meta[k.strip()] = v.strip().strip('"')
    return meta, body


def load_skills(directory=None):
    """Every skills/<name>/SKILL.md, keyed by frontmatter name."""
    root = directory or SKILLS_DIR
    out = {}
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name, 'SKILL.md')
        if not os.path.isfile(path):
            continue
        meta, body = _parse_frontmatter(open(path).read())
        key = meta.get('name') or name
        out[key] = {'name': key, 'path': path, 'meta': meta, 'body': body}
    return out


def missing_required(rules=None, installed=None):
    need = list(REQUIRED)
    if rules:
        need = list((rules.get('skills') or {}).get('required') or need)
    have = installed if installed is not None else load_skills()
    return [n for n in need if n not in have]


def missing_refs(directory=None):
    root = os.path.join(directory or SKILLS_DIR, 'options-core-patterns', 'references')
    return [n for n in REQUIRED_REFS if not os.path.isfile(os.path.join(root, n))]


def filter_underlyings(symbols, rules=None):
    """P5/V5: drop names the venue or the canon forbids for the VRP book."""
    kept, dropped = [], []
    for s in symbols or []:
        u = (s or '').upper()
        if u in EUROPEAN_INDEX:
            dropped.append((s, 'P5_european_index')); continue
        if u in VOL_ETP:
            dropped.append((s, 'V5_vol_etp')); continue
        if u in CRYPTO_PROXY:
            dropped.append((s, 'P13_crypto_proxy')); continue
        if LEVERAGED.match(u):
            dropped.append((s, 'P13_leveraged_etp')); continue
        kept.append(s)
    return kept, dropped


def scope_touches(underlying, scope):
    """Map classifier scope onto the live book (not only SPY/QQQ)."""
    if scope == 'BOTH':
        return True
    u = (underlying or '').upper()
    if scope == 'QQQ_ONLY':
        return u in QQQ_CLUSTER
    if scope == 'SPY_ONLY':
        return u in SPY_CLUSTER
    if scope and scope.endswith('_ONLY'):
        return u == scope[:-5]
    return False


def load_ranked(rules=None):
    path = 'data/universe_ranked.json'
    if rules:
        path = ((rules.get('universe') or {}).get('selector') or {}).get('path', path)
    if not os.path.isabs(path):
        path = os.path.join(REPO, path)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def regime_gate(rules=None, ranked=None):
    """
    P8/V2: short vega only in contango-proxy and when VIX is not in the top
    1y stress decile. Missing data is not a licence — it is UNKNOWN, caller
    decides. Returns (ok, reason, vix_dict).
    """
    sk = (rules or {}).get('skills') or {}
    if sk.get('enforce_vix_regime') is False:
        return True, 'regime_gate_disabled', None
    ranked = ranked if ranked is not None else load_ranked(rules)
    vix = (ranked or {}).get('vix') if ranked else None
    if not vix:
        return True, 'no_vix_snapshot', None
    if vix.get('contango_proxy') is False:
        return False, 'P8_backwardation_vix3m_proxy', vix
    if (vix.get('vix_pct_1y') or 0) >= 0.70:
        return False, 'P8_vix_pct_1y>=0.70', vix
    return True, 'contango_proxy', vix


def stage5_allows(symbol, ranked=None, rules=None):
    """Stage 5 snapshot from the selector. Missing row is not a veto (live gates still run)."""
    ranked = ranked if ranked is not None else load_ranked(rules)
    if not ranked:
        return True, 'no_ranked_file'
    gate = (ranked.get('stage5') or {}).get(symbol)
    if not gate:
        return True, 'no_stage5_row'
    if gate.get('decision') == 'NO_TRADE':
        return False, 'S5_' + (gate.get('reasons') or ['no_trade'])[0]
    return True, gate.get('decision') or 'ok'


def annotate_candidates(candidates, rules=None, ranked=None):
    """Drop short-vol names the skill forbids. Returns (kept, dropped, info)."""
    ranked = ranked if ranked is not None else load_ranked(rules)
    ok_reg, why_reg, vix = regime_gate(rules, ranked)
    kept, dropped = [], []
    for c in candidates:
        u = c.get('underlying')
        names, drops = filter_underlyings([u], rules)
        if not names:
            dropped.append({'candidate_id': c.get('id'), 'underlying': u,
                            'reason': drops[0][1] if drops else 'skill_filter'})
            continue
        short = (c.get('side') or 'credit') != 'debit'
        if (u or '').upper() == 'VXX' and short:
            dropped.append({'candidate_id': c.get('id'), 'underlying': u,
                            'reason': 'V5_vxx_not_a_warehouse'})
            continue
        if short and not ok_reg:
            dropped.append({'candidate_id': c.get('id'), 'underlying': u, 'reason': why_reg})
            continue
        c = dict(c)
        c['skill'] = 'options-core-patterns'
        kept.append(c)
    return kept, dropped, {'regime': why_reg, 'vix': vix}


if __name__ == '__main__':
    have = load_skills()
    miss = missing_required(installed=have)
    assert not miss, f"missing required skills: {miss}"
    assert not missing_refs(), missing_refs()
    body = have['options-core-patterns']['body']
    for needle in ('## P1.', '## P12.', '## P8.', '**V5.**', 'SPX/XSP/VIX untradable'):
        assert needle in body, needle
    kept, dropped = filter_underlyings(['SPY', 'SPX', 'VIX', 'VXX', 'TQQQ', 'QQQ', 'IBIT'])
    assert kept == ['SPY', 'VXX', 'QQQ'], kept
    assert {s for s, _ in dropped} == {'SPX', 'VIX', 'TQQQ', 'IBIT'}
    assert scope_touches('QQQ', 'QQQ_ONLY') and not scope_touches('GLD', 'QQQ_ONLY')
    assert scope_touches('IWM', 'SPY_ONLY') and scope_touches('XLV', 'BOTH')
    assert not scope_touches('XLV', 'QQQ_ONLY')
    ranked = {'vix': {'contango_proxy': False, 'vix_pct_1y': 0.2},
              'stage5': {'XBI': {'decision': 'NO_TRADE', 'reasons': ['spread']}}}
    ok, why, _ = regime_gate({'skills': {'enforce_vix_regime': True}}, ranked)
    assert not ok and why.startswith('P8_'), why
    assert stage5_allows('XBI', ranked)[0] is False
    assert stage5_allows('QQQ', ranked)[0] is True
    calm = {'vix': {'contango_proxy': True, 'vix_pct_1y': 0.03},
            'stage5': {'XBI': {'decision': 'NO_TRADE', 'reasons': ['spread']}}}
    kept_c, _, _ = annotate_candidates(
        [{'id': 'xbi', 'underlying': 'XBI'}, {'id': 'qqq', 'underlying': 'QQQ'}],
        {'skills': {'enforce_vix_regime': True}}, ranked=calm)
    assert [c['id'] for c in kept_c] == ['xbi', 'qqq'], kept_c
    stress = {'vix': {'contango_proxy': False, 'vix_pct_1y': 0.8}}
    kept_s, drop_s, _ = annotate_candidates(
        [{'id': 'ic', 'underlying': 'SPY', 'side': 'credit'},
         {'id': 'db', 'underlying': 'SPY', 'side': 'debit'}],
        {'skills': {'enforce_vix_regime': True}}, ranked=stress)
    assert [c['id'] for c in kept_s] == ['db'], kept_s
    assert drop_s and drop_s[0]['candidate_id'] == 'ic'
    print('skills.py self-check OK', sorted(have))
