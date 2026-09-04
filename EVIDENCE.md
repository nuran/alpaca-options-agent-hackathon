# Evidence pack — 2026-08-30_book16_frozen-b16_1Day

> **What this file covers, and what it does not.** These statistics were computed in the
> development repository over its full grid search — **379 configurations** — for a
> **16-underlying** champion configuration (`frozen-b16`). That is *not* either of the two
> books shipped here, whose universes and tenors differ. It is included because the
> multiple-testing result is the single most important caveat on any parameter chosen by
> search, and it does not become less true for being inconvenient: a deflated Sharpe of
> **0.563** does not clear the conventional 0.95 bar. The run folders it references live in
> the development repo and are not part of this submission. The evidence for the two books
> that *are* shipped is the friction sweep in README section 5, reproducible with
> `make restore && make sweep`.

Window 2024-01-18 → 2026-08-28 · 379 configurations evaluated · net of modelled friction and fees.

## Headline (champion)

| metric | value |
|---|---:|
| total return | 119.53% |
| Sharpe | 1.36 |
| max drawdown | -21.70% |
| trades | 1238 |
| win rate | 90.3% |
| profit factor | 1.58 |

## Multiple testing

- Selection pool: **379** (376 grid cells + 3 kept run folders)
- Observed annualised Sharpe: **1.36**
- Benchmark SR\* (expected max of 379 trials): **1.26**
- Return skew -0.69, kurtosis 10.48, 645 daily observations
- **Deflated Sharpe Ratio: 0.563** (does NOT clear the conventional 0.95 bar)

## Backtest overfitting (CSCV)

- Variants in the matrix: 3 · splits: 70 · periods: 644
- NOTE: the matrix below holds only the kept run folders. The PBO over the grid the parameters were actually selected from (75 variants) is in runs/_grid/overfit.json.
- **PBO = 0.34** — the share of splits where the in-sample winner lands in the bottom half out of sample (median logit +0.00)

## Rolling windows (net of cost)

| start | end | return | Sharpe |
|---|---|---:|---:|
| 2024-02-02 | 2024-05-02 | -0.37% | 0.05 |
| 2024-03-05 | 2024-06-03 | +2.19% | 0.52 |
| 2024-04-04 | 2024-07-03 | +3.80% | 0.76 |
| 2024-05-03 | 2024-08-02 | -0.10% | 0.10 |
| 2024-06-04 | 2024-09-03 | -3.94% | -0.50 |
| 2024-07-05 | 2024-10-02 | -2.53% | -0.25 |
| 2024-08-05 | 2024-10-31 | +8.40% | 1.35 |
| 2024-09-04 | 2024-12-02 | +12.14% | 2.05 |
| 2024-10-03 | 2025-01-02 | +8.96% | 1.76 |
| 2024-11-01 | 2025-02-04 | +18.36% | 4.41 |
| 2024-12-03 | 2025-03-06 | +7.65% | 1.34 |
| 2025-01-03 | 2025-04-04 | -7.88% | -0.86 |
| 2025-02-05 | 2025-05-06 | -13.36% | -1.70 |
| 2025-03-07 | 2025-06-05 | -2.04% | -0.13 |
| 2025-04-07 | 2025-07-08 | +16.85% | 3.26 |
| 2025-05-07 | 2025-08-06 | +23.50% | 3.78 |
| 2025-06-06 | 2025-09-05 | +23.79% | 4.77 |
| 2025-07-09 | 2025-10-06 | +28.84% | 5.08 |
| 2025-08-07 | 2025-11-04 | +24.26% | 4.72 |
| 2025-09-08 | 2025-12-04 | +8.89% | 1.47 |
| 2025-10-07 | 2026-01-06 | +2.82% | 0.54 |
| 2025-11-05 | 2026-02-05 | -8.93% | -1.08 |
| 2025-12-05 | 2026-03-09 | +6.85% | 1.06 |
| 2026-01-07 | 2026-04-08 | +8.99% | 1.31 |
| 2026-02-06 | 2026-05-07 | +24.96% | 3.48 |
| 2026-03-10 | 2026-06-08 | +12.10% | 1.64 |
| 2026-04-09 | 2026-07-09 | +9.23% | 1.42 |
| 2026-05-08 | 2026-08-07 | +12.09% | 1.77 |

