"""Daily scan: regime -> shortlist -> candidates per ticker -> rank -> budget allocation -> order intents.

Runs in three modes (cfg/CLI):  RESEARCH (ledger only), SHADOW (ledger + simulated intents), PAPER (submit mleg
orders to the Alpaca paper endpoint).  Live is out of scope for this version by design (authorization gateway).
"""
from __future__ import annotations
import json, uuid, math, datetime as dt
from .db import now_utc
from .regime import compute_regime, skew_median
from .preselect import Funnel
from .structures import Builder, rank, S4, LONG_VOL


def _chain_rows(con, symbol: str, expiration: dt.date, as_of: dt.date):
    rows = con.execute("""
        WITH last AS (SELECT max(captured_at) m FROM pit_chain_snapshots WHERE underlying=? AND captured_at::DATE<=?)
        SELECT cp, strike, bid, ask, iv, delta, vega, gamma, spot, theta FROM pit_chain_snapshots, last
        WHERE underlying=? AND expiration=? AND captured_at=last.m""", [symbol, as_of, symbol, expiration]).fetchall()
    src = "pit"
    if not rows and symbol == "SPY":
        rows = con.execute("""SELECT substr(key,10,1), cast(substr(key,11,8) AS DOUBLE)/1000, latestquote_bp, latestquote_ap,
                              impliedvolatility, greeks_delta, greeks_vega, greeks_gamma, NULL, greeks_theta FROM option_chain_SPY
                              WHERE strptime(substr(key,4,6),'%y%m%d')::DATE = ?""", [expiration]).fetchall()
        src = "option_chain_SPY(sample)"
    return [dict(cp=r[0], strike=r[1], bid=r[2], ask=r[3], iv=r[4], delta=r[5], vega=r[6], gamma=r[7], theta=r[9]) for r in rows], src


def _exdiv_in_life(con, symbol: str, as_of: dt.date, expiration: dt.date) -> bool:
    n = con.execute("SELECT count(*) FROM pit_dividends_forward WHERE symbol=? AND ex_date BETWEEN ? AND ?",
                    [symbol, as_of, expiration]).fetchone()[0]
    if n:
        return True
    # fallback: project last year's ex-dates forward (flagged as projection, conservative = assume ex-div if within +-5d)
    rows = con.execute("SELECT ex_date FROM corporate_actions WHERE symbol=? AND action_type='cash_dividends'", [symbol]).fetchall()
    for (d,) in rows:
        try:
            proj = d.replace(year=d.year + 1)
        except ValueError:
            continue
        if as_of - dt.timedelta(days=5) <= proj <= expiration + dt.timedelta(days=5):
            return True
    return False


def budgets(cfg: dict, open_positions: list[dict]) -> dict:
    r, nav = cfg["risk"], cfg["nav_usd"]
    used_book = sum(p.get("max_loss_total", 0) for p in open_positions)
    used_by_t = {}
    for p in open_positions:
        used_by_t[p["underlying"]] = used_by_t.get(p["underlying"], 0) + p.get("max_loss_total", 0)
    return {"pos": r["pos_nav_pct"] * nav, "book_left": r["book_nav_pct"] * nav - used_book,
            "ticker_left": {t: r["ticker_nav_pct"] * nav - u for t, u in used_by_t.items()},
            "ticker_cap": r["ticker_nav_pct"] * nav, "n_open": len(open_positions),
            "n_by_t": {t: sum(1 for p in open_positions if p["underlying"] == t) for t in used_by_t}}


def haircuts_for(cfg: dict, s: dict, reg) -> tuple[float, list[str]]:
    """v2.1: economic uncertainty -> size multiplier (logged), never a veto.  Hard blockers stay in the gates."""
    h = cfg.get("haircuts", {})
    mult, why = 1.0, []
    if s.get("haircut_quote_size"):
        mult *= h.get("quote_size_below_min", 1.0); why.append("quote_size_below_min")
    r2 = s.get("har_r2_is")
    if r2 is not None and not math.isnan(r2) and r2 < h.get("har_r2_below", {}).get("threshold", -1):
        mult *= h["har_r2_below"]["mult"]; why.append(f"har_r2={r2:.3f}")
    rvf, rvp = s.get("rv_f"), s.get("rv_persist")
    if rvf and rvp and abs(rvf - rvp) / rvf > h.get("forecast_disagreement", {}).get("threshold", 9):
        mult *= h["forecast_disagreement"]["mult"]; why.append("forecast_disagreement")
    if (s.get("bars_repaired") or 0) >= h.get("bars_repaired", {}).get("threshold", 1e9):
        mult *= h["bars_repaired"]["mult"]; why.append("bars_repaired")
    if reg.state == "POST_SHOCK":
        mult *= h.get("post_shock", 0.5); why.append("post_shock")
    return mult, why


