"""Live Pipeline -- an options agent built to refuse.

A read-only page over this repository's committed artifacts. It opens no network
connection, reads no credentials, and cannot reach Alpaca: every figure below is
loaded from a JSON or CSV file that ships in this repo, so the demo cannot drift
from the results it describes.

Run locally:  streamlit run streamlit_app.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
REPO_URL = "https://github.com/nuran/alpaca-options-agent-hackathon"

st.set_page_config(
    page_title="Live Pipeline -- an options agent built to refuse",
    page_icon="🔒",
    layout="wide",
)


# --------------------------------------------------------------------------- loaders
# Every loader degrades to None/empty rather than raising: a run folder missing an
# optional artifact should render a notice, not a traceback.


@st.cache_data
def load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


@st.cache_data
def load_csv(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path)
    except (OSError, ValueError, pd.errors.EmptyDataError):
        return pd.DataFrame()


@st.cache_data
def available_runs() -> list[str]:
    if not RUNS.is_dir():
        return []
    return sorted(p.parent.name for p in RUNS.glob("*/summary.json"))


def pct(x, digits: int = 1) -> str:
    return "n/a" if x is None else f"{x * 100:+.{digits}f}%"


def money(x) -> str:
    return "n/a" if x is None else f"${x:,.0f}"


def num(x, digits: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{digits}f}"


# --------------------------------------------------------------------------- header

st.title("Live Pipeline")
st.subheader("An autonomous options agent built to refuse")

st.markdown(
    "A systematic put-credit-spread desk that ran unattended on Alpaca **paper** "
    "accounts on a 15-minute timer, with no human approving a cycle. A candidate has "
    "to survive seven gates before an order exists, and the single model call sits "
    "sixth -- after every risk check. It classifies news into three enums that "
    "deterministic code turns into ALLOW or PASS; it never sizes, prices or authorises "
    "anything, and every failure mode (no key, transport error, refusal, malformed "
    "JSON, empty feed) resolves to PASS. **The agent degrades to trading nothing, "
    "never to trading without the check.**"
)

st.info(
    "**This page reads only committed files.** No credentials, no API calls, no live "
    "account access — the imports are `json`, `pathlib`, `pandas`, `streamlit` and "
    f"nothing else. [Source]({REPO_URL})",
    icon="🔒",
)

st.caption(
    "For research and educational purposes only. Not investment advice. Backtested "
    "results are hypothetical, do not represent actual trading, and do not guarantee "
    "future results. Paper trading is simulated and may differ materially from live "
    "trading."
)

runs = available_runs()
if not runs:
    st.error("No run folders with a `summary.json` were found under `runs/`.")
    st.stop()

# --------------------------------------------------------------------------- 1. live

st.header("1 · Live, and measured")

live_ab = load_json(ROOT / "data" / "live_ab.json")
if not live_ab:
    st.warning("`data/live_ab.json` not found -- skipping the live section.")
else:
    st.markdown(f"Two paper accounts, **{live_ab['window']}**. {live_ab['note']}")

    cols = st.columns(2)
    for col, acct in zip(cols, live_ab["accounts"]):
        with col:
            st.subheader(f"{acct['label']} · `{acct['account_id']}`")
            st.caption(f"{acct['profile']} · {acct['tenor']}")
            a, b = st.columns(2)
            a.metric("Equity", money(acct["equity"]), pct(acct["return_pct"], 3))
            b.metric("Cycles", f"{acct['cycles']:,}")
            st.dataframe(
                pd.DataFrame(
                    {
                        "": ["spreads submitted", "filled", "unfilled at limit", "open at flat"],
                        " ": [
                            str(acct["spreads_submitted"]),
                            str(acct["filled"]),
                            str(acct["unfilled_at_limit"]),
                            str(acct["open_at_flat"]),
                        ],
                    }
                ),
                hide_index=True,
                width="stretch",
            )

    st.markdown(
        "**The backtest disagrees with the live ranking, and that disagreement is the "
        "most useful thing on this page.** The rows below are read from each account's "
        "own run folder, not restated here."
    )

    rows, bench_row = [], None
    for acct in live_ab["accounts"]:
        run_summary = load_json(RUNS / acct["backtest_run"] / "summary.json") or {}
        bench_row = run_summary.get("benchmarks", {}).get("buy_and_hold") or bench_row
        for entry in run_summary.get("friction_sweep", []):
            rows.append(
                {
                    "account": f"{acct['label']} · {acct['tenor']}",
                    "friction (per leg)": f"{entry['friction_pct'] * 100:.0f}%",
                    "total return": pct(entry.get("total_return")),
                    "Sharpe": num(entry.get("sharpe")),
                    "max drawdown": pct(entry.get("max_drawdown")),
                    "trades": f"{entry.get('trades', 0):,}",
                    "win rate": pct(entry.get("win_rate"), 1).lstrip("+"),
                }
            )
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    if bench_row:
        st.caption(
            f"SPY buy & hold over the same window: {pct(bench_row.get('total_return'))}, "
            f"Sharpe {num(bench_row.get('sharpe'))} — **neither book beats it.**"
        )
    st.success(live_ab["reading"], icon="📈")

# --------------------------------------------------------------------------- 2. backtest

st.header("2 · Backtest")

with st.sidebar:
    st.header("Backtest run")
    run_name = st.selectbox("Run folder", runs, index=0)
    st.caption(
        "Each folder under `runs/` is one self-contained backtest, with its own config, "
        "data fingerprint, trades and warnings."
    )
    st.divider()
    st.markdown(f"[Repository]({REPO_URL})")
    st.caption("Paper only, structurally: no code path in this repo constructs `--live`.")

run_dir = RUNS / run_name
summary = load_json(run_dir / "summary.json") or {}
metrics = summary.get("metrics", {})
bench = summary.get("benchmarks", {}).get("buy_and_hold", {})

st.caption(
    f"`{run_name}` · {summary.get('strategy_name', 'n/a')} · "
    f"{summary.get('start', '?')} to {summary.get('end', '?')} · "
    f"{', '.join(summary.get('symbols', []))}"
)

c = st.columns(5)
c[0].metric("Total return", pct(metrics.get("total_return")))
c[1].metric("Annualised", pct(metrics.get("annualized_return")))
c[2].metric("Sharpe", num(metrics.get("sharpe")))
c[3].metric("Max drawdown", pct(metrics.get("max_drawdown")))
c[4].metric("Trades", f"{metrics.get('trades', 0):,}")

c = st.columns(5)
c[0].metric("Win rate", pct(metrics.get("win_rate"), 1).lstrip("+"))
c[1].metric("Profit factor", num(metrics.get("profit_factor")))
c[2].metric("Final equity", money(metrics.get("final_equity")))
c[3].metric("SPY return", pct(bench.get("total_return")))
c[4].metric("SPY Sharpe", num(bench.get("sharpe")))

equity = load_csv(run_dir / "equity.csv")
benchmark = load_csv(run_dir / "benchmark_equity.csv")
if not equity.empty:
    curve = equity[["date", "equity"]].rename(columns={"equity": "book"})
    if not benchmark.empty:
        curve = curve.merge(
            benchmark.rename(columns={"equity": "SPY buy & hold"}), on="date", how="left"
        )
    curve["date"] = pd.to_datetime(curve["date"])
    st.line_chart(curve.set_index("date"), height=380)
    st.caption("Marked equity curve against SPY buy & hold — same window, same starting cash.")

# --------------------------------------------------------------------------- 3. friction

st.header("3 · The friction sweep")
st.markdown(
    "Alpaca serves **no historical option bid/ask** — `/v1beta1/options/quotes` returns "
    "404, and only `/quotes/latest` exists. Fills are therefore modelled from daily bar "
    "closes plus an assumed friction, not from a real book. This is the single largest "
    "source of error in any result here, so the harness reports a **sweep** rather than "
    "one number: *if a strategy is profitable at 1% and dead at 6%, its edge is smaller "
    "than the spread.*"
)

sweep = summary.get("friction_sweep", [])
if sweep:
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "friction (per leg)": f"{r['friction_pct'] * 100:.0f}%",
                    "total return": pct(r.get("total_return")),
                    "annualised": pct(r.get("annualized_return")),
                    "Sharpe": num(r.get("sharpe")),
                    "max drawdown": pct(r.get("max_drawdown")),
                    "trades": f"{r.get('trades', 0):,}",
                    "win rate": pct(r.get("win_rate"), 1).lstrip("+"),
                }
                for r in sweep
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    st.bar_chart(
        pd.DataFrame(
            {"total return": [r.get("total_return") for r in sweep]},
            index=[f"{r['friction_pct'] * 100:.0f}% per leg" for r in sweep],
        ),
        height=260,
    )
    st.caption(
        "Per-leg friction amplifies on the net credit — a spread is a small difference "
        "between two larger prices, so 3% per leg lands near 9% on the credit."
    )

# --------------------------------------------------------------------------- 4. selection

st.header("4 · Does it survive selection?")
st.markdown(
    "A backtest that reports the best of many configurations is reporting the maximum "
    "of a search, not an edge. Both numbers belong on the same page."
)

evidence = load_json(ROOT / "evidence.json")
if not evidence:
    st.warning("`evidence.json` not found.")
else:
    d = evidence.get("dsr", {})
    m = st.columns(5)
    m[0].metric("Deflated Sharpe", num(d.get("dsr"), 3), "bar is 0.95", delta_color="off")
    m[1].metric("Observed Sharpe", num(d.get("sharpe")))
    m[2].metric("Benchmark SR*", num(d.get("sr_star"), 3))
    m[3].metric("Trials evaluated", f"{d.get('n_trials', 0):,}")
    m[4].metric("PBO", num(evidence.get("pbo", {}).get("pbo"), 3))

    st.markdown(
        f"**{d.get('n_trials', 0)} configurations were evaluated and the best was "
        f"reported.** The expected maximum of that many trials (SR\\* "
        f"{num(d.get('sr_star'), 2)}) sits just below the observed Sharpe "
        f"{num(d.get('sharpe'), 2)}, so the deflated Sharpe of "
        f"**{num(d.get('dsr'), 3)}** does not clear the 0.95 bar. Read together with a "
        f"PBO of {num(evidence.get('pbo', {}).get('pbo'), 2)}: the *ranking* of "
        "configurations generalises more often than not; the *level* of the Sharpe is "
        "explained by the search."
    )

    rolling = evidence.get("rolling", [])
    if rolling:
        st.subheader("Rolling three-month windows")
        roll = pd.DataFrame(rolling)
        roll["end"] = pd.to_datetime(roll["end"])
        left, right = st.columns(2)
        with left:
            st.caption("Rolling window return")
            st.bar_chart(roll.set_index("end")[["return"]], height=260)
        with right:
            st.caption("Rolling window Sharpe")
            st.bar_chart(roll.set_index("end")[["sharpe"]], height=260)
        negative = int((roll["return"] < 0).sum())
        st.caption(
            f"{len(roll)} overlapping windows, **{negative} of them negative**. The "
            "strategy is regime-dependent, not uniformly profitable — a single headline "
            "annualised number hides exactly this."
        )

    variants = evidence.get("variants", {})
    if variants:
        st.subheader("The variants the champion was chosen from")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "variant": name,
                        "total return": pct(v.get("total_return")),
                        "annualised": pct(v.get("annualized_return")),
                        "Sharpe": num(v.get("sharpe")),
                        "max drawdown": pct(v.get("max_drawdown")),
                        "trades": f"{v.get('trades', 0):,}",
                        "win rate": pct(v.get("win_rate"), 1).lstrip("+"),
                    }
                    for name, v in variants.items()
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        st.caption(f"Champion: `{evidence.get('champion', 'n/a')}`")

st.info(
    'Believe *"selling short-dated defined-risk put spreads gated on IV/RV earns a '
    'premium"* more than you believe *"the Sharpe of this configuration is 1.36"*.',
    icon="⚖️",
)

# --------------------------------------------------------------------------- 5. trades

st.header("5 · Trades")

trades = load_csv(run_dir / "round_trips.csv")
if trades.empty:
    st.warning("`round_trips.csv` not found for this run.")
else:
    f = st.columns(3)
    names = sorted(trades["underlying"].dropna().unique().tolist())
    picked = f[0].multiselect("Underlying", names, default=names)
    reasons = sorted(trades["exit_reason"].dropna().unique().tolist())
    picked_reasons = f[1].multiselect("Exit reason", reasons, default=reasons)
    dte_lo, dte_hi = int(trades["dte_at_entry"].min()), int(trades["dte_at_entry"].max())
    dte = f[2].slider("DTE at entry", dte_lo, dte_hi, (dte_lo, dte_hi))

    view = trades[
        trades["underlying"].isin(picked)
        & trades["exit_reason"].isin(picked_reasons)
        & trades["dte_at_entry"].between(*dte)
    ]

    m = st.columns(4)
    m[0].metric("Trades shown", f"{len(view):,}")
    m[1].metric("Net P&L", money(view["pnl"].sum()) if len(view) else "n/a")
    m[2].metric("Win rate", pct((view["pnl"] > 0).mean(), 1).lstrip("+") if len(view) else "n/a")
    m[3].metric("Worst trade", money(view["pnl"].min()) if len(view) else "n/a")

    if len(view):
        a, b = st.columns(2)
        with a:
            st.caption("P&L by exit reason")
            st.bar_chart(view.groupby("exit_reason")["pnl"].sum(), height=260)
        with b:
            st.caption("P&L by underlying")
            st.bar_chart(view.groupby("underlying")["pnl"].sum(), height=260)

        st.caption("P&L distribution")
        hist = view.groupby(pd.cut(view["pnl"], bins=40), observed=True)["pnl"].count()
        hist.index = [f"{int(i.left):,}" for i in hist.index]
        st.bar_chart(hist, height=240)
        st.caption(
            "The shape that matters: many small credits kept, and a thin left tail of "
            "spreads that went the wrong way. A short put vertical's worst case is "
            "capped by construction, which is what lets it be sized exactly."
        )

        cols = [
            "entry_date", "exit_date", "underlying", "dte_at_entry", "held_days",
            "short_strike", "long_strike", "width", "qty", "credit", "exit_debit",
            "short_delta", "iv_rv_ratio", "max_loss", "pnl", "return_on_risk",
            "exit_reason",
        ]
        st.dataframe(
            view[[c for c in cols if c in view.columns]].sort_values("entry_date"),
            hide_index=True,
            width="stretch",
            height=340,
        )

# --------------------------------------------------------------------------- 6. economics

st.header("6 · Where the P&L actually comes from")

econ = summary.get("economics", {})
if econ:
    m = st.columns(4)
    m[0].metric("Credit collected", money(econ.get("credit_collected_dollars")))
    m[1].metric("Net P&L", money(econ.get("pnl_dollars")))
    m[2].metric("Breakeven win rate", pct(econ.get("breakeven_winrate"), 2).lstrip("+"))
    m[3].metric("Actual win rate", pct(econ.get("actual_winrate"), 2).lstrip("+"),
                pct(econ.get("winrate_gap"), 2))

    st.markdown(
        "A 90% win rate is not the achievement it looks like: this credit-to-width ratio "
        "*requires* roughly that rate simply to break even. The gap between the two is "
        "the entire edge."
    )

    by_reason = econ.get("by_exit_reason", {})
    if by_reason:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "exit reason": reason,
                        "trades": f"{v.get('n', 0):,}",
                        "P&L": money(v.get("pnl_dollars")),
                        "average": money(v.get("avg_pnl")),
                        "worst": money(v.get("worst_pnl")),
                    }
                    for reason, v in by_reason.items()
                ]
            ),
            hide_index=True,
            width="stretch",
        )

    tail = econ.get("tail")
    if tail:
        st.subheader("The tail")
        m = st.columns(4)
        m[0].metric("Tail trades", f"{tail.get('n', 0):,}")
        m[1].metric("Share of trades", pct(tail.get("share_of_trades"), 1).lstrip("+"))
        m[2].metric("Share of gross loss", pct(tail.get("share_of_gross_loss"), 1).lstrip("+"))
        m[3].metric("Tail P&L", money(tail.get("pnl_dollars")))
        st.warning(
            f"Defined as {tail.get('threshold', 'n/a')}. A few percent of trades carry "
            "the overwhelming majority of the losses — which is the risk being paid for, "
            "not an anomaly.",
            icon="⚠️",
        )

direction = summary.get("direction", {})
if direction:
    st.caption(
        f"Direction check — P&L on up moves {money(direction.get('pnl_on_up_moves'))} "
        f"across {direction.get('trades_up', 0):,} trades, on down moves "
        f"{money(direction.get('pnl_on_down_moves'))} across "
        f"{direction.get('trades_down', 0):,}. Verdict: *{direction.get('verdict', 'n/a')}*."
    )

# --------------------------------------------------------------------------- 7. caveats

st.header("7 · What this cannot tell you")
st.markdown(
    "Every assumption below is recorded in the run's own `summary.json` and reproduced "
    "verbatim. They are the reason the friction sweep exists."
)

for i, item in enumerate(summary.get("assumptions", []), 1):
    st.markdown(f"**{i}.** {item}")

for item in summary.get("warnings", []):
    st.warning(item, icon="⚠️")

fingerprint = summary.get("data_fingerprint")
if fingerprint:
    with st.expander("Data fingerprint — exactly what was read"):
        st.json(fingerprint)

st.divider()
st.caption(
    "Built for the lablab.ai × Alpaca AI Trading Agents Hackathon. Source and full "
    f"methodology: {REPO_URL} · MIT licensed · paper trading only."
)
