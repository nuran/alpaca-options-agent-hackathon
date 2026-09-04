# The VIX Complex: Canon V1–V5

Read for any trade that touches VIX: VX futures, VIX options, vol ETPs
(VXX, UVXY, VIXY, SVIX/SVXY), hedging an SPX book with vol.

## V0. What VIX is (one paragraph)

VIX is a calculated index of 30-day SPX implied vol, assembled from a strip
of OTM SPX options (variance-swap methodology). Spot VIX is **untradable**:
you cannot "buy VIX at 15." What trades: VX futures (a contract per month +
weeklies), options on VIX, and ETPs on futures baskets. Everything tradable
is an expectation of VIX on a date, not VIX itself.

## V1. VIX options price off the future, not off spot

**Mechanics.** A VIX option expiring on date T is European, cash-settled,
settles to a special opening quotation (SOQ), and before expiry prices off
the VX future of the same date T. With spot at 15 and the 3-month VX at 19,
a 17-strike call is an option *in the money relative to the future*, even
though it is "above spot."

**Consequences.**
- Moneyness, delta, breakevens — all computed from the corresponding
  month's future.
- "Cheap" far-dated VIX calls at low spot are often already paid for by
  contango: the future sits higher, the strike is closer, the premium is
  larger.
- A VIX put with spot at 13 and VX at 17 can be expensive not because a
  "drop is expected," but because the future must roll down to spot if calm
  persists.
- VIX options of different months are options on DIFFERENT underlyings
  (different futures): cross-month verticals do not exist; calendars are
  spreads of two different underlyings.

**Action / checklist.**
- [ ] For every leg, its future and that future's price are recorded; all
      greeks run off it.
- [ ] Breakevens computed from the future, not from spot VIX.
- [ ] Calendar constructions understood as inter-futures spreads.
- [ ] Expiration: Wednesday, AM SOQ; the settlement-print gap risk is
      accounted for (SOQ can differ from the prior close).

## V2. The term structure — carry and regime indicator

**Mechanics.** In calm regimes the VX curve is in contango: deferred
futures above spot, and each future rolls down toward spot over time —
roll-down. In stress the curve inverts (backwardation): spot above
futures, the market pricing normalization.

**Consequences.**
- Contango = systematic income for short futures / long SVIX and systematic
  bleed for long ETPs and long VIX calls.
- Backwardation = the stress regime; shorting vol in backwardation is
  catching a falling knife against carry.
- The curve's shape is a leading filter for the entire SPX book: contango
  flattening often precedes rising realized vol.

**Action / checklist.**
- [ ] Record daily: spot/VX1, VX1/VX2 (annualized roll).
- [ ] Normal contango → carry strategies allowed with a closed tail.
- [ ] Flattening below threshold / inversion → the book's short vega is cut
      by a pre-written rule (not "by feel in the moment").
- [ ] Short-vol re-entry — not by the VIX level ("it's 30 already, time to
      sell"), but by the curve's return out of backwardation: the level does
      not mean-revert on schedule; the curve's shape is more informative
      than the level.

## V3. The VIX distribution: mean reversion, positive skew, vol-of-vol

**Mechanics.** VIX cannot go to zero and can multiply within days (9→50 in
a week has happened). The distribution has a hard floor (~9–10) and a long
right tail. Hence VIX calls are systematically richer than VIX puts
(positive skew — the mirror of negative equity skew), and mean reversion is
strong but untimeable.

**Consequences.**
- Short VIX (futures, calls; long puts are insufficient) = a position with
  bounded profit (the floor) and a multiplicative tail (the squeeze).
  February 2018: a one-day +100%+ move in the futures, the death of XIV.
- Long VIX calls are lottery tickets with a known negative expected return
  (paying for V2 carry + V3 skew); justified only as a budgeted hedge, not
  as a standalone "bet on fear."
- VIX puts are often the cheapest expression of "normalization" in
  backwardation: bounded risk vs knife-catching with short futures.

**Action / checklist.**
- [ ] Short-vol positions are sized to the "future ×2 in a day" scenario,
      not to the historical daily sigma.
- [ ] The long hedge runs as a budget (X% of NAV per year in premium) with
      a roll rule, not as one-off purchases on fear.
- [ ] In backwardation, the normalization bet is expressed with bought VIX
      puts / put spreads, not a naked short future.
- [ ] Ratio structures on VIX calls (selling the upper wing) are forbidden
      without a closed tail: the right tail of VIX is not bounded by
      history.

## V4. Hedge design: SPX puts vs VIX calls

**Mechanics.** Both hedge the same mechanism (SPX down → IV up) via
different triggers. An SPX put pays off PRICE (the strike must be pierced)
and carries vega as a bonus. A VIX call pays off VOL: it can fire on a
sharp IV rise even on a moderate price move, but expires worthless in a
slow grind lower with calm vol (2015-style), where the SPX put would have
reached the money.

**Consequences.**
- Crash gap (COVID 2020): VIX calls deliver more payoff per premium dollar
  (vol convexity + VIX beta to the selloff).
- Slow grind: SPX puts work, VIX calls burn.
- The combination is not redundant — these are two different scenarios of
  the same bear.

**Action / checklist.**
- [ ] The hedge budget is split by scenario: crash gap → VIX calls / call
      spreads; grind down → SPX puts / put spreads in longer series.
- [ ] For the VIX leg: strike and month chosen off the future (V1); the
      carry cost (V2) is included in the hedge's price.
- [ ] Monetization is pre-written: what a fired hedge converts into (sale,
      roll down, conversion to a spread) — deciding during panic is too
      late.

## V5. Vol ETPs: path over level

**Mechanics.** VXX/VIXY/UVXY (long; UVXY levered) and SVIX/SVXY (short)
hold a rolled VX1/VX2 basket at constant 30-day duration. The daily roll in
contango = systematically buying rich / selling cheap for the long side.
Levered/inverse versions additionally carry volatility drag from daily
rebalancing.

**Consequences.**
- Long ETPs structurally bleed out: tens of percent per year in contango;
  reverse splits of VXX/UVXY are a fact of life. Not an instrument to "buy
  and wait for the crash."
- Short ETPs collect carry but inherit the V3 tail: the SVXY mechanics
  survived 2018 only after the leverage was cut; XIV did not.
- ETP PnL is determined by the path of the futures curve, not by the VIX
  level: VIX can return to the same level while the ETP does not.

**Action / checklist.**
- [ ] ETPs are used only for short-horizon exposure (days), never as a
      hedge warehouse.
- [ ] For any ETP position, the roll contribution (current curve's
      contango/backwardation) to expected PnL is modeled.
- [ ] Options on ETPs: remember the underlying is itself a derivative with
      drag — long puts on a long ETP enjoy a roll tailwind, calls fight a
      headwind.
- [ ] Shorting a long ETP as a "smart" carry trade is checked for squeeze
      risk, borrow cost, and the broker's recall rules.

## Mapping to the SKILL.md patterns

- V1 → P5/P6: greeks and parity are computed off the underlying future.
- V2 → P8: the term structure = the carry curve and the book's regime
  filter.
- V3 → P7/P9: VIX skew mirrors equity skew; the tail is not bounded by
  history.
- V4 → P7: choosing the hedge instrument = choosing the scenario.
- V5 → P13: an ETP is equivalent neither to spot VIX nor to a future — a
  path-dependent derivative.