def book_scores(con, cfg: dict) -> dict:
    """Realized/predicted EV ratio per structure family over the last 20 closed trades -> multiplier in [floor, 1]."""
    floor = cfg.get("haircuts", {}).get("book_score_floor", 0.5)
    try:
        rows = con.execute("""
            SELECT p.structure, sum(p.pnl), sum(d.ev_exec * p.qty) FROM positions p JOIN decision_ledger d USING (decision_id)
            WHERE p.status='CLOSED' GROUP BY 1""").fetchall()
    except Exception:
        return {}
    out = {}
    for fam, real, pred in rows:
        if pred and pred > 0:
            out[fam] = float(min(1.0, max(floor, real / pred)))
    return out


def run_scan(con, cfg: dict, as_of: dt.date, stage: str = "STAGE_0", open_positions: list[dict] | None = None,
             shortlist: list[dict] | None = None, mode: str = "RESEARCH", regime_override: str | None = None) -> dict:
    open_positions = open_positions or []
    reg = compute_regime(con, cfg, as_of)
    if regime_override:
        # RESEARCH-ONLY: exercise the pipeline without VIX data; never valid for SHADOW/PAPER
        from .regime import Regime
        assert mode == "RESEARCH", "regime override is allowed in RESEARCH mode only"
        reg = Regime(as_of, regime_override, regime_override != "RED", float("nan"), float("nan"), 3,
                     cfg["regime"]["k_by_quintile"][3], float("nan"), True, [], ["SYNTHETIC OVERRIDE — research only"],
                     cfg["regime"]["k_long"], regime_override == "POST_SHOCK")
    out = {"as_of": as_of.isoformat(), "regime": reg.dict(), "intents": [], "rejections": [], "mode": mode,
           "opportunity_cost": [], "gate_diagnostics": {}}
    if shortlist is None:
        shortlist = Funnel(con, cfg, as_of).run()["shortlist"]
    out["shortlist"] = [{k: s.get(k) for k in ("symbol", "expiration", "dte", "iv_over_rvf", "short_vol_score",
                                                "long_convex_score", "branch_hint", "l3_pass")} for s in shortlist]
    if reg.state in ("RED", "DATA_BLOCKED"):
        out["decision"] = f"NO_TRADE ({reg.state}: {'; '.join(reg.reasons)})"
        _ledger_regime_block(con, cfg, as_of, reg)
        return out
    scores = book_scores(con, cfg)
    oc = cfg.get("objective", {})
    # ---------------- 1. build every candidate across shortlist x window expiries (registered grid)
    pool = []   # (candidate, shortlist_row, expiration, src, haircut_mult, haircut_reasons)
    for s in shortlist:
        sym = s["symbol"]
        exps = s.get("window_expiries") or [(s["expiration"], s["dte"])]
        hc_mult, hc_why = haircuts_for(cfg, s, reg)
        if reg.state == "POST_SHOCK" and not (s.get("rv5") and s.get("rv_yz20") and s["rv5"] < s["rv_yz20"]):
            out["rejections"].append({"symbol": sym, "reason": "POST_SHOCK requires rv5 < rv20 (deceleration) on the underlying"}); continue
        for exp, dte in exps:
            if isinstance(exp, str):
                exp = dt.date.fromisoformat(exp)
            chain, src = _chain_rows(con, sym, exp, as_of)
            if not chain:
                out["rejections"].append({"symbol": sym, "expiration": exp.isoformat(), "reason": "no chain rows"}); continue
            bld = Builder(cfg, sym, exp, dte, chain, s["spot"], s["forward"], s["rv_f"], s["mae_up_p50"], s["mae_dn_p50"],
                          s.get("skew_25d") or float("nan"), _exdiv_in_life(con, sym, as_of, exp), reg.k,
                          realized_move_frac=s.get("realized_move_frac_5d", 0.0))
            bld.k_long = reg.k_long
            vdir = bld.shadow_direction_scores()
            cands = bld.build()
            for c in cands:
                if c.structure in LONG_VOL and not reg.convex_allowed:
                    c.passed, c.reason = False, "long-vol branch closed by regime (RED)"
                elif c.structure not in LONG_VOL and c.structure != "NO_TRADE" and reg.state not in ("GREEN", "POST_SHOCK"):
                    c.passed, c.reason = False, f"short-premium branch closed: regime {reg.state}"
            gd = out["gate_diagnostics"].setdefault(sym, {"candidates": 0, "passed": 0, "fails": {}, "iv_atm": bld.iv_atm,
                                                          "rv_f": bld.rv_f, "k": reg.k, "k_long": reg.k_long, "haircut": hc_mult, "haircut_why": hc_why,
                                                          "vol_direction": vdir})
            for c in cands:
                if c.legs:
                    gd["candidates"] += 1
                for g, v in c.gates.items():
                    if not v:
                        gd["fails"][f"{c.structure}:{g}"] = gd["fails"].get(f"{c.structure}:{g}", 0) + 1
                _ledger(con, cfg, as_of, reg, c, s, 0, "REJECT" if not c.passed else "CANDIDATE", c.reason)
                if c.passed:
                    gd["passed"] += 1
                    pool.append((c, s, exp, src, hc_mult, hc_why))
    if not pool:
        out["decision"] = "NO_TRADE (no candidate survived gates)"
        return out
    # ---------------- 2. global competition: best candidate per (symbol) first, then across symbols by utility
    best_by_sym = {}
    for item in pool:
        c, s = item[0], item[1]
        if s["symbol"] not in best_by_sym or c.utility > best_by_sym[s["symbol"]][0].utility:
            best_by_sym[s["symbol"]] = item
    ranked_syms = sorted(best_by_sym.values(), key=lambda it: it[0].utility, reverse=True)
    # ---------------- 3. budgeted allocation with cluster cap and opportunity-cost ledger
    b = budgets(cfg, open_positions)
    cluster_used = sum(p.get("max_loss_total", 0) for p in open_positions if p.get("index_clone"))
    cluster_cap = cfg["risk"].get("index_cluster_nav_pct", 1.0) * cfg["nav_usd"]
    stage_mult = cfg["risk"]["size_stage_mult"][stage]
    for i, (best, s, exp, src, hc_mult, hc_why) in enumerate(ranked_syms):
        sym = s["symbol"]
        next_best = ranked_syms[i + 1][0] if i + 1 < len(ranked_syms) else None
        v_miss = (next_best.utility * best.margin * best.hold_days) if (next_best and oc.get("next_best_opportunity_cost")) else 0.0
        t_left = b["ticker_left"].get(sym, b["ticker_cap"])
        is_clone = (s.get("beta_spy") or 0) > 0.85 and (s.get("corr_spy") or 0) > 0.9
        if b["n_open"] >= cfg["risk"]["max_open_positions"] or b["n_by_t"].get(sym, 0) >= cfg["risk"]["max_per_ticker"]:
            out["opportunity_cost"].append({"symbol": sym, "structure": best.structure, "ev_forgone_per_spread": round(best.ev_exec, 1),
                                            "reason": "position-count cap"}); continue
        budget = min(b["pos"], t_left, b["book_left"]) * stage_mult * hc_mult * scores.get(best.structure, 1.0)
        if is_clone:
            budget = min(budget, cluster_cap - cluster_used)
        qty = int(math.floor(budget / best.max_loss)) if best.max_loss > 0 else 0
        if qty < 1:
            out["opportunity_cost"].append({"symbol": sym, "structure": best.structure, "ev_forgone_per_spread": round(best.ev_exec, 1),
                                            "reason": f"budget {budget:.0f} < max_loss {best.max_loss:.0f} (haircut {hc_mult}, {hc_why})"}); continue
        ev_total = best.ev_exec * qty
        bps_day = ev_total / cfg["nav_usd"] * 1e4 / best.hold_days
        if bps_day < oc.get("min_pnl_per_capital_day_bps_nav", 0.0):
            out["opportunity_cost"].append({"symbol": sym, "structure": best.structure, "ev_forgone_per_spread": round(best.ev_exec, 1),
                                            "reason": f"EV {bps_day:.3f} bps NAV/day below reservation {oc['min_pnl_per_capital_day_bps_nav']}"}); continue
        data_ok = s.get("l3_pass", False) and src == "pit"
        decision = best.structure if data_ok else "SHADOW_ONLY (quotes not live)"
        did = _ledger(con, cfg, as_of, reg, best, s, qty, decision, "selected")
        plan = dict(best.exec_plan); plan["v_miss"] = round(v_miss, 1)
        intent = {"decision_id": did, "symbol": sym, "structure": best.structure, "expiration": exp.isoformat(), "dte": best.dte,
                  "qty": qty, "limit_credit": plan["start"]["limit_credit"], "floor_credit": plan["floor_credit"],
                  "credit_mid": round(best.credit_mid, 2), "credit_exec": round(best.credit_exec, 2),
                  "max_loss_total": best.max_loss * qty, "ev_exec_total": round(ev_total, 0),
                  "hold_days": round(best.hold_days, 1), "pnl_per_capital_day": round(best.pnl_per_capital_day, 5),
                  "ev_bps_nav_per_day": round(bps_day, 3), "edge_on_margin": round(best.edge_on_margin, 4),
                  "edge_cost_ratio": round(best.edge_cost_ratio, 2), "p_profit": round(best.p_profit, 3),
                  "p_touch": round(best.p_touch, 3), "haircut": hc_mult, "haircut_why": hc_why,
                  "book_score": scores.get(best.structure, 1.0), "exec_plan": plan,
                  "legs": [dict(symbol=l.symbol, side=l.side, strike=l.strike, cp=l.cp, bid=l.bid, ask=l.ask, delta=l.delta) for l in best.legs],
                  "data_ok": data_ok, "source": src, "index_clone": is_clone,
                  "alternatives": sorted([(c.structure, c.expiration.isoformat(), round(c.utility, 5)) for c, s2, *_ in pool
                                          if s2["symbol"] == sym and c is not best], key=lambda x: -x[2])[:4]}
        out["intents"].append(intent)
        b["book_left"] -= intent["max_loss_total"]; b["ticker_left"][sym] = t_left - intent["max_loss_total"]
        b["n_open"] += 1; b["n_by_t"][sym] = b["n_by_t"].get(sym, 0) + 1
        if is_clone:
            cluster_used += intent["max_loss_total"]
    out["decision"] = f"{len(out['intents'])} intents" if out["intents"] else "NO_TRADE (no candidate cleared allocation)"
    return out


