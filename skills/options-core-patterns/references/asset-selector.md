# US-Market Asset Selector for Options Trading (Daily OHLCV)

Read when choosing/ranking underlyings for options strategies. Executable
parts: `scripts/universe_filter.py` (stages 0–3 from universe.json) and
`scripts/asset_selector.py` (OHLCV metrics and scores). This module ranks
**the RV side of the edge and tradability**; the IV side (IV level, skew,
spreads, OI per strike) is invisible to daily OHLCV and is verified as a
second stage under the options-vol-research gates.

## Full pipeline

`universe.json` → `scripts/universe_filter.py` (stages 0–3: venue, expiry
density under DTE≤12, penny/OI/strikes, factor tags) → OHLCV for the
shortlist → `scripts/asset_selector.py` (scores) → chain-level IV stage
(short-dte.md).

## Layer −1: the venue funnel (universe_filter.py)

- **Venue gate:** European-style index products (SPX/XSP/VIX/DJX) are
  untradable on Alpaca — index ideas are expressed with SPY/QQQ/IWM
  (American style, physical settlement: P12 assignment/ex-div fully
  engaged). S5 (VIX options) does not exist on this venue; VIX is a regime
  input only, and vol exposure runs only through VXX with the V5 caveats.
- **Expiry gate:** for the DTE≤12 mandate only dailies and weeklies
  qualify; the "2–3 expirations" tier (monthlies, ~3/4 of the universe) is
  cut — an expiry inside the window is not guaranteed.
- **Executability proxies:** penny-program membership (the strongest
  available proxy for tight spreads), an OI floor (file OI is a floor, not
  a census: a rank, never an absolute), strike density.
- **Factor tags:** crypto_proxy (duplicates the crypto desk's factor!),
  leveraged (drag), vol_etp (V5), stock → event regime S3 by default. One
  cluster = one portfolio slot.

Reference result on universe.json (2026-08): CORE = SPY, QQQ, IWM, GLD,
XLF, SMH (dailies, OI≥1M, full penny — spanning six distinct vol factors);
EXTENDED ≈ 196 (weeklies+penny+OI≥300k): ~30 ETFs (TLT, HYG, SLV, USO,
UNG, EEM/FXI/KWEB/EWZ, sector XL*, XBI, GDX...), ~140 single names (S3),
a crypto cluster ~17, a leveraged cluster ~6, VXX.

## OHLCV-stage architecture: three layers

**Layer 0 — static universe (facts not derivable from OHLCV).**
Option liquidity, exercise style, settlement come from the reference table:

| Tier | Assets | Properties |
|---|---|---|
| A: index core | SPX/XSP (via ES/SPY OHLCV), SPY, QQQ, IWM, VIX complex | deepest chains; SPX/XSP European, cash, 1256 |
| B: liquid ETFs | TLT, GLD, SLV, USO, HYG, EEM, FXI, XLE, XLF, SMH | American style; distinct vol drivers (rates, oil, metals) |
| C: mega caps | AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA, AMD, NFLX | liquid chains, but event (earnings) regime by default |

Anything outside the table is not admitted to ranking: an OHLCV score
without option liquidity is garbage (leg count and spreads will kill any
edge).

**Layer 1 — OHLCV metrics (what the script actually computes).**

| Metric | Computation | Meaning for options |
|---|---|---|
| RV_21 (Parkinson + c2c) | high-low estimator + close-close, annualized | vol level: too low → premiums don't cover friction |
| Vol percentile 1y | percentile of current RV_21 over a year | regime gate: extreme low = gamma trap, extreme high = stress |
| Persistence (HAR-lite R²) | in-sample R² of fwd RV_5d on (1d, 5d, 22d) | RV forecastability — the core of VRP suitability: the edge IS the forecast |
| Overnight share | close→open variance share of the total | how much risk arrives where hedging is impossible |
| Gap tail | p99 |gap| / mean daily vol | jumpiness; input to the MAE gate |
| MAE_10d | worst adverse excursion over 10-day windows (via high/low) | direct input to strike-distance checks for 7–12 DTE |
| Vol-of-vol | std of log RV_21 changes | regime explosiveness: bad for shorts, fuel for long convexity |
| Expansion prob | P(RV_fwd_21 > 1.5 × RV_trail_21) | frequency of calm→storm exits (the gamma trap, quantified) |
| Efficiency ratio | Kaufman: |ΔP_20| / Σ|ΔP_1| | chop feeds short gamma; trend feeds debit structures |
| Earnings signature | clustering of top gaps on a ~quarterly grid | auto-tag "event asset" (regime S3, not VRP) |
| Dollar volume | median close × volume | underlying liquidity proxy (hedge legs) |

**Layer 2 — two scores and the mapping into structures (S1–S5,
short-dte.md).**

- **ShortVolScore** (suitability for S1/S2, selling premium at 7–12 DTE):
  higher with high persistence, low gap tail, low overnight share, moderate
  vol-of-vol, a sufficient RV level (premium exists), a mid-range
  percentile, high liquidity. The earnings signature (Tier C) cuts the
  score — those names route to S3.
- **LongConvexScore** (suitability for debit/event structures): higher with
  a low current percentile + high expansion prob + high vol-of-vol + high
  efficiency ratio (the regime delivers movement — the linear-benchmark
  gate still applies as a second stage).

Scores rank candidates; they are not entry signals. Between a score and a
trade stand all the options-vol-research gates (IV vs HAR forecast, dollar
vega yield, executable reprice).

## VIX: the special case

- OHLCV of VIX itself is a **regime input, not a tradable asset**: spot is
  untradable; what trades is VX futures with their own dynamics
  (convergence, roll).
- The selector uses the VIX series for: the regime percentile (a gate for
  all short-vol scores), the market's vol-of-vol, inversion detection
  (proxy — VIX spikes with negative SPX efficiency).
- Scoring VIX as an underlying requires OHLCV of the **VX futures** (front
  + second) — if present in the data, the script computes roll proxies
  (VX1−VIX, VX2−VX1); without them, VIX structures (S5) are ranked manually
  per vix.md.

## Honest limits of the method

1. **IV-blindness.** OHLCV ranks where RV is forecastable and the tail
   manageable, but does not know whether that is already paid in IV. The
   #1 ShortVolScore asset with cheap IV is NO_TRADE.
2. **In-sample persistence R²** is a proxy, not walk-forward: fine for
   ranking, forbidden as "proven forecastability." The finalist gets a full
   HAR with purged walk-forward.
3. **Ranking = multiple testing.** The top of an N-candidate list carries
   selection bias; treat it as a shortlist for falsification, not a proven
   ordering.
4. **Chain liquidity** is verified by fact (per-strike OI, live spreads) at
   trade time — dollar volume of the underlying is necessary, not
   sufficient.

## Usage protocol

1. Run `universe_filter.py` on a fresh universe.json → eligibility +
   buckets + factor tags.
2. Run `asset_selector.py` on the survivors' OHLCV (≥ 2 years daily per
   ticker; the VIX series is mandatory as the regime input).
3. Take top-3 by ShortVolScore and top-3 by LongConvexScore as the
   shortlist, respecting one slot per factor cluster.
4. Second stage for the shortlist: live chains (IV, skew, spreads, OI),
   gates and executable reprice per short-dte.md.
5. Recompute scores weekly; a leadership change is not by itself a signal
   (scores are slow), but a name falling out of eligibility (gap tail,
   regime) is an immediate stop for new positions in it.
