# Asset Selection Logic for Options Trading (US Equities, DTE ≤ 12)

Deterministic five-stage funnel. Each stage removes names for a reason that
kills a trade regardless of edge. Ranking happens only after eligibility;
IV is judged only at the trade stage. Output of each stage is the input of
the next.

```
Stage 0  Venue & style          universe.json      → tradable set
Stage 1  Expiry density         expiry_tier        → DTE≤12-compatible set
Stage 2  Executability proxies  penny/OI/strikes   → eligible set (~200)
Stage 3  Factor de-duplication  tags               → portfolio slots
Stage 4  OHLCV scoring          daily bars         → shortlist (top-N per branch)
Stage 5  IV gate (trade-time)   option chains      → trade / NO_TRADE
```

---

## Stage 0 — Venue & style gates (hard, binary)

- DROP if not tradable at the execution venue. On Alpaca this removes all
  European-style index products (SPX, XSP, VIX, DJX): they appear in the
  contracts feed but cannot be traded.
- Consequences, encode explicitly:
  - Index exposure is expressed via American-style, physically settled ETFs
    (SPY/QQQ/IWM). Assignment, ex-div and pin-risk management is therefore
    mandatory, not optional.
  - VIX options do not exist on this venue. VIX is a **regime input only**;
    the only vol-instrument is VXX (path-dependent ETP — never a hedge
    warehouse, short-horizon tactical only).

## Stage 1 — Expiry density (mandate compatibility)

Mandate: every position lives at DTE ≤ 12, so an expiry must exist inside
the window at all times.

- KEEP `expiry_tier ∈ {dailies, weeklies}` (13+, 7-12, 4-6 buckets).
- DROP monthly-only names (tier 2-3, ~75% of the universe): on most calendar
  days the nearest expiry falls outside 12 days → the mandate cannot be
  expressed.

## Stage 2 — Executability proxies (hard thresholds, from contracts data)

Executable edge is the only edge; these are the best proxies available
without quotes:

- `penny_contracts > 0` — CBOE penny program membership; strongest available
  proxy for tight spreads. Hard requirement.
- `total_oi ≥ OI_MIN` — liquidity floor. NOTE: collected OI is a **floor,
  not a census** (capped crawl) → use as a *rank*, never as an absolute.
  Defaults: `OI_MIN = 1,000,000` for CORE, `300,000` for EXTENDED.
- `strikes ≥ 40` — strike density; sparse grids force bad strike selection
  for spreads.

Bucketing:
- **CORE** = dailies + OI ≥ 1M + penny. (2026-08 result: SPY, QQQ, IWM,
  GLD, XLF, SMH — six names spanning six distinct vol factors: broad
  market, tech, small-cap, gold, financials, semis.)
- **EXTENDED** = weeklies + OI ≥ 300k + penny (~196 names).

## Stage 3 — Factor de-duplication (portfolio logic, not per-name)

Tag every survivor; one factor cluster = one portfolio slot:

- `crypto_proxy` (IBIT, ETHA, MSTR, COIN, miners, treasuries…): duplicates
  the crypto desk's factor — the slot is already occupied; exclude unless
  the trade is explicitly a cross-venue relative-value idea.
- `leveraged` (TQQQ/SQQQ/SOXL/…): derivatives with rebalancing drag, not
  independent underlyings; never scored as standalone.
- `vol_etp` (VXX): V5 rules — tactical, days-horizon, roll modeled.
- `stock` (single names): default to **event mode (S3)** — earnings gaps
  are scheduled tail risk; excluded from the VRP branch unless the position
  is explicitly an event trade.
- ETFs without a tag: eligible for both branches.

## Stage 4 — OHLCV scoring (ranking, not signals)

Compute per name on ≥ 2y of daily OHLCV (script: `asset_selector.py`).

Metrics (exact definitions):

| Metric | Definition | Role |
|---|---|---|
| RV21 | annualized √(mean over 21d of 0.5·Parkinson + 0.5·close-to-close daily variance); Parkinson = ln²(H/L)/(4·ln2) | premium must exist: too-low vol → premium < friction |
| pct_1y | percentile of current RV21 within trailing 252d | regime proxy; extremes are bad for selling (low = gamma trap, high = stress) |
| har_r2 | in-sample R² of HAR-lite: log fwd-5d mean variance ~ log(1d, 5d, 22d) averages | RV forecastability — the core of VRP suitability (the edge IS the forecast) |
| overnight_share | Var(ln(O/C₋₁)) / [Var(ln(O/C₋₁)) + Var(ln(C/O))] | share of risk arriving where hedging is impossible |
| gap_tail | p99(\|ln(O/C₋₁)\|) / σ_daily | jumpiness; feeds the MAE gate |
| mae10_p95 | p95 of worst adverse excursion over 10-day windows, both sides, via H/L | direct input to strike-distance checks for 7–12 DTE shorts |
| vol_of_vol | annualized σ of Δlog(RV21) | regime explosiveness: bad for short vol, fuel for long convexity |
| expansion_prob | P(RV21_fwd / RV21 > 1.5) over 21d horizon | frequency of calm→storm transitions (the gamma trap, quantified) |
| eff_ratio | median Kaufman ratio \|ΔP_20\| / Σ\|ΔP_1\| | chop feeds short gamma; trend feeds debit structures |
| earnings_sig | share of top-2% \|gaps\| spaced ~63±8 trading days apart | auto-detect event-mode names |
| dollar_vol | median(close × volume) | hedge-leg liquidity proxy |

