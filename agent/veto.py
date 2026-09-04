"""
News veto, v2.3: the LLM classifies context, deterministic code decides.

Lineage: the original single-call decide() (model picked a candidate from full JSON),
the v2 proposal (veto-only output), and the veto_agent_v2_2 package (classify -> decide
split, caching, enum outputs). This module keeps the split and fixes what the v2_2
package got wrong:

  * v2_2 PASSED whenever `actionable_preference == NONE` -- i.e. on every quiet day, which
    is exactly the day the vol gate was calibrated to trade. Quiet tape => ALLOW here.
  * v2_2 used a fixed "5-session horizon" and listed FOMC/CPI/NFP as veto reasons. The
    agent trades ~1-DTE condors; the horizon is the candidate's DTE, FOMC is blacked out
    in code before the model runs, and the CPI/NFP blackout was tested and rejected
    (CHANGELOG 2026-08-28). Scheduled releases are therefore NOT a veto reason.
  * v2_2 sent iv_rv_ratio / credit_ratio / net_delta to the model. They invite the model to
    re-validate. The model gets DTE and the short strikes (for the distance test) only.
  * v2_2 had no counterfactual. Every veto is journalled with the candidate it removed so
    `veto_value_added` can be computed weekly (agent_loop / weekly review).

Pipeline per cycle:
    headlines -> filter_headlines() -> context_hash
             -> classify_context()   (LLM, cached by context_hash; skipped when empty)
             -> decide()             (deterministic, per candidate)
Any failure in classification resolves to a conservative context (UNKNOWN scope), which
decide() turns into PASS with pass_reason "unclear_news".
"""
import datetime as dt
import hashlib
import json
import os
import re

import skills as SK

SHOCK_WORDS = re.compile(
    r"tariff|halt|circuit|war|missile|strike|attack|emergency|shutdown|default|downgrade|"
    r"guidance|earnings|sec |fraud|hack|outage|bankrupt|sanction|invasion|nuclear|"
    r"supreme court|debt ceiling|unscheduled|surprise", re.I)

SYSTEM_PROMPT = """You are the news-veto classifier for a paper-only options agent. The strategy is fixed by code: short-dated defined-risk iron condors on American-style ETFs the selector chose (Alpaca has no SPX/XSP/VIX options). Canon skills/options-core-patterns is enforced in Python (assignment plan, VIX as regime not a trade, defined-risk wings). You do not trade, size, price, or manage risk. Code has already enforced defined risk, size, credit/width, liquidity, quote freshness, IV/RV richness, the FOMC blackout, VIX-regime gate, position caps and every exit. Never recalculate, challenge or modify any of that.

TASK: read the headlines and classify the UNSCHEDULED jump risk inside the candidate's DTE. Output JSON only.

catalyst_scope: which underlyings the jump risk touches.
  NONE      no credible jump catalyst (this is the normal answer on a quiet tape)
  QQQ_ONLY  a mega-cap or semiconductor single-name shock large enough to gap the Nasdaq (QQQ/XLK/SMH cluster)
  SPY_ONLY  a shock concentrated outside tech (financials, energy, health policy — SPY/IWM cluster)
  BOTH      broad or systemic: geopolitics, sudden policy/tariff shock, unscheduled central-bank
            action, halt/circuit-breaker talk, sovereign/debt-ceiling/shutdown surprise
  UNKNOWN   headlines describe something material but you cannot tell scope or timing
jump_severity: expected index gap if the catalyst resolves inside the DTE.
  NONE / MINOR (under ~1%) / MAJOR (~1.5% or more, i.e. able to reach a 15-delta short strike)
news_clarity: CLEAR if the headlines are consistent; UNCLEAR if they conflict or are unresolved rumours.
catalyst: one sentence naming the event, or null.
confidence: 0-1 in this classification (diagnostic only; never affects size).

NEVER treat as a catalyst: high implied vol or VIX (that is why the trade exists; VIX is regime context, not a trigger); the tape looking
extended, bullish or bearish (the structure is delta-neutral; drift is not a jump); a scheduled macro
release such as FOMC, CPI or NFP (handled in code); generic, promotional, stale or unrelated items.
Headlines that are only noise => scope NONE, severity NONE, clarity CLEAR. Reserve UNKNOWN for material
but ambiguous news, not for the absence of news."""

