# Short-DTE Module: Structures Under DTE ≤ 12 (US Market + VIX)

Read for any position design with tenor up to 12 days. The ranking below is
a set of priors ordered by (premium concentration at the front ×
survivability at executable prices × compliance with options-vol-research
gates). Every structure carries a falsification criterion — without passing
the gates the default is NO_TRADE.

## What the DTE ≤ 12 constraint changes

- **Vega ≈ dead; gamma and theta ARE the position.** Every trade = a bet
  "RV of the next 7–12 days vs front IV." Views on the IV level (vega
  trades), standard 30/60 calendars, standing VIX-call hedges are out of
  scope: they need tenor.
- **Forecast horizon = structure horizon.** The RV forecast is built for
  7–12 days (HAR-class models work precisely there); trailing RV as an
  anchor is forbidden — at low-vol percentiles it is the gamma trap.
- **The entire hold lives in the peak-gamma zone.** The "30 DTE, take
  profit at DTE ≤ 14" roll matrix degenerates: the position is born inside
  the danger zone. Consequences: faster profit-taking (25–50% of max),
  mandatory time stop, never sit to expiration (pin + gamma), re-entry
  instead of sitting through pain.
- **Entry errors have no time to amortize.** The MAE/jump gate becomes the
  main one: if the historical max adverse excursion over 7–12 days exceeds
  the distance to the short strike by 15%+ — defined-risk or debit only.
- **The event calendar IS the front map.** CPI, FOMC, NFP, earnings almost
  always land inside a 12-day window; every position is classified: VRP
  regime or event regime (different trades, event margin ×1.5).

## Where premium concentrates at the front (sources, not guarantees)

1. **Front VRP.** Structural demand for short-dated protection and the
   supply/demand tilt in near series; harvestable only in a calm regime
   (gate: VIX percentile < ~70 over 1y, VX contango).
2. **Steep SPX front put skew.** The crash price is richest in near series
   — sold only with a closed tail.
3. **Event IV (earnings/macro).** Inflated front IV that collapses after
   the event — a separate regime, not VRP.
4. **Front VX convergence to spot.** Accelerates in the final days, BUT:
   roll-down is already in the future's price (V1) — by itself it is not
   edge; edge exists only if the convergence path is mispriced.

## Structure ranking (descending prior)

### S1. SPX/XSP (or SPY/QQQ where index options are unavailable), 7–12 DTE: short strangle / ATM straddle, 2 legs
**Bet:** RV(7–12d) < front IV; the VRP core.
**Why above condors:** the executable-falsification lesson — 4-leg
structures die on friction, 2-leg ATM survives. Fewer legs + ATM liquidity
+ a hold long enough to matter.
**Gates:** regime (percentile, contango), IV ≥ HAR forecast with a buffer
(at low percentiles require ~1.4–1.5× vs trailing), dollar vega yield ≥
5–10× round-trip cost, MAE check on strike distance, event calendar clean
or the trade reclassified as an event trade.
**Management:** delta stop (−50% of premium → cut half, −100% → close),
profit-take at 25–50% of max or time stop, corridor hedge (not to zero), no
averaging down. Naked only in a clean event window; otherwise wings (see
S2 logic), accepting the loss of part of the edge as the price of margin.
**Falsification:** executable reprice with pessimistic exits; attribution —
the Γ+ν share of PnL ≥ 25%, otherwise DELTA_ONLY → this is not a vol trade.

### S2. Index put credit spread, 7–12 DTE, 2 legs (SPX; on Alpaca — SPY/QQQ with P12 caveats)
**Bet:** the crash price on the near put wing is too high vs the RV
forecast and the shape of the distribution.
**Why a 2-leg spread, not BWB/condor:** the same leg-count argument; a BWB
(3 legs) and an iron condor (4) are admissible only if the combo book
demonstrably fills better than the sum of legs — verified by repricing, not
assumed.
**Gates:** RR/fly metrics confirm the wing is rich (compare wings, not the
IV level); the regime gate as in S1; the spread width survives MAE.
**Specific risk:** doubled factor leverage against the seller in a crash
(delta+vega) — compensated by defined risk; compute stress PnL under
−20% spot / +40 vp IV.
**Falsification:** if executable edge/margin < ~1% — PASS; compare with S1
on edge-per-margin: if close (≈1.3% vs 1.1%) → choose by hedging
preference, not by edge.

