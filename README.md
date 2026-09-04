# Alpaca Options Agent

An autonomous options-trading agent built on Alpaca's Trading API and CLI. It runs on a
`launchd` timer, decides on its own, and trades **defined-risk put credit spreads** on
short-dated ETF options. **Paper only — no code path in this repository constructs `--live`.**

Submitted to the [lablab.ai × Alpaca AI Trading Agents Hackathon](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon),
4 September 2026.

---

## 1. The accounts

Two paper accounts run the same agent with different rulebooks, so the effect of a single
parameter — the tenor — is directly observable rather than argued about.

| | Account C — **submission** | Account B — second book |
|---|---|---|
| Account | `PA3UU4TX8Y3K` | `PA3ZNXHF8ID1` |
| Rulebook | [`agent/profiles/putcr-core6-d47/`](agent/profiles/putcr-core6-d47/rules.json) | [`agent/profiles/putcr-core6/`](agent/profiles/putcr-core6/rules.json) |
| Tenor | DTE 4–7 | DTE 1–7 |
| Started at | $100,000.00 | $100,000.00 |
| Equity | **$100,342.80 (+0.343%)** | $100,277.20 (+0.277%) |
| Cycles run | 242 | 131 |
| Spreads submitted | 8 (2 filled, 6 expired unfilled at the limit) | 7 (all filled and closed) |
| Flat-by date | 2026-09-18 | 2026-09-03 (reached — book closed out) |
| Account created | **2026-03-26** | 2026-08-27 |

**On account age, stated up front rather than left to be found.** The rules ask for a
brand-new account dedicated to the hackathon. Account C's `created_at` is 2026-03-26,
five months before kickoff; its equity read exactly $100,000.00 when trading began on
1 September, which is the signature of an Alpaca paper reset — a reset restores the
balance and keeps the original creation timestamp. Account B was created on 2026-08-27,
the day before kickoff, and is unambiguously new. Both books are in this repository and
either can be judged; if C's provenance disqualifies it, B is the same agent on an
account with no such question.

Account B's book reached its flat-by date on 3 September and closed itself out, which is
why it holds nothing and why its seven spreads are all closed. Account C runs to
18 September and still carries one open spread.

Everything below applies to both books. They differ in exactly two fields: `min_dte` and
the length of the underlying list.

---

## 2. AI logic — what the model does, and what it cannot do

One LLM call per cycle, and its output is an enum, not a trade.

```
0. MARKET GATE   is the market open? enough time before close?     no model
1. OBSERVE       account · positions · orders · chain · news       Alpaca CLI
2. RISK GATE     halts · forced exits · capacity                   no model
                 ── candidates generated and risk-checked here ──
3. CLASSIFY      one LLM call, strict JSON, PASS on any doubt      model
4. VALIDATE      re-check the pick against every rule              no model
5. EXECUTE       marketable limit via `alpaca order submit`        no model
6. JOURNAL       every input, rationale, order and fill
```

The model receives the DTE, the short strikes (for a distance test) and the filtered
headlines. It returns three enums — `catalyst_scope`, `jump_severity`, `news_clarity`.
Deterministic code in [`agent/veto.py`](agent/veto.py) turns those into ALLOW/PASS per
underlying, and code ranks and picks the surviving candidate.

The model **never sees** thresholds, position sizing, P&L, or the legs of a spread. It
cannot size a position, raise a limit price, or skip a gate. Every failure mode — no API
key, transport error, refusal, malformed JSON, an empty news feed — resolves to PASS. The
agent degrades to *trade nothing*, never to *trade without the check*.

That is a deliberate response to a documented failure in this literature: FinAgent's
analysis of FinMem records it "ignored the long-term downward trend and provided a buying
rationale" from two headlines. A model that can only accept or reject from a
pre-risk-checked list cannot make that class of mistake, however confident its reasoning.

---

## 3. Risk gates

Every one of these is code, runs before the model, and is covered by a test that forces
the failure and asserts it never becomes a trade.