CLASSIFY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["catalyst_scope", "jump_severity", "news_clarity", "catalyst", "confidence"],
    "properties": {
        "catalyst_scope": {"type": "string", "enum": ["NONE", "QQQ_ONLY", "SPY_ONLY", "BOTH", "UNKNOWN"]},
        "jump_severity": {"type": "string", "enum": ["NONE", "MINOR", "MAJOR"]},
        "news_clarity": {"type": "string", "enum": ["CLEAR", "UNCLEAR"]},
        "catalyst": {"type": ["string", "null"], "maxLength": 240},
        # No minimum/maximum: Anthropic structured output 400s on number bounds
        # ("For 'number' type, properties maximum, minimum are not supported").
        "confidence": {"type": "number"},
    },
}

QUIET = {"catalyst_scope": "NONE", "jump_severity": "NONE", "news_clarity": "CLEAR",
         "catalyst": None, "confidence": 1.0, "source": "no_headlines"}
FAILED = {"catalyst_scope": "UNKNOWN", "jump_severity": "MAJOR", "news_clarity": "UNCLEAR",
          "catalyst": None, "confidence": 0.0, "source": "classifier_failed"}


# ---------------------------------------------------------------- headlines

def filter_headlines(news_items, symbols=("SPY", "QQQ"), max_age_h=18, max_items=8, now=None):
    """Newest first, deduplicated, symbol-tagged or shock-keyworded, recent, truncated."""
    now = now or dt.datetime.now(dt.timezone.utc)
    out, seen = [], set()
    for n in sorted(news_items or [], key=lambda n: n.get('created_at') or '', reverse=True):
        title = (n.get('headline') or '').strip()
        if not title:
            continue
        key = re.sub(r'\W+', ' ', title.lower())[:80]
        if key in seen:
            continue
        ts = n.get('created_at') or ''
        try:
            age_h = (now - dt.datetime.fromisoformat(ts.replace('Z', '+00:00'))).total_seconds() / 3600
        except ValueError:
            age_h = 0.0
        if age_h > max_age_h:
            continue
        tags = [s for s in (n.get('symbols') or []) if s in symbols]
        if not tags and not SHOCK_WORDS.search(title):
            continue
        seen.add(key)
        out.append({'t': ts[11:16] if len(ts) >= 16 else '', 'tags': tags or ['-'], 'title': title[:140]})
        if len(out) >= max_items:
            break
    return out


def context_hash(headlines):
    return hashlib.sha1('\n'.join(h['title'] for h in headlines).encode()).hexdigest()[:12]


def render_payload(headlines, candidates, as_of):
    lines = [f"as_of: {as_of}", "candidates:"]
    for c in candidates:
        if c.get('structure') == 'condor':
            lines.append(f"- {c['underlying']} dte {c['dte']} short {c['put_short_strike']:.0f}P/{c['call_short_strike']:.0f}C")
        else:
            lines.append(f"- {c['underlying']} dte {c['dte']} short {c['short_strike']:.0f}{c['right']}")
    lines.append("headlines (newest first):")
    for h in headlines:
        lines.append(f"- [{h['t']}] [{','.join(h['tags'])}] {h['title']}")
    lines.append("Classify.")
    return "\n".join(lines)


# ---------------------------------------------------------------- classify

