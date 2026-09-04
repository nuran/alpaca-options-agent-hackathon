---
name: options-core-patterns
description: "Fundamental, actionable trading patterns for US-market options (SPX/SPY/ES, single stocks, ETFs) and the VIX complex (VX futures, VIX options, vol ETPs). First principles: right vs obligation, IV against forecast RV, put-call parity adjusted for American exercise and dividends, greeks as local theory, equity skew, 0DTE, earnings IV crush, VIX options priced off futures (not spot), VIX term structure as carry and regime, overnight/earnings gap risk, Reg-T vs portfolio margin, assignment risk. Use ALWAYS when choosing an underlying or structure on US options, when deciding to buy/sell premium on SPX or stocks, in any trade involving VIX or VXX/UVXY/SVIX, when hedging a portfolio with puts or VIX calls, in earnings setups, and when diagnosing PnL that diverged from the greeks — the first layer before options-vol-research. Includes a structure-ranking module for short tenors (0DTE–12DTE) and an OHLCV asset selector with scripts. Every pattern = mechanics, consequence, action."
---

# Patterns: US-Market Options and VIX

Each pattern: **Mechanics → Consequence → Action**. Market specifics are
baked into the patterns, not relegated to footnotes: American exercise,
dividends, cash/physical settlement, Reg-T/PM margin, earnings, the VIX
complex.

Detail files:
- `references/patterns.md` — playbook P1–P13 (when to apply, how it breaks,
  checklists). Read when building a specific position.
- `references/vix.md` — VIX-complex canon (V1–V5): futures, options, term
  structure, ETPs, hedge design. Read for ANY trade that touches VIX.
- `references/short-dte.md` — DTE ≤ 12 module: what the constraint does to
  vega/gamma/management, structure ranking S1–S5 with gates and
  falsification criteria. Read for ANY position design under ~2 weeks.
- `references/asset-selector.md` + `scripts/` — underlying selector from
  daily OHLCV: layered funnel (liquidity universe → OHLCV metrics →
  ShortVolScore/LongConvexScore), mapping into S1–S5, honest limits
  (IV-blindness). Read/run when choosing assets.

## P1. Right vs obligation asymmetry — the primary choice of side

**Mechanics.** The buyer pays premium for a right; the seller takes premium
for an obligation. On the US market the seller's obligation is amplified by
American exercise: assignment can arrive any day, not only at expiration.

**Consequence.** The seller has the higher probability of profit; the buyer
has the better tail shape. Buying a call has a *lower* probability of profit
than buying the stock — premium shifts the breakeven. The seller carries not
only the price tail but also the exercise tail (early assignment).

**Action.** Choose the side by the resource you pay with: time (long) or
tail (short), not by your directional forecast. Short American options only
with an assignment plan (see P12).

## P2. A directional trade through options is always a vol trade

**Mechanics.** Price = f(underlying, time, IV, strike, rate, dividends). A
rising stock does not guarantee a falling put; on the US market the biggest
IV distorter is earnings: IV inflates into the print and collapses after
(IV crush), regardless of the report's direction.

**Consequence.** Every option position is a vol position. An option bought
before earnings carries a built-in IV-crush loss that the move must
overcome; "right on direction, down on the position" after earnings is
normal, not an anomaly.

**Action.** Before the trade, write down the greeks and your IV view. For
earnings: compare the implied move (ATM straddle of the front series)
against the ticker's historical realized moves on prints — trade the spread
between them, not a forecast of the report. No vol view — express direction
in stock/ES.

## P3. The time-value map — where to sell, where to buy, and what 0DTE is

**Mechanics.** Time value peaks ATM, decays to zero at expiration; theta
accelerates into expiry, vega is proportional to time value. US specifics:
SPX/SPY expire every day — 0DTE is the map's limit point: near-zero vega,
extreme gamma and theta.

**Consequence.** Choosing tenor = choosing the gamma/vega risk mix: short
tenor trades realized movement (gamma vs theta), long tenor trades the IV
level (vega). 0DTE is a pure gamma-theta duel with no vega component.

**Action.** Sell rich time value (ATM, short tenor, high IV); buy cheap
convexity (OTM wings, longer tenor). Pick tenor by the type of view: an IV
view → vega tenors (30–90d); a realized-movement view → short/0DTE, sized
from gamma, not from premium.

## P4. IV against forecast RV — cheap/expensive only relative to a forecast

**Mechanics.** IV is set by option trades and reflects expected future vol;
HV is the past. IV−HV alone is not a signal; technical analysis of the IV
chart is not a trigger. On the US market, index VRP is systematically
positive (insurance is overpaid); on single stocks it is unstable and rips
on earnings.

**Consequence.** Selling index premium = harvesting VRP with a heavy left
tail; selling single-stock premium is a different risk (idiosyncrasy + M&A
+ earnings gaps where a stock can open −30%).