| Gate | Rule |
|---|---|
| Structure | Defined risk always — both legs, every time. Max loss is known before entry, so sizing is exact rather than estimated. |
| Size | 2% of equity per trade, ≤ 20 contracts, vol-target scaling on 21-session realised vol (0.25×–2.0×) |
| Concentration | 3 concurrent structures, 1 per underlying |
| Halts | −4% daily, −10% drawdown, both on the **marked** curve. A breach stops new entries and requires a human to clear `decisions/state.json`. It will not restart itself. |
| Execution | 8%-of-mid spread cap; 300 s quote-age cap; limit orders only, never market — indicative quotes are modelled, not a real book |
| Exits | Take profit at 50% of credit; stop at 2× credit; close on expiry day at 15:00 ET (American, physically settled — never held through settlement) |
| Calendar | No new entries while an FOMC date falls inside `[today, today + max_dte]`; flat-by date clamps the chain window so nothing is opened that cannot be held to expiry |
| Account | `assert_account` refuses to start when the credentials reach a different account than the rulebook names — the reason two books can run side by side without silently sharing one account |
| Orders | Every order carries a `client_order_id`; Alpaca rejects duplicates, which is what makes a retry safe rather than double-filling |
| Live trading | Structurally impossible. Not a setting to be careful with — the code path does not exist. |

`make test` runs 88 tests — 20 on the backtest engine, 68 failure-injection tests on the
agent — plus 17 module self-checks. They cover assignment reconciliation, orphan legs,
stale quotes, malformed model output, lost order ids, and the drawdown halt. Every one of
them forces a failure that would otherwise appear only live, and asserts it never becomes
a trade.

---

## 4. Alpaca infrastructure

The agent talks to Alpaca through the **CLI**, as the rules require.

| Surface | Used for |
|---|---|
| `alpaca order submit` (multi-leg `mleg`) | Both legs as one atomic package — a spread is one position, not two |
| `alpaca data option chain` | Live chains with Alpaca's own Greeks and IV, so no Black-Scholes inversion in the live path |
| `alpaca clock` | The market gate — half-days and holidays come from Alpaca rather than a hardcoded calendar |
| `alpaca data news` | The tape the news veto classifies |
| `alpaca position list` / `order list` | Reconciliation every cycle: what the agent believes it holds versus what the broker says |
| `alpaca account get` | Equity, buying power, options level, and the account assertion |
| Historical bars API | 1.66 M option bars into DuckDB for the backtest |

Scheduling is a local `launchd` timer ([`agent/com.nuka.alpaca-agent-c.plist`](agent/com.nuka.alpaca-agent-c.plist)),
firing every 30 minutes. The agent's own market gate exits immediately outside regular
hours, so the calendar lives at the broker, not in the timer.

---

## 5. What the backtest found

Window 2024-01-22 → 2026-08-26, six underlyings, 1,661,101 option bars, net of modelled
fees and friction. **Read the sweep, not the headline.** There is no historical option
bid/ask on this venue, so the fill assumption is the single largest source of error;
`backtest/fills.py` explains why. A book that is only positive at 1% is not a book.

**Account B's tenor (DTE 1–7)** — positive across the whole sweep:

| friction | return | Sharpe | max DD | trades | win rate |
|---|---:|---:|---:|---:|---:|
| 1% per leg | +16.62% | 0.68 | −13.82% | 233 | 88.4% |
| **3% per leg** | **+11.51%** | **0.51** | **−14.92%** | 233 | 87.1% |
| 6% per leg | +2.75% | 0.16 | −14.45% | 232 | 86.2% |

**Account C's tenor (DTE 4–7)** — positive only at the most optimistic friction:

| friction | return | Sharpe | max DD | trades | win rate |
|---|---:|---:|---:|---:|---:|
| 1% per leg | +18.23% | 0.86 | −8.91% | 317 | 80.8% |
| **3% per leg** | **−3.79%** | **−0.14** | **−12.96%** | 312 | 76.9% |
| 6% per leg | −25.11% | −1.21 | −28.23% | 325 | 71.7% |

*SPY buy-and-hold over the same window: +58.19%, Sharpe 1.21. Neither book beats it.*

Both sweeps run with the drawdown halt **disabled**, and live it is armed at 10%. That is
deliberate and it cuts against the numbers rather than for them: a halt fires once and
stops the run, which truncates the sample and makes two configurations incomparable. The
question a backtest answers is whether an edge survives friction, not when it would have
switched itself off. With the halt armed, account C's book stops after 95 trades.

