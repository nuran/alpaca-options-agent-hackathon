# Run notes

**Created:** 2026-09-04T10:28:46
**Underlying:** book6   **Window:** 2024-01-22 -> 2026-08-26

## What was run

Short-dated defined-risk vertical credit spreads, 4-7 DTE,
short leg at 0.15 delta (+/-0.08),
$5 wide, side=put.
Exits: take profit at 50% of credit, stop at 2.0x credit,
otherwise held to expiry.
Sizing: max 2.0% of equity at risk per trade,
at most 3 concurrent positions.
Friction tested: 1%, 3%, 6% per leg.

## Data lineage

Fetched with the Alpaca CLI into DuckDB via `data/ingest.py`, then read through
`backtest/store.py`. Underlying bars use feed=sip with adjustment=all; option bars come
from the indicative feed (this account is not OPRA-entitled).

Option bars in store: 1,661,101
(2024-01-22 -> 2026-08-26),
229,839 contracts, 644 expiries.

See `data_fingerprint.json` for the close-sum equivalence check that identifies this
exact dataset.

## Deviations from the vendored alpaca-trading-backtest skill

That skill's V1 supports `stocks` and `crypto` only and explicitly lists options as
unsupported ("options require explicit contract selection and fill logic"). This run is
options, so it follows the skill's *methodology and artifact conventions* -- run folder
layout, data fingerprints, metric formulas (sample-stddev Sharpe from daily equity),
fee modelling, mandatory disclosures -- while implementing the contract selection and
fill logic the skill says it does not provide.

Data is still fetched through the Alpaca CLI as the skill requires: `alpaca data option
bars` accepts up to 100 contract symbols per request.

## Known limitations

See `warnings.json` for the full list. The one that matters most: there is no historical
option bid/ask available from Alpaca at all, so fills are modelled from bar closes plus
an assumed friction. Treat the friction sweep, not any single number, as the result.