**Action.** Every idea = a pair of numbers: the position's IV vs your RV
forecast for the tenor. Index and single-stock premium are sized under
different rules: the stock tail is not "rare," it is calendar-scheduled
(earnings). The VIX level is regime context for SPX IV, not a trigger
(details: references/vix.md, V2).

## P5. Parity and synthetics — adjusted for American exercise and dividends

**Mechanics.** Clean parity (C − P = F − K) holds on European cash-settled
indexes (SPX, XSP, VIX options). On American single-stock and SPY/QQQ
options, parity is distorted by early exercise and dividends: deep ITM calls
lose their time value before ex-div; puts can trade above "parity" because
of rates.

**Consequence.** On SPX, synthetics are exact: direction is flipped with
ES/futures without touching the option leg. On stocks, "equivalent"
substitutions carry dividend and assignment risk: a short ITM call through
ex-div will almost surely be exercised, turning the position into short
stock owing the dividend.

**Action.** Express index ideas in SPX/XSP by default (European exercise,
cash settlement, Section 1256 tax treatment) rather than SPY, absent a
small-size liquidity reason. If the venue offers no index options (Alpaca:
SPX/XSP/VIX untradable) — the working instruments are SPY/QQQ with P12 fully
engaged (assignment, ex-div, physical settlement). On stocks, before every
ex-div date check short ITM calls: call time value < dividend → prepare for
assignment or close.

## P6. Greeks are local theory, not cash accruals

**Mechanics.** Greeks are model derivatives, ceteris paribus. Theta is not
"debited" on a calendar: weekend decay is priced in ahead of time (market
makers shift effective time on Friday), a large price step moves delta
itself (gamma), an IV shock overwhelms a week of theta in minutes.

**Consequence.** Greeks are navigation; PnL is fact; the attribution
residual is information about which factor dominated. "Sell Friday, buy
back Monday, collect weekend theta" is an illusion — it is already in the
prices.

**Action.** Aggregate greeks at the book level (per ticker and in SPX
beta). Run PnL attribution (delta/gamma/vega/theta components + residual)
and investigate the residual. Never build a strategy on "guaranteed"
calendar decay.

## P7. Equity skew — the market prices crashes, not symmetry

**Mechanics.** On indexes, OTM puts are systematically richer than
equidistant OTM calls (indexes fall faster than they rise; hedge demand is
structural). Skew is a separate coordinate on top of the IV level; the
spot-vol correlation is negative: a falling market means rising IV, which
reinforces both puts and VIX.

**Consequence.** A long index put wins twice (delta + vega); a short one
loses twice. Selling the index put wing = a short-crash position with
doubled factor leverage. On single stocks skew is milder and can invert
into earnings (call demand).

**Action.** Compare wings with RR/fly metrics, not the ATM IV level.
Selling the rich put wing requires an explicit view that the market's crash
price is too high, plus a closed tail. Hedge design: compare SPX puts vs
VIX calls (references/vix.md, V4) — they trade different parts of the same
mechanism.

## P8. Carry curves: ES basis and the VIX term structure

**Mechanics.** The US-market analog of "funding" is two carry curves.
(1) The ES/SPX basis: rate minus dividends, nearly deterministic, breaks
only in stress (2020-style). (2) The VIX futures term structure: in calm
regimes contango (deferred futures above spot VIX), each future rolls down
toward spot — systematic roll-down; in stress, backwardation.

**Consequence.** VIX contango is the carry source of short-vol positions
and the constant bleed of long-vol ETPs (VXX/UVXY); backwardation is the
stress-regime marker and the fuel of short-vol disasters (the February 2018
squeeze). The shape of the VIX curve is one of the cleanest regime
indicators for the entire options book.

**Action.** Monitor the VIX term structure (VX1/VX2, spot/VX1) as a regime
filter: contango → carry strategies allowed with a tail hedge; flattening/
inversion → cut short vega down to a cheap re-entry. Details and thresholds
— references/vix.md (V2, V3).

## P9. Gaps break stops — overnight and earnings are a gap calendar

**Mechanics.** US stocks gap overnight every day; earnings are scheduled
gaps (±10–30% on single names); circuit breakers halt trading but do not
prevent gap opens. A stop fills at the next price and loses the position; a
long put caps the loss at exactly the premium and keeps the position.

**Consequence.** Linear protection does not bound the tail. On the US
market the tail is not a "rare event" but a schedule: every night, every
report, every macro date (CPI/FOMC).

**Action.** Every obligation-bearing position must survive a gap to the
instrument's historical maximum (for single stocks — the worst earnings gap
in the sector). Sold tails are closed with wings or size. Wing cost is an
infrastructure expense. Naked short single-stock options are never carried
through earnings without a wing.

## P10. Stress margin: Reg-T vs portfolio margin, and vega inflation