### S3. Event regime: earnings/macro inside 12 days (a separate regime, NOT VRP)
**Bet:** the front implied move vs the ticker's historical realized moves
on the event; a managed surprise.
**Structures in order of preference:**
- intra-window calendar: sell the event series, buy the next one — BOTH
  legs ≤ 12 DTE is feasible (e.g., 2d/9d). Captures the crush with defined
  risk; 4 legs are unnecessary — this is 2 legs.
- a strangle one day before the event with a mandatory delta hedge into the
  close.
**Gates:** event margin ×1.5 in ROM, the MAE gate is mandatory (for single
stocks — the worst sector earnings gap, not the ticker's own history),
implied vs realized move over the last 8–12 events of the ticker.
**Falsification:** the executable reprice is critical — the calendar was a
live example of death by friction (+920 paper → −5,776 executable); if
intra-series spreads eat the edge — NO_TRADE.

### S4. 0–2 DTE: intraday short ATM straddle, 2 legs
**Bet:** intraday IV vs intraday RV; a pure gamma-theta duel.
**Lower prior:** edge capacity is small, friction dominates, theta here is
compensation for gamma, not income. 4-leg "iron fly at the open" is
structurally against the leg-count lesson.
**Gates:** edge ≥ 3× round trip — most 0DTE screens die on this gate; size
from gamma (movement to the stop), not from premium; macro days excluded or
moved to S3.
**Falsification:** pessimistic bid-exit model; slippage sensitivity at
10/25/50/100% of the half-spread — the edge must survive at 50%.

### S5. VIX front ≤ 12 DTE: put spreads on normalization, 2 legs
**Venue gate:** on venues without index options (Alpaca) S5 does not
execute — VIX options are untradable; VIX remains a regime input, and vol
exposure is possible only through VXX (see V5: path-dependent, not an
equivalent). In that case S5 is removed from the ranking entirely.
**Bet:** only in backwardation — the market underprices the speed of
normalization; expressed with a bought VIX put spread (bounded risk vs
catching the knife with a short VX).
**Anti-pattern:** "buy VIX puts to harvest roll-down in contango" — the
roll is already in the future's price (V1); that is not edge, it is paying
fair value for convergence.
**Gates:** all greeks/moneyness from the front VX, not from spot; SOQ risk
of the final settlement; event dates before the VX expiry accounted for.
**Falsification:** benchmark against a plain short VX with a hard stop —
the option version must win after friction, otherwise it is expensive
delta.

## What is NOT on the list, and why

- **Ratio spreads** — an open tail at any proportion >1; 12 days do not
  change the infinity of the risk.
- **Naked short single-stock premium through earnings** — a calendar-
  scheduled tail.
- **"Weekend theta"** — priced in ahead of time; not a source.
- **Long straddles "for a breakout" without an RV-expansion trigger** — the
  structure needs a regime that actually delivers movement; and the
  linear-benchmark gate: a debit structure must beat the delta-equivalent
  futures position after costs.
- **Standing VIX-call hedges at the front** — 12 days of decay eat the
  budget; tail hedging at this horizon = wings inside the structures
  themselves.

## Consolidated pre-trade checklist (short-DTE)

- [ ] An RV forecast for 7–12d exists (HAR-class); IV is compared against
      it, not against trailing.
- [ ] Regime gate: VIX percentile, VX curve shape.
- [ ] The window's event calendar is mapped; the trade's regime is named
      (VRP / event).
- [ ] The MAE gate passes for the strike distance.
- [ ] Leg count is minimal; combo fills verified by fact, not by faith.
- [ ] Executable reprice with pessimistic exits: edge ≥ 3× round trip,
      edge-on-margin ≥ ~2% (investigate) / < 1% (pass).
- [ ] Management plan: delta stop, profit-take 25–50%, time stop, exit
      before expiration, no averaging down.
- [ ] PnL attribution is set up; a Γ+ν share < 25% → reclassify as a linear
      strategy.