Reproduce both tables exactly with `make restore && make sweep`; artifacts land in `runs/`.

---

## 6. What the backtest cannot tell you

This section is the point of the project, not an apology for it.

- **The submission account's book does not survive the friction sweep.** C measures −3.79%
  at 3% and −25.11% at 6%. Its live P&L is positive, but three days of paper trading on
  two filled spreads is noise, not evidence. B's book is the one that survives.
- **Half of account C's universe was never backtested.** C's rulebook lists twelve
  underlyings. Six — USO, SLV, TLT, IBIT, UNG, XLV — have **no option-bar history at
  all** in the store, so no measurement covers them. The tables above are the other six.
- **Three of those six cannot actually be traded.** Measured on live quotes, GLD, XLF and
  SMH show 18–80% bid-ask spreads and admit *zero* in-band contracts through the 8% cap.
  A backtest with no historical bid/ask cannot see this; only the live agent's decline log
  did. The tradeable universe is really SPY, QQQ and IWM.
- **Selection is not free.** The parameters here came from a grid search.
  [`EVIDENCE.md`](EVIDENCE.md) records the multiple-testing analysis for the development
  repo's champion configuration: **deflated Sharpe 0.563**, which does *not* clear the
  conventional 0.95 bar, and **PBO 0.34**. Read together: the *ranking* of configurations
  generalises more often than not; the *level* of the Sharpe is explained by the search.
  That analysis covers a different (16-name) configuration than the two books here — the
  two books' own evidence is the friction sweep above and nothing more.
- **A −5% overnight gap costs a third of the account.** Short put spreads have a
  closed-form worst case and it was computed, not assumed (`backtest/tail_test.py`). The
  realised drawdowns above contain no such gap.

---

## 7. Setup

```bash
make setup                          # venv + dependencies
brew install alpacahq/tap/cli       # the hackathon mandates CLI or MCP; this uses the CLI
cp .env.example .env                # then add your paper keys
make restore                        # rebuild the market store from committed Parquet, offline
make test                           # 88 tests + 17 self-checks, no network, no keys
make sweep                          # reproduce both tables in section 5
make preflight-c                    # verify every live dependency for account C
make shadow-c                       # one full cycle, every order dry-run
make live-c                         # one cycle against the paper broker
```

`make restore` rebuilds `data/market.duckdb` from `data/export/*.parquet` in about ten
seconds with no network. That round trip is what makes the numbers above reproducible
without re-downloading two and a half years of option bars.

## 8. Layout

| Path | What |
|---|---|
| `agent/agent_loop.py` | The decision loop — market gate, observe, risk gate, classify, validate, execute, journal |
| `agent/veto.py` | News classification schema and the deterministic ALLOW/PASS veto |
| `agent/structures.py` | Structure-level P&L, broker reconciliation, atomic closes |
| `agent/preflight.py` | 23 live-dependency checks; run before trading |
| `agent/profiles/*/rules.json` | One rulebook per account, with the reasoning for every threshold inline |
| `agent/profiles/*/decisions/` | The live journals — every cycle, every gate, every order |
| `backtest/` | `engine.py` · `strategy.py` · `fills.py` · `store.py` · `run.py` and the statistics |
| `data/restore.py` | Rebuild the DuckDB store from the committed Parquet |
| `skills/options-core-patterns/` | The written rules the code enforces (no European index products, VIX regime, close before expiry) |
| `runs/` | One self-contained folder per backtest, with full provenance |

Each rulebook carries `_why` fields next to its numbers. They are the reasoning behind
each threshold, kept beside the value so a later reader cannot change one without seeing
the other.

## Disclosure

For research and educational purposes only. Not investment advice. Backtested results are
hypothetical, do not represent actual trading, and do not guarantee future results. Paper
trading is simulated and may differ materially from live trading. Options trading is not
suitable for all investors; read [Characteristics and Risks of Standardized Options](https://www.theocc.com/company-information/documents-and-archives/options-disclosure-document)
before investing. See [alpaca.markets/disclosures](https://alpaca.markets/disclosures).

MIT licensed — see [LICENSE](LICENSE).
