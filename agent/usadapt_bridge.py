"""
Bridge between the live agent and the US-ADAPT layer (Horizon B of the plan).

When `agent_rules.json -> universe.mode == "funnel"` the agent's `build_candidates` is
replaced by the three-stage US-ADAPT pipeline in `us_adaptive/`:

    Stage 1  assets by profit potential   (census straddle vs trailing RV, top-N)
    Stage 2  asset dynamics and surface   (HAR forecast, MAE, gaps, live IV, regime)
    Stage 3  structure selection          (registered grid, executable EV, gates,
                                           EV per capital-day, budgeted allocation)

The output of stage 3 -- `scan.run_scan(...)['intents']` -- is converted here into the
candidate dictionaries the agent already validates, journals and submits, so
`validate()`, `submit_spread()` and the structure registry are unchanged.

Only defined-risk CREDIT structures are executable by this agent version (its validator
requires `credit > 0`); long-vol intents (S4-S7) are journalled as `not_executable` and
skipped. Everything else the funnel decides -- regime, haircuts, budgets, reservation
hurdle -- is carried inside the intent and re-checked by `validate()`.

Data prerequisites (see data/pit_ingest.py and us_adaptive/INSTRUCTIONS in the package):
a DuckDB holding the Alpaca contract census (the data-profile output) plus the pit_*
tables. Without them the funnel fails closed and returns no candidates.
"""
import datetime as dt
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, REPO_ROOT)

CONTRACT_MULTIPLIER = 100


def intent_to_candidate(intent):
    """
    Map a US-ADAPT intent to the agent's candidate shape. Returns None for structures
    this agent cannot submit (debit / long-vol families).
    """
    legs = intent['legs']
    shorts = [l for l in legs if l['side'] == 'SELL']
    longs = [l for l in legs if l['side'] == 'BUY']
    if intent['structure'] in ('S4_DEBIT_VERTICAL', 'S5_LONG_STRADDLE', 'S6_LONG_STRANGLE',
                               'S7_REVERSE_IRON_CONDOR') or intent['credit_exec'] <= 0:
        return None
    qty = int(intent['qty'])
    # 'credit' is the EXECUTABLE credit (touch): max_loss and the validator's one-wing
    # arithmetic are consistent with it. The starting limit comes from the exec plan.
    credit = float(intent['credit_exec'])
    base = {
        'id': f"USADAPT-{intent['decision_id'][:8]}-{intent['symbol']}-{intent['structure']}",
        'underlying': intent['symbol'],
        'expiry': intent['expiration'],
        'dte': int(intent.get('dte', 0)),
        'qty': qty,
        'credit': round(credit, 3),
        'credit_mid': round(float(intent['credit_mid']), 3),
        'limit_credit': float(intent['limit_credit']),
        'floor_credit': float(intent['floor_credit']),
        'credit_ratio': None,
        'max_loss': round(float(intent['max_loss_total']), 2),
        'max_profit': round(credit * CONTRACT_MULTIPLIER * qty, 2),
        'worst_leg_spread_pct': max(((l['ask'] - l['bid']) / ((l['ask'] + l['bid']) / 2)) if l['bid'] and l['ask'] else 1.0
                                    for l in legs),
        'usadapt': {k: intent.get(k) for k in ('ev_exec_total', 'pnl_per_capital_day', 'edge_on_margin',
                                               'edge_cost_ratio', 'p_profit', 'p_touch', 'haircut',
                                               'haircut_why', 'book_score', 'data_ok', 'source')},
        'decision_id': intent['decision_id'],
    }
    if intent['structure'] == 'S2_IRON_CONDOR':
        ps = next(l for l in shorts if l['cp'] == 'P'); pl = next(l for l in longs if l['cp'] == 'P')
        cs = next(l for l in shorts if l['cp'] == 'C'); cl = next(l for l in longs if l['cp'] == 'C')
        width = max(ps['strike'] - pl['strike'], cl['strike'] - cs['strike'])
        base.update({
            'kind': 'iron_condor', 'structure': 'condor',
            'put_short_occ': ps['symbol'], 'put_long_occ': pl['symbol'],
            'put_short_strike': ps['strike'], 'put_long_strike': pl['strike'],
            'call_short_occ': cs['symbol'], 'call_long_occ': cl['symbol'],
            'call_short_strike': cs['strike'], 'call_long_strike': cl['strike'],
            'put_short_delta': ps.get('delta'), 'call_short_delta': cs.get('delta'),
            'net_delta': round((ps.get('delta') or 0) + (cs.get('delta') or 0), 4),
            'width': width, 'put_width': ps['strike'] - pl['strike'], 'call_width': cl['strike'] - cs['strike'],
            'credit_ratio': round(credit / width, 3) if width else None,
        })
    elif intent['structure'] in ('S1_PUT_VERTICAL', 'S1_CALL_VERTICAL', 'S3_IRON_FLY'):
        if intent['structure'] == 'S3_IRON_FLY':
            return None   # 4-leg fly: not in this agent's validator yet
        sh, lg = shorts[0], longs[0]
        width = abs(sh['strike'] - lg['strike'])
        base.update({
            'kind': 'put_credit' if sh['cp'] == 'P' else 'call_credit', 'structure': 'vertical',
            'right': sh['cp'], 'short_occ': sh['symbol'], 'long_occ': lg['symbol'],
            'short_strike': sh['strike'], 'long_strike': lg['strike'],
            'short_delta': sh.get('delta'), 'width': width,
            'credit_ratio': round(credit / width, 3) if width else None,
        })
    else:
        return None
    return base