## All variants tried

| variant | return | Sharpe | trades | win rate |
|---|---:|---:|---:|---:|
| 2026-08-30_book16_frozen-b16_1Day ← | +119.53% | 1.36 | 1238 | 90.3% |
| 2026-08-30_book16_alloc-pername_1Day | +121.98% | 1.35 | 1238 | 90.3% |
| 2026-08-30_book6_champ-pv-b6_1Day | +100.66% | 1.27 | 1011 | 90.8% |

## Caveats carried from the run

- No historical option bid/ask exists: Alpaca serves no historical options quotes endpoint (/v1beta1/options/quotes returns 404, verified 2026-08-25). Entry and exit prices are derived from daily bar closes with a modelled friction, NOT from a real book. This is the single largest source of error in these results.
- Fills are same-bar: a signal from day T's close is filled at day T's close with friction applied against the trade. The vendored skill flags same_bar as carrying look-ahead risk. It is used because daily bars are the only option history available; the friction sweep bounds the resulting optimism.
- Greeks and implied volatility are not in the historical data. Delta is recovered by inverting Black-Scholes on the traded close, assuming European exercise, a constant 4% risk-free rate and a 1.2% dividend yield.
- Option prices come from Alpaca's INDICATIVE feed, not OPRA. This account is not OPRA-entitled (feed=opra returns 403 'OPRA agreement is not signed'), so the underlying prints are modelled derivatives of the real tape, delayed 15 minutes.
- Assignment, early exercise and pin risk are not modelled; expiring spreads are cash settled at intrinsic value.
- Any position still open at the end of the window is marked at its full defined max loss, which is conservative rather than realistic.
- Live/backtest exit divergence (documented, conservative-neutral): the live agent closes any structure still open on its expiry day at exits.close_at_dte_time ET with one mleg order; this daily-bar backtest cannot model an intraday close and settles at intrinsic instead. Take-profit and stop-loss use identical arithmetic in both (agent/structures.evaluate_exit == Engine.manage).
- Macro blackout: entries are blocked when a listed event falls in [date, date + max_dte]; the default list is FOMC only (run.py --blackout). CPI/NFP dates in data/events.csv are rule-based approximations and were not adopted (see CHANGELOG 2026-08-28).
- Daily option closes in this store cannot support a Black-Scholes P&L decomposition: recovered IV of a single contract moves a median ~1.7 vol points a day (real SPY is 0.5-1.5). The convexity-share gate is not computable here. Each run reports direction.json (P&L on up vs down underlying moves) instead. Do not copy a textbook hedge_gain or quadratic underlying impact -- this book does not trade the underlying, and option-spread friction is already in fills.py.

## Data fingerprint

```json
{
  "provider": "alpaca",
  "access_method": "alpaca_cli",
  "underlying": "SPY,QQQ,IWM,GLD,XLF,SMH,USO,IEF,UNG,TLT,ASHR,XLE,XLU,IBIT,XLV,SLV",
  "underlyings": [
    "SPY",
    "QQQ",
    "IWM",
    "GLD",
    "XLF",
    "SMH",
    "USO",
    "IEF",
    "UNG",
    "TLT",
    "ASHR",
    "XLE",
    "XLU",
    "IBIT",
    "XLV",
    "SLV"
  ],
  "feed": "sip (underlying) / indicative (options)",
  "adjustment": "split (underlying) -- NOT dividend-adjusted, so it matches option strikes",
  "timeframe": "1Day",
  "window": {
    "start": "2024-01-18",
    "end": "2026-08-28"
  },
  "option_bars_in_window": 2746299,
  "option_close_sum": 17820899.2399,
  "option_first_ts": "2024-01-18",
  "option_last_ts": "2026-08-28",
  "underlying_bars_in_window": 10304,
  "underlying_close_sum": 1747224.135
}
```

---

Generated by `backtest/evidence.py`. DSR: Bailey & Lopez de Prado. PBO/CSCV: Bailey, Borwein, Lopez de Prado & Zhu. Reporting structure follows the 2026 audits (arXiv 2605.19337, 2605.16895, 2606.08285, 2603.27539).