def _ledger(con, cfg, as_of, reg, c, s, qty, decision, reason) -> str:
    did = str(uuid.uuid4())
    con.execute("INSERT INTO decision_ledger VALUES (" + ",".join("?" * 25) + ")", [
        did, now_utc(), as_of, cfg["version"], reg.state, c.underlying, c.structure,
        json.dumps([dict(symbol=l.symbol, side=l.side, strike=l.strike, cp=l.cp, bid=l.bid, ask=l.ask) for l in c.legs]),
        c.credit_exec, c.credit_mid, c.width, c.max_loss, c.margin, c.ev_exec, c.ev_pess, c.edge_on_margin,
        c.edge_cost_ratio, c.rt_cost, s.get("iv_atm"), s.get("rv_f"), reg.k, qty, decision, reason,
        json.dumps({"p_profit": c.p_profit, "p_touch": c.p_touch, "es99": c.es99, "delta": c.delta, "vega": c.vega,
                    "gates": c.gates})])
    return did


def _ledger_regime_block(con, cfg, as_of, reg):
    con.execute("INSERT INTO decision_ledger (decision_id, decided_at, as_of, config_version, regime, underlying, structure, "
                "qty, decision, reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [str(uuid.uuid4()), now_utc(), as_of, cfg["version"], reg.state, "*", "NO_TRADE", 0, "NO_TRADE",
                 "; ".join(reg.reasons)])


def submit_intents(api, cfg: dict, intents: list[dict]) -> list[dict]:
    """PAPER mode only: mleg limit at mid credit; the walk-down is executed by manage.work_orders()."""
    out = []
    for it in intents:
        if not it["data_ok"]:
            out.append({**it, "submitted": False, "why": "data not live"}); continue
        legs = [{"symbol": l["symbol"], "ratio_qty": "1", "side": "sell" if l["side"] == "SELL" else "buy",
                 "position_intent": "sell_to_open" if l["side"] == "SELL" else "buy_to_open"} for l in it["legs"]]
        # Alpaca mleg convention: credit orders carry a NEGATIVE limit price
        lp = -it["limit_credit"] if it["limit_credit"] > 0 else abs(it["limit_credit"])
        r = api.submit_mleg(legs, it["qty"], lp, client_order_id=f"usadapt-{it['decision_id'][:8]}")
        out.append({**it, "submitted": True, "order_id": r.get("id")})
    return out
