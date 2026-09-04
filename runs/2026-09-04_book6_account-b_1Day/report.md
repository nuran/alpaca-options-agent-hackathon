# book6 short-dated credit spreads -- backtest report

**Window:** 2024-01-22 -> 2026-08-26  ·  **Timeframe:** 1Day  ·  **Friction:** 3% per leg

| | Total Return | Ann. Return | Max Drawdown | Sharpe | Final Equity |
|---|---:|---:|---:|---:|---:|
| **Strategy** | 11.51% | 4.30% | -14.92% | 0.51 | $111,506 |
| book6 buy & hold | 58.19% | 19.39% | -18.94% | 1.21 | $158,187 |

## Friction sweep

The result at several execution-cost assumptions. Alpaca serves no historical
option bid/ask, so friction is an assumption, not a measurement -- read the row
that matches what you believe execution actually costs, and note where the edge
disappears.

| Friction / leg | Total Return | Trades | Win rate | Profit factor | Max DD | Sharpe |
|---:|---:|---:|---:|---:|---:|---:|
| 1% | 16.62% | 233 | 88.4% | 1.33 | -13.82% | 0.68 |
| 3% | 11.51% | 233 | 87.1% | 1.24 | -14.92% | 0.51 |
| 6% | 2.75% | 232 | 86.2% | 1.06 | -14.45% | 0.16 |

## Trade statistics

- Trades: **233** (203 wins / 30 losses)
- Win rate: **87.1%**
- Profit factor: **1.24**
- Average trade: **$49.38**  (win $288.41 / loss $1,568.01)
- Expectancy per trade: **$49.38**
- Largest win / loss: $709.53 / $-3,668.80
- Longest drawdown: 401 sessions

## Direction (no greeks)

P&L split by whether the underlying rose or fell between entry and exit.
Daily closes in this store cannot support a Greek decomposition (asynchronous IV);
this split is the measurement that survives. It does not assume a hedge in the
underlying.

| | P&L | trades |
|---|---:|---:|
| underlying up | $40,369 | 139 |
| underlying down | $-28,863 | 94 |

Asymmetry **17%** — both sides contribute.

## Economics (winrate gap)

Stable statistics at n≈100: credit leftover, breakeven vs actual winrate,
exit mix, fat-tail share. Do not pick parameters by P&L on this sample.

- Credit collected: **$72,567**; P&L left: **$11,506** (15.9% of credit)
- Mean credit/width: **0.086** → breakeven WR **91.4%**; actual **87.1%**; gap **-4.3%**
- Tail (|pnl| > half max_loss): **12** trades, $-35,587

| exit | n | P&L | avg | worst |
|---|---:|---:|---:|---:|
| expiry | 186 | $11,869 | $64 | $-3,669 |
| stop_loss | 10 | $-9,366 | $-937 | $-3,170 |
| take_profit | 37 | $9,003 | $243 | $81 |

## Data

- Option bars: 1,661,101 (2024-01-22 -> 2026-08-26)
- Distinct contracts: 229,839 across 644 expiries
- Underlying bars: 4,486

## How to read this

Every number above is conditional on assumptions that cannot be verified from
the available data. In order of how much they matter:

- No historical option bid/ask exists: Alpaca serves no historical options quotes endpoint (/v1beta1/options/quotes returns 404, verified 2026-08-25). Entry and exit prices are derived from daily bar closes with a modelled friction, NOT from a real book. This is the single largest source of error in these results.
- Fills are same-bar: a signal from day T's close is filled at day T's close with friction applied against the trade. The vendored skill flags same_bar as carrying look-ahead risk. It is used because daily bars are the only option history available; the friction sweep bounds the resulting optimism.
- Greeks and implied volatility are not in the historical data. Delta is recovered by inverting Black-Scholes on the traded close, assuming European exercise, a constant 4% risk-free rate and a 1.2% dividend yield.
- Option prices come from Alpaca's INDICATIVE feed, not OPRA. This account is not OPRA-entitled (feed=opra returns 403 'OPRA agreement is not signed'), so the underlying prints are modelled derivatives of the real tape, delayed 15 minutes.
- Assignment, early exercise and pin risk are not modelled; expiring spreads are cash settled at intrinsic value.
- Any position still open at the end of the window is marked at its full defined max loss, which is conservative rather than realistic.
- Live/backtest exit divergence (documented, conservative-neutral): the live agent closes any structure still open on its expiry day at exits.close_at_dte_time ET with one mleg order; this daily-bar backtest cannot model an intraday close and settles at intrinsic instead. Take-profit and stop-loss use identical arithmetic in both (agent/structures.evaluate_exit == Engine.manage).
- Macro blackout: entries are blocked when a listed event falls in [date, date + max_dte]; the default list is FOMC only (run.py --blackout). CPI/NFP dates in data/events.csv are rule-based approximations and were not adopted (see CHANGELOG 2026-08-28).
- Daily option closes in this store cannot support a Black-Scholes P&L decomposition: recovered IV of a single contract moves a median ~1.7 vol points a day (real SPY is 0.5-1.5). The convexity-share gate is not computable here. Each run reports direction.json (P&L on up vs down underlying moves) instead. Do not copy a textbook hedge_gain or quadratic underlying impact -- this book does not trade the underlying, and option-spread friction is already in fills.py.

## Disclosure

> **Important disclosure**
> This backtest is a hypothetical historical simulation and does not represent actual
> trading performance. Backtested results do not guarantee future results. Results depend
> on market-data quality, data feed selection, corporate-action handling, fees, slippage,
> liquidity, taxes, execution assumptions, and implementation details. This material is for
> research and educational purposes only and is not investment advice, a recommendation, an
> offer, or a solicitation to buy or sell securities, options, cryptocurrencies, or any
> other financial product. All investments involve risk and may lose value. Review Alpaca's
> disclosures at [alpaca.markets/disclosures](https://alpaca.markets/disclosures).
>
> Paper trading is a simulated environment. It does not involve real money or actual
> securities transactions. Paper results may differ from live trading because of fill
> assumptions, market impact, liquidity, latency, data differences, order handling, fees,
> and other market conditions.
>
> Options trading is not suitable for all investors due to its inherent high risk, which can
> potentially result in significant losses. Please read [Characteristics and Risks of
> Standardized Options](https://www.theocc.com/company-information/documents-and-archives/options-disclosure-document)
> before investing in options.