def classify_context(headlines, candidates, as_of, rules, cache, post_fn):
    """
    LLM classification of the filtered headlines, cached by content hash for the session.
    `post_fn(payload_json) -> (ok, body_dict_or_text)` isolates transport for testing.
    Returns the classification dict plus 'source' in {cache, llm, no_headlines, classifier_failed}.
    """
    if not headlines:
        return dict(QUIET)
    key = context_hash(headlines)
    if key in cache:
        return dict(cache[key], source='cache')
    llm = rules['llm']
    body = {
        "model": llm['model'],
        "max_tokens": llm.get('max_tokens_classify', 200),
        "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        "output_config": {"format": {"type": "json_schema", "schema": CLASSIFY_SCHEMA},
                          "effort": llm.get('effort', 'low')},
        "messages": [{"role": "user", "content": render_payload(headlines, candidates, as_of)}],
    }
    ok, resp = post_fn(body)
    if not ok or not isinstance(resp, dict):
        return dict(FAILED, raw=str(resp)[:300])
    if resp.get('stop_reason') == 'refusal':
        return dict(FAILED, raw='refusal')
    raw = "".join(b.get('text', '') for b in resp.get('content', []) if b.get('type') == 'text')
    try:
        ctx = json.loads(raw)
        for k in CLASSIFY_SCHEMA['required']:
            if k not in ctx:
                raise ValueError(k)
        if ctx['catalyst_scope'] not in CLASSIFY_SCHEMA['properties']['catalyst_scope']['enum'] or \
           ctx['jump_severity'] not in CLASSIFY_SCHEMA['properties']['jump_severity']['enum'] or \
           ctx['news_clarity'] not in CLASSIFY_SCHEMA['properties']['news_clarity']['enum']:
            raise ValueError('enum')
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        return dict(FAILED, raw=raw[:300], error=str(e))
    try:
        ctx['confidence'] = max(0.0, min(1.0, float(ctx.get('confidence') or 0.0)))
    except (TypeError, ValueError):
        ctx['confidence'] = 0.0
    ctx['source'] = 'llm'
    ctx['raw'] = raw[:600]
    if len(cache) > 64:          # a long-running process sees a few dozen distinct tapes a day
        cache.clear()
    cache[key] = {k: v for k, v in ctx.items() if k != 'source'}
    return ctx


# ---------------------------------------------------------------- decide

def decide(underlying, ctx):
    """
    Deterministic veto. Returns (decision, pass_reason).
      UNCLEAR or UNKNOWN            -> PASS unclear_news   (uncertainty resolves to no trade)
      scope touches the underlying  -> PASS catalyst   only if severity is MAJOR; MINOR is logged, allowed
      otherwise                     -> ALLOW              (a quiet tape is the normal, tradable state)
    """
    if ctx['news_clarity'] == 'UNCLEAR' or ctx['catalyst_scope'] == 'UNKNOWN':
        return 'PASS', 'unclear_news'
    scope = ctx['catalyst_scope']
    touched = SK.scope_touches(underlying, scope)
    if touched and ctx['jump_severity'] == 'MAJOR':
        return 'PASS', 'catalyst'
    return 'ALLOW', None


def apply_veto(candidates, ctx):
    """Split candidates into (allowed, vetoed) with per-candidate reasons, preserving code order."""
    allowed, vetoed = [], []
    for c in candidates:
        d, why = decide(c['underlying'], ctx)
        if d == 'ALLOW':
            allowed.append(c)
        else:
            vetoed.append({'candidate_id': c['id'], 'underlying': c['underlying'], 'pass_reason': why,
                           'catalyst': ctx.get('catalyst')})
    return allowed, vetoed