**Mechanics.** Margin on a short option grows against the position; rising
IV inflates requirements with no price move. Portfolio margin (PM) is
efficient in calm but is recomputed under stress scenarios and jumps
together with IV; brokers may raise house requirements in stress; VIX
products often carry elevated requirements.

**Consequence.** A seller can be right at expiration and liquidated before
it — the margin call arrives mark-to-market at the moment of worst
liquidity. The PM discount is procyclical credit: it grants the most
leverage at peak calm.

**Action.** Size short premium from stress requirements (simultaneous price
and IV shock across all legs, house add-ons), not current ones. Hold free
collateral for the "PM recomputed in stress" scenario. The de-risking plan
triggers on margin utilization, not on PnL.

## P11. Structures trade the shape of the distribution

**Mechanics.** A spread reduces (does not fix) risk and caps profit; a
strangle vs a straddle has a flat loss zone and a lower risk/reward ratio;
a ratio spread has unbounded risk at any short proportion >1; an earnings
calendar on stocks is a separate position on the IV difference between
series (front IV inflated). A straddle can lose even when price moves, if
theta + vol-down outweigh it.

**Consequence.** Every structure is a bet on the shape of the distribution
(magnitude × path), not "a way to make it cheaper." Earnings structures
trade the IV shape between series, not the report's direction.

**Action.** Choose the structure from the forecast shape of the move.
Legging: the bought leg fills first, the sale second. On SPX prefer single
combo orders (the complex order book supports it), not legs.

## P12. Assignment and expiration — exercise is an event, not a formality

**Mechanics.** American options (stocks, SPY/QQQ/IWM) can be exercised any
day; rationally — calls before ex-div (when time value < dividend) and deep
ITM puts (funding cost). SPX is European, cash-settled, with no assignment
risk before expiry; AM/PM settlement differs by series. Physical settlement
turns an ITM option into a stock position with its full margin; pin risk:
at the strike on expiration day you do not know whether you are exercised.

**Consequence.** A short American leg is an obligation with an open date:
the position can change overnight without your action (waking up short the
stock through ex-div). Cash-settled indexes remove an entire risk class.

**Action.** Take profit by selling/buying back in the book, not through
expiration. An ex-div calendar across all short calls is a mandatory check.
Close SPY/stock spreads before expiration when both legs are near the money
(pin risk + asynchronous exercise). Default index instrument — SPX/XSP for
European cash settlement.

## P13. Pseudo-equivalences — verify the profile, not the name

**Mechanics.** True equivalent: a bull call spread ≡ a bull put spread at
the same strikes on SPX (differences: debit/credit and margin). False:
"call = short put"; "covered call on a stock = short put on SPX" (different
skew, dividends, assignment); "SPY position = SPX position"
(American/European style, physical/cash, 1256 taxes, size). VIX
instruments are a separate world: a VIX call is not equivalent to an SPX
put (references/vix.md, V4).

**Consequence.** Equivalence is only full-profile: payoff across the whole
axis + greeks + margin + exercise/settlement + worst case.

**Action.** Run every substitution through five checks (payoff, greeks,
margin, settlement, worst case). Among true equivalents, choose by
liquidity, margin and taxes.

## VIX: five rules of the complex (full canon — references/vix.md)

- **V1.** Spot VIX is untradable; VIX options price off the CORRESPONDING
  future, not spot. Greeks and moneyness are computed from the future.
- **V2.** The term structure is carry and regime: contango = roll-down
  income for shorts and bleed for longs; backwardation = stress regime.
- **V3.** VIX is mean-reverting with positive skew (calls richer than puts)
  — the mirror of equity skew; shorting VIX = shorting the squeeze with an
  unbounded tail.
- **V4.** Hedge design: SPX puts vs VIX calls — different triggers (price
  vs vol), different behavior in a slow grind vs a crash.
- **V5.** Vol ETPs (VXX/UVXY/SVIX) are derivatives on a futures basket with
  a daily roll: path matters more than level; longs bleed out in contango,
  shorts die in the squeeze.

## Order of application

1. P1 — side: what do I pay with, time or tail (+ the short's assignment
   tail).
2. P2+P4 — the pair of numbers, IV vs forecast RV; earnings calendar
   checked.
3. P8+V2 — regime via the VIX term structure and basis; is short vega
   allowed?
4. P7 — skew: do I agree with the market's crash price; SPX put vs VIX
   call.
5. P3+P11 — tenor (vega vs gamma, 0DTE logic), strikes, structure matching
   the forecast shape.
6. P5 — instrument: SPX/XSP by default, SPY/stocks for a reason; parity
   adjusted for dividends.
7. P9+P10 — size from the gap calendar (overnight/earnings) and PM stress
   margin.
8. P6 — book greeks aggregated, attribution running.
9. P12+P13 — ex-div calendar, exit plan before expiration, full-profile
   equivalence check.
