# Playbook P1–P13: Application, Limits, Checklists (US Market)

Expansion of the SKILL.md patterns for the US market. VIX specifics live in
vix.md; short-tenor structure ranking in short-dte.md.

## P1. Right vs obligation

**How it breaks.** The illusion "selling premium = income": the win rate is
paid for with the tail, and on the US market the tail is double — price and
assignment (a short American leg can be exercised any night). Symmetric
buyer illusion: "risk is capped" ≠ "risk is small" — a string of burned
0DTE premiums kills an account faster than a gap.

**Checklist.**
- [ ] The resource is named: time (long) or tail (short).
- [ ] For shorts: worst price scenario + early-assignment scenario.
- [ ] For longs: theta/day × holding period < expected move × probability.
- [ ] Remember: buying a call wins less often than buying the stock.

## P2. Multi-factor pricing / earnings

**How it breaks.** Buying options "into the print" without the math: IV
crush removes 20–60% of front-series premium overnight. And selling
"expensive" earnings vol without recognizing the implied move is often
honest: inflated IV is not edge by itself.

**Checklist.**
- [ ] Greeks and the IV view written down before entry.
- [ ] Earnings: implied move (front ATM straddle) vs the ticker's realized
      moves over the last 8–12 prints.
- [ ] The trade is formulated as implied vs expected realized, not as a
      forecast of the report's direction.
- [ ] The macro calendar (CPI, FOMC, NFP) checked for index positions.

## P3. Time-value map / tenor selection

**How it breaks.** Selling 0DTE "for theta" without seeing it is a pure
gamma position: theta there is compensation for gamma, not free income. And
buying long-dated options "for a move view" when the view is actually about
IV.

**Checklist.**
- [ ] The view type is named: realized movement → short tenor/0DTE; IV
      level → 30–90d (vega tenors).
- [ ] 0DTE shorts sized from gamma (distance to stop/wing), not premium.
- [ ] The sold leg sits at maximum time value; the bought leg where
      convexity is cheap.
- [ ] Noted: theta and vega concentrate in the same place (ATM).

## P4. IV vs forecast RV

**How it breaks.** Comparing IV with history (HV) instead of a forecast;
using IV-chart TA as a trigger; transplanting index VRP logic onto single
stocks, where the tail is calendar-scheduled (earnings) and idiosyncratic
(M&A, guidance).

**Checklist.**
- [ ] Numbers exist for IV and for the RV forecast over the position's
      tenor.
- [ ] The spread covers costs + tail compensation.
- [ ] Index and single-stock premium sized separately, separate limits.
- [ ] The regime indicator (VIX term structure, vix.md V2) decides whether
      short vega is allowed at all.

## P5. Parity, dividends, SPX/SPY choice

**How it breaks.** Transplanting clean European synthetics onto American
options: a deep ITM short call before ex-div gets exercised when its time
value < dividend — the "synthetic" becomes short stock owing the dividend.
And choosing SPY "by habit" where SPX/XSP offer European exercise, cash
settlement, and 1256 taxes.

**Checklist.**
- [ ] Index ideas default to SPX/XSP; SPY only for a reason (small size,
      liquidity of specific strikes).
- [ ] The ex-div calendar checked for every short call leg: time value vs
      dividend.
- [ ] Direction flips on SPX via ES/MES without touching the option leg; on
      stocks — with assignment risk accounted for.
- [ ] A "parity violation" on stocks is first explained by dividend/rate/
      borrow, only then considered arbitrage.

## P6. Greeks as local theory

**How it breaks.** "I'll collect weekend theta" (it is priced in ahead);
linear extrapolation of delta over a large step (gamma moves delta);
reading the sign of position greeks as the position's composition.

**Checklist.**
- [ ] Greeks aggregated at book level: per ticker and in SPX beta.
- [ ] PnL attribution: delta/gamma/vega/theta + residual; the residual is
      investigated.
- [ ] No strategy leans on calendar-"guaranteed" decay.
- [ ] Scenario analysis (±X% price, ±Y vol points) supplements greeks on
      large steps.

## P7. Equity skew

**How it breaks.** Selling "expensive" index puts as "harvesting the
overpayment": doubled factor leverage (delta+vega against the position) in
a crash. Reading pre-event skew inversion on a stock as an anomaly (it is
call demand — normal for biotech/M&A).

**Checklist.**
- [ ] Wings compared with RR/fly metrics, separately from the ATM IV
      level.
- [ ] Selling the index put wing — only with a closed tail and an explicit
      thesis for why the crash price is too high.
- [ ] The hedge chosen by scenario: SPX puts (grind) vs VIX calls (crash) —
      vix.md V4.
- [ ] The book's skew exposure known separately from its vega.

## P8. Carry curves (ES basis, VIX term structure)