Two scores (cross-sectional percentile ranks, weights in brackets):

```
ShortVolScore =
    .25·rank(har_r2)            # forecastable RV
  + .20·rank(−gap_tail)         # no jumps
  + .15·rank(−overnight_share)  # risk arrives while hedgeable
  + .10·rank(−vol_of_vol)       # stable regime
  + .10·rank(RV21)              # premium exists
  + .10·midness(pct_1y)         # mid-range percentile (1 − 2·|pct−0.5|)
  + .10·rank(dollar_vol)
  − .15·event_mode              # single names → S3, not VRP

LongConvexScore =
    .30·rank(−pct_1y)           # compressed
  + .25·rank(expansion_prob)    # history of expansions
  + .20·rank(vol_of_vol)
  + .15·rank(eff_ratio)         # regime delivers movement
  + .10·rank(dollar_vol)
```

Outputs: top-3 per branch = shortlist. Weekly recompute. A leadership change
is not a signal (scores are slow); a name falling out of eligibility
(gap_tail, regime) is an immediate stop for new positions in it.

## Stage 5 — IV gate (trade-time, per short-dte.md / options-vol-research)

OHLCV is IV-blind: it ranks where RV is forecastable and tails manageable,
not whether that is already paid for. Before any trade on a shortlisted name:

1. IV (structure's tenor/strike) vs **forecast** RV (HAR-class, horizon-
   matched, never trailing) — the pair of numbers must exist.
2. Regime gate: VIX percentile < ~70/1y; VX term structure in contango for
   short-vol (from VX futures data; VIX index series alone is insufficient).
3. Dollar vega yield ≥ 5–10× round-trip cost; spread ≤ ~10% of premium.
4. MAE gate: `mae10_p95` vs short-strike distance (breach by 15%+ →
   defined-risk only or debit).
5. Executable reprice with pessimistic exits; edge ≥ 3× round-trip cost,
   edge-on-margin ≥ ~2% investigate / < 1% pass.
6. Default outcome is NO_TRADE. The #1-ranked asset with cheap IV is
   NO_TRADE.

---

## Algorithm (reference pseudocode)

```
INPUT:  universe.json, OHLCV store (daily bars), params
OUTPUT: shortlist per branch + per-name eligibility state

# ---- eligibility (stages 0–3), recompute on each universe refresh ----
eligible = []
for u in universe.option_underlyings:
    if u.not_tradable_on_venue or u.european_style:        continue   # S0
    if u.expiry_tier not in {DAILIES, WEEKLIES}:           continue   # S1
    if u.penny_contracts == 0:                             continue   # S2
    if u.strikes < 40:                                     continue   # S2
    if u.expiry_tier == DAILIES and u.total_oi >= 1e6:  bucket = CORE
    elif u.total_oi >= 3e5:                             bucket = EXTENDED
    else:                                                  continue   # S2
    tag = factor_tag(u)                                               # S3
    eligible.append((u.symbol, bucket, tag, event_mode(u)))

# ---- scoring (stage 4), recompute weekly ----
rows = []
for (sym, bucket, tag, ev) in eligible:
    bars = ohlcv(sym, min_years=2);  if missing → skip
    m = metrics(bars)                       # table above
    rows.append(m + {bucket, tag, ev})
scores = cross_sectional_scores(rows)       # ShortVolScore, LongConvexScore

shortlist_sv = top3(scores, ShortVolScore,  where tag ∉ {leveraged},
                    respecting one-slot-per-factor-cluster)
shortlist_lc = top3(scores, LongConvexScore, same constraints)

# ---- trade time (stage 5), per candidate ----
for sym in shortlist:
    chain = live_chain(sym)
    if not iv_gate(chain, har_forecast(sym)):        → NO_TRADE
    if not regime_gate(vix_series, vx_curve):        → NO_TRADE
    if not mae_gate(m.mae10_p95, strikes):           → NO_TRADE
    if not executable_reprice(structure, chain):     → NO_TRADE
    → structure selection per short-dte.md (S1–S4; S5 excluded on Alpaca)
```

## Integrity notes (do not silently drop)

- Selecting top-N from ~200 candidates **is multiple testing**: the
  shortlist is a set of hypotheses to falsify, not a proven ordering.
- `har_r2` is in-sample — valid for ranking, forbidden as "proven
  forecastability". The finalist gets a full HAR with purged walk-forward.
- Chain-level liquidity (per-strike OI, live spreads) is verified by fact at
  Stage 5; dollar_vol and penny membership are necessary, not sufficient.
- OI figures are floors (capped crawl) — rank, never threshold-as-truth
  beyond the coarse eligibility cut.
```