if __name__ == "__main__":
    # Self-check: the quiet tape must ALLOW (the v2_2 bug), scoping works, uncertainty passes.
    quiet = dict(QUIET)
    assert decide('SPY', quiet) == ('ALLOW', None) and decide('QQQ', quiet) == ('ALLOW', None)
    nvda = {'catalyst_scope': 'QQQ_ONLY', 'jump_severity': 'MAJOR', 'news_clarity': 'CLEAR'}
    assert decide('QQQ', nvda) == ('PASS', 'catalyst') and decide('SPY', nvda) == ('ALLOW', None)
    assert decide('SMH', nvda) == ('PASS', 'catalyst') and decide('XLV', nvda) == ('ALLOW', None)
    minor = dict(nvda, jump_severity='MINOR')
    assert decide('QQQ', minor) == ('ALLOW', None)
    both = {'catalyst_scope': 'BOTH', 'jump_severity': 'MAJOR', 'news_clarity': 'CLEAR'}
    assert decide('SPY', both) == ('PASS', 'catalyst')
    unclear = {'catalyst_scope': 'NONE', 'jump_severity': 'NONE', 'news_clarity': 'UNCLEAR'}
    assert decide('SPY', unclear) == ('PASS', 'unclear_news')
    assert decide('SPY', FAILED) == ('PASS', 'unclear_news')
    now = dt.datetime.now(dt.timezone.utc)
    items = [{'headline': 'NVDA cuts guidance; NDX futures -2.6%', 'symbols': ['QQQ'], 'created_at': now.isoformat()},
             {'headline': 'NVDA cuts guidance; NDX futures -2.6%', 'symbols': ['QQQ'], 'created_at': now.isoformat()},
             {'headline': '3 dividend ETFs to watch', 'symbols': ['XYZ'], 'created_at': now.isoformat()},
             {'headline': 'Old tariff story', 'symbols': ['SPY'], 'created_at': (now - dt.timedelta(hours=30)).isoformat()}]
    h = filter_headlines(items)
    assert len(h) == 1 and h[0]['tags'] == ['QQQ'], h
    cache = {}
    calls = []
    def fake_post(body):
        calls.append(body)
        return True, {'content': [{'type': 'text', 'text': json.dumps(
            {'catalyst_scope': 'QQQ_ONLY', 'jump_severity': 'MAJOR', 'news_clarity': 'CLEAR',
             'catalyst': 'NVDA guidance cut', 'confidence': 0.85})}]}
    rules = {'llm': {'model': 'x', 'effort': 'low'}}
    cands = [{'id': 'SPY-1', 'underlying': 'SPY', 'structure': 'condor', 'dte': 1, 'put_short_strike': 754, 'call_short_strike': 777},
             {'id': 'QQQ-1', 'underlying': 'QQQ', 'structure': 'condor', 'dte': 1, 'put_short_strike': 700, 'call_short_strike': 724}]
    ctx = classify_context(h, cands, '2026-09-01T10:05 ET', rules, cache, fake_post)
    ctx2 = classify_context(h, cands, '2026-09-01T10:20 ET', rules, cache, fake_post)
    assert ctx['source'] == 'llm' and ctx2['source'] == 'cache' and len(calls) == 1
    assert calls[0]['system'][0]['cache_control']['type'] == 'ephemeral'
    assert 'iv_rv' not in calls[0]['messages'][0]['content'] and 'max_loss' not in calls[0]['messages'][0]['content']
    allowed, vetoed = apply_veto(cands, ctx)
    assert [c['id'] for c in allowed] == ['SPY-1'] and vetoed[0]['candidate_id'] == 'QQQ-1'
    assert 'minimum' not in CLASSIFY_SCHEMA['properties']['confidence']
    assert 'maximum' not in CLASSIFY_SCHEMA['properties']['confidence']
    assert classify_context([], cands, 'x', rules, cache, fake_post)['source'] == 'no_headlines'
    bad = classify_context(h + [{'t': '', 'tags': ['SPY'], 'title': 'y'}], cands, 'x', rules, {}, lambda b: (True, {'content': [{'type': 'text', 'text': '{oops'}]}))
    assert bad['source'] == 'classifier_failed' and decide('SPY', bad) == ('PASS', 'unclear_news')
    print("veto.py self-check OK")