**How it breaks.** Ignoring the VIX curve's shape while trading SPX
premium: short vega in backwardation is trading against the regime.
Details, thresholds, re-entry — vix.md V2.

**Checklist.**
- [ ] Spot/VX1 and VX1/VX2 recorded daily in annualized terms.
- [ ] The rule for cutting short vega on flattening/inversion is written in
      advance.
- [ ] The ES basis checked for anomalies in stress (dislocation = signal).

## P9. The gap calendar

**How it breaks.** It doesn't — this is the hardest pattern. It is only
weakened by underestimating the maximum gap: for single stocks the max gap
= the worst earnings gap in the sector (biotech: −60%+ on trial data), not
the ticker's own history.

**Checklist.**
- [ ] Every position survives a gap to the asset class's historical
      maximum.
- [ ] Earnings dates for all short legs collected into a calendar; naked
      shorts through a print forbidden.
- [ ] Wings bought; their cost included in the strategy's expected return.
- [ ] A stop is nowhere counted as gap protection; where a stop would
      stand, a put is priced.

## P10. Stress margin (Reg-T / PM)

**How it breaks.** Sizing from current PM requirements at peak calm: the PM
discount is procyclical — stress recomputes requirements in a jump together
with IV, brokers add house surcharges, VIX products get special
requirements.

**Checklist.**
- [ ] Stress requirements computed under a simultaneous price and IV shock
      across all legs + a house buffer.
- [ ] Free collateral ≥ stress − current, with a margin of safety.
- [ ] The de-risking plan triggers on margin utilization, not PnL.
- [ ] The broker's rules for VIX products and concentration known in
      advance.

## P11. Structures

**How it breaks.** Choosing a structure "by cheapness"; ratios without a
tail (unbounded risk at any proportion >1); earnings calendars without
understanding that the trade is the IV difference between series and the
front collapses after the print.

**Checklist.**
- [ ] Structure ← forecast shape of the move (direction × magnitude ×
      path).
- [ ] Spread: the profit cap weighed against what was paid/received.
- [ ] Ratio: the guaranteed-loss point computed; the tail closed.
- [ ] Earnings calendar: the position formulated in terms of front vs back
      IV.
- [ ] SPX structures executed as combo orders, not legs; where legging is
      unavoidable — the purchase first.

## P12. Assignment and expiration

**How it breaks.** Treating expiration as a formality: pin risk at the
strike, asynchronous knowledge of exercise across a spread's legs (the
short leg assigned, the long not exercised), physical settlement of
SPY/stocks with the full margin of the delivered position, overnight ex-div
assignment.

**Checklist.**
- [ ] Default exit — closing in the book before expiration.
- [ ] The ex-div calendar for short calls — a weekly check.
- [ ] Spreads with near-the-money legs are not carried into expiration.
- [ ] If holding an ITM leg: the delivered position (shares/cash) planned
      for margin and hedge; for SPX the series' AM/PM settlement known.

## P13. Pseudo-equivalences

**How it breaks.** In both directions: false equivalents ("SPY = SPX",
"covered call = short put" ignoring dividends/assignment/taxes; "VIX call =
SPX put") and missed true ones (call spread ≡ put spread at the same SPX
strikes — chosen by credit/debit, margin and book, not by the name's
direction).

**Checklist.**
- [ ] Five checks: payoff across the whole axis, greeks, margin,
      settlement/exercise, worst case.
- [ ] Only delta or the payoff picture matches → it is a different
      position.
- [ ] Among true equivalents, the better book/margin/tax variant chosen
      (1256 for SPX/XSP/VIX vs the ordinary regime of SPY/stocks).

## End-to-end example

Idea: "S&P is stretched; I expect a correction within 1–2 months; VIX is
low."

1. **P1**: directional view, unwilling to pay with the tail → buyer.
2. **P2/P4**: SPX 30–60d IV below my RV forecast → buying premium is
   consistent with the VRP assessment; the macro calendar (CPI/FOMC) inside
   the tenor is part of the forecast.
3. **P8/V2**: the VIX curve in stable contango → calm regime; long vega
   will pay carry — that cost is computed via roll-down.
4. **P7/V4**: the scenario is "correction," not "crash" → an SPX put spread
   fits better than VIX calls; if also hedging the crash tail — a small
   budget slice in VIX call spreads (strikes off the future, V1).
5. **P3/P11**: 45–60 DTE (vega tenor), put spread: buy ~25-delta put, sell
   a lower wing — selling the expensive part of the skew; the profit cap
   accepted consciously.
6. **P5**: instrument — SPX/XSP (European, cash, 1256), combo order.
7. **P9/P10**: risk = net premium; size = the amount written off to zero;
   margin trivial (debit spread).
8. **P6**: book greeks updated; attribution will show whether delta or vega
   pays.
9. **P12/P13**: exit — buy the spread back at target / 7–10 days before
   expiration; the call version of the same spread compared on credit and
   book.