def build_candidates_funnel(rules, obs, mode='SHADOW'):
    """Run the US-ADAPT pipeline and return agent-shaped candidates (credit structures only)."""
    from us_adaptive.db import load_config, connect
    from us_adaptive.scan import run_scan
    u = rules['universe'].get('usadapt', {})
    cfg = load_config(u.get('config', os.path.join(REPO_ROOT, 'us_adaptive', 'config.default.yaml')))
    cfg['db_path'] = u.get('db_path', cfg.get('db_path'))
    cfg['nav_usd'] = float(obs.get('equity') or cfg.get('nav_usd'))
    con = connect(cfg)
    try:
        res = run_scan(con, cfg, dt.date.today(), stage=u.get('stage', 'STAGE_0'), mode=mode)
    finally:
        con.close()
    out, skipped = [], []
    for it in res.get('intents', []):
        c = intent_to_candidate(it)
        if c is None:
            skipped.append({'symbol': it['symbol'], 'structure': it['structure'], 'reason': 'not_executable_by_agent'})
        elif not it.get('data_ok', False):
            skipped.append({'symbol': it['symbol'], 'structure': it['structure'], 'reason': 'quotes not live (shadow only)'})
        else:
            out.append(c)
    return out, {'regime': res.get('regime'), 'decision': res.get('decision'), 'skipped': skipped,
                 'rejections': res.get('rejections'), 'opportunity_cost': res.get('opportunity_cost')}


if __name__ == "__main__":
    fake = {'decision_id': 'abcdef1234', 'symbol': 'SPY', 'structure': 'S2_IRON_CONDOR', 'expiration': '2026-09-04',
            'dte': 8, 'qty': 3, 'credit_mid': 2.28, 'credit_exec': 2.25, 'limit_credit': 2.27, 'floor_credit': 2.27,
            'max_loss_total': 1725.0, 'data_ok': True, 'source': 'pit',
            'legs': [dict(symbol='SPY260904P00754000', side='SELL', cp='P', strike=754.0, bid=2.11, ask=2.13, delta=-0.22),
                     dict(symbol='SPY260904P00746000', side='BUY', cp='P', strike=746.0, bid=1.19, ask=1.21, delta=-0.12),
                     dict(symbol='SPY260904C00777000', side='SELL', cp='C', strike=777.0, bid=1.63, ask=1.65, delta=0.22),
                     dict(symbol='SPY260904C00785000', side='BUY', cp='C', strike=785.0, bid=0.33, ask=0.34, delta=0.07)]}
    c = intent_to_candidate(fake)
    assert c['structure'] == 'condor' and c['qty'] == 3 and c['width'] == 8.0 and c['credit'] == 2.25 and c['credit_mid'] == 2.28
    assert c['max_loss'] == 1725.0 and abs(c['net_delta']) < 0.01
    fake['structure'] = 'S5_LONG_STRADDLE'
    assert intent_to_candidate(fake) is None
    print("usadapt_bridge.py self-check OK")
