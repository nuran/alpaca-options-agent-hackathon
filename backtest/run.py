"""
Run a backtest and write a self-contained, reproducible run folder.

Artifacts follow the vendored alpaca-trading-backtest skill's layout so a run here is
comparable to one produced by that skill:

    runs/<date>_<symbol>_<strategy>_<timeframe>/
        notes.md  strategy_spec.json  config.json
        summary.json  report.md
        trades.csv  equity.csv  round_trips.csv  direction.json  economics.json
        data_fingerprint.json  warnings.json  fee_source.json

Usage:
    python3 backtest/run.py [--underlying SPY] [--shortlist data/shortlist.csv]
                            [--buckets CORE] [--start D] [--end D]
                            [--friction 0.03] [--sweep] [--width 5] [--delta 0.20]
                            [--side put|call|both] [--risk-pct 0.02] [--concurrent 3]
                            [--structure condor|vertical|adaptive] [--iv-rv 1.2|none]
                            [--cash 100000] [--label NAME] [--db PATH]

    --shortlist  trade every skill-safe name in the CSV that has option bars in the store.
    --buckets    CORE or CORE,EXTENDED (only with --shortlist). Default CORE.

    --sweep   run the whole backtest at 1%, 3% and 6% friction and report all three.
              This is the honest way to read the result -- see fills.py for why.

Full instructions: see RUNBOOK.md.
"""
import csv
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')), 'data'))

import attribution as ATTR
import economics as ECO
import fills
import metrics
import shortlist as SL
from engine import Engine
from store import Store

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
RUNS_DIR = os.path.join(REPO_ROOT, 'runs')

FEE_SOURCE = {
    'url': 'https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf',
    'revision_date': 'unknown -- not re-fetched for this run',
    'modeled_categories': ['OCC clearing', 'ORF', 'SEC Section 31 (sells)', 'FINRA TAF (sells)'],
    'excluded_categories': [
        'commissions (Alpaca charges $0 on options)',
        'ADR pass-through (not applicable to index ETFs)',
        'borrow / margin interest (defined-risk spreads post no borrow)',
        'exercise & assignment handling beyond the modeled per-contract fee',
        'taxes',
    ],
}

DISCLOSURE = """> **Important disclosure**
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
> before investing in options."""

METHOD_CAVEATS = [
    "No historical option bid/ask exists: Alpaca serves no historical options quotes "
    "endpoint (/v1beta1/options/quotes returns 404, verified 2026-08-25). Entry and exit "
    "prices are derived from daily bar closes with a modelled friction, NOT from a real "
    "book. This is the single largest source of error in these results.",
    "Fills are same-bar: a signal from day T's close is filled at day T's close with "
    "friction applied against the trade. The vendored skill flags same_bar as carrying "
    "look-ahead risk. It is used because daily bars are the only option history available; "
    "the friction sweep bounds the resulting optimism.",
    "Greeks and implied volatility are not in the historical data. Delta is recovered by "
    "inverting Black-Scholes on the traded close, assuming European exercise, a constant "
    "4% risk-free rate and a 1.2% dividend yield.",
    "Option prices come from Alpaca's INDICATIVE feed, not OPRA. This account is not OPRA-"
    "entitled (feed=opra returns 403 'OPRA agreement is not signed'), so the underlying "
    "prints are modelled derivatives of the real tape, delayed 15 minutes.",
    "Assignment, early exercise and pin risk are not modelled; expiring spreads are cash "
    "settled at intrinsic value.",
    "Any position still open at the end of the window is marked at its full defined max "
    "loss, which is conservative rather than realistic.",
    "Live/backtest exit divergence (documented, conservative-neutral): the live agent closes any "
    "structure still open on its expiry day at exits.close_at_dte_time ET with one mleg order; this "
    "daily-bar backtest cannot model an intraday close and settles at intrinsic instead. Take-profit "
    "and stop-loss use identical arithmetic in both (agent/structures.evaluate_exit == Engine.manage).",
    "Macro blackout: entries are blocked when a listed event falls in [date, date + max_dte]; the "
    "default list is FOMC only (run.py --blackout). CPI/NFP dates in data/events.csv are rule-based "
    "approximations and were not adopted (see CHANGELOG 2026-08-28).",
    "Daily option closes in this store cannot support a Black-Scholes P&L decomposition: recovered "
    "IV of a single contract moves a median ~1.7 vol points a day (real SPY is 0.5-1.5). The "
    "convexity-share gate is not computable here. Each run reports direction.json (P&L on up vs "
    "down underlying moves) instead. Do not copy a textbook hedge_gain or quadratic underlying "
    "impact -- this book does not trade the underlying, and option-spread friction is already in fills.py.",
]


# Every flag this harness accepts. Without the check below a typo -- or a flag that was
# never plumbed -- lands in `o`, is never read, and the run silently uses the default.
# `--har-horizon` did exactly that on 2026-08-29: four runs came back byte-identical to
# the previous four, which would have read as "matching the forecast horizon changes
# nothing" rather than "the flag does nothing".
KNOWN_OPTS = {
    'underlying', 'underlyings', 'start', 'end', 'cash', 'friction', 'label', 'db',
    'min_dte', 'max_dte', 'max_dte_s3', 'width', 'width_pct', 'width_min', 'wing_sigmas',
    'delta', 'delta_tolerance', 'target_delta', 'iv_rv', 'rv_window', 'vol_gate_iv',
    'structure', 'side', 'risk_pct', 'concurrent', 'max_concurrent', 'max_contracts',
    'max_per_name', 'max_portfolio_risk', 'equity_basis',
    'vol_target', 'vol_window', 'vol_scale_min', 'vol_scale_max', 'risk_alloc',
    'min_credit_ratio', 'min_credit_ratio_vertical', 'max_credit_ratio',
    'drawdown_halt', 'daily_loss_halt', 'take_profit', 'stop_loss', 'close_at_dte',
    'events', 'blackout', 'flat_by', 'time_basis', 'har_horizon', 'out',
    'max_overnight_share', 'min_iv_har_ratio', 'shortlist', 'buckets',
}


def _friction(raw):
    """A number for the sweep, or the calibrated book model."""
    if str(raw).strip().lower() in ('measured', 'live', 'calibrated'):
        import fills
        return fills.MeasuredSpread()
    return float(raw)


def parse_args(argv):
    o = {'sweep': False}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == '--sweep':
            o['sweep'] = True; i += 1
        elif a.startswith('--'):
            name = a[2:].replace('-', '_')
            if name not in KNOWN_OPTS:
                near = sorted(k for k in KNOWN_OPTS
                              if k.replace('_', '') == name.replace('_', ''))
                print(f"ERROR: unknown option: {a}"
                      + (f" -- did you mean --{near[0].replace('_', '-')}?" if near else ""))
                sys.exit(1)
            if i + 1 >= len(argv):
                print(f"ERROR: {a} requires a value"); sys.exit(1)
            o[name] = argv[i + 1]; i += 2
        else:
            print(f"ERROR: unrecognized argument: {a}"); sys.exit(1)
    return o


def build_config(o, friction, names=None):
    und = o.get('underlying', 'SPY').upper()
    names = names or [und]
    return {
        'underlying': names[0],
        'underlyings': names,
        'initial_cash': float(o.get('cash', 100_000)),
        'min_dte': int(o.get('min_dte', 1)),
        'max_dte': int(o.get('max_dte', 7)),
        'width': float(o.get('width', 5.0)),
        'width_pct': _optional_float(o.get('width_pct')),
        'width_min': float(o.get('width_min', 1.0)),
        'target_delta': float(o.get('delta', 0.20)),
        'delta_tolerance': float(o.get('delta_tolerance', 0.10)),
        'min_credit_ratio': float(o.get('min_credit_ratio', 0.15)),
        'min_credit_ratio_vertical': float(o.get('min_credit_ratio_vertical', 0.08)),
        'max_credit_ratio': float(o.get('max_credit_ratio', 0.60)),
        'side': o.get('side', 'both'),
        'structure': o.get('structure', 'condor'),
        'condor_vs_put_ratio': float(o.get('condor_vs_put_ratio', 0.90)),
        'min_iv_rv_ratio': _optional_float(o.get('iv_rv', '1.2')),
        'rv_window': int(o.get('rv_window', 21)),
        'max_risk_per_trade_pct': float(o.get('risk_pct', 0.02)),
        # `--concurrent` was in build_config but NOT in KNOWN_OPTS, and `--max-concurrent`
        # was in KNOWN_OPTS but never read: the first errored out, the second landed in
        # `o` and was silently ignored. Concurrency had therefore never been varied in
        # any run in this repo -- exactly the failure the KNOWN_OPTS comment above was
        # written about. Both spellings now reach the engine.
        'max_concurrent': int(o.get('concurrent', o.get('max_concurrent', 3))),
        'max_per_name': int(o.get('max_per_name', 1)),
        'max_portfolio_risk_pct': (None if o.get('max_portfolio_risk') in (None, '', 'none', 'null')
                                   else float(o['max_portfolio_risk'])),
        'equity_basis': o.get('equity_basis', 'cash'),
        'risk_alloc': o.get('risk_alloc', 'per_trade'),
        'vol_target_pct': _optional_float(o.get('vol_target')),
        'vol_window': int(o.get('vol_window', 21)),
        'vol_scale_min': float(o.get('vol_scale_min', 0.25)),
        'vol_scale_max': float(o.get('vol_scale_max', 2.0)),
        'max_contracts': int(o.get('max_contracts', 20)),
        'max_drawdown_halt_pct': float(o.get('drawdown_halt', 0.10)),
        'take_profit_pct': float(o.get('take_profit', 0.50)),
        'stop_loss_mult': float(o.get('stop_loss', 2.0)),
        'close_at_dte': int(o.get('close_at_dte', 0)),
        'vol_gate_iv': o.get('vol_gate_iv', 'short_leg'),
        'max_overnight_share': _optional_float(o.get('max_overnight_share')),
        'min_iv_har_ratio': _optional_float(o.get('min_iv_har_ratio')),
        # --events path/to/events.csv (event_date,event[,source]); --blackout FOMC,CPI,NFP
        # (default FOMC: the only blackout that improved the 2024-2026 result, see CHANGELOG)
        'blackout_types': [t.strip().upper() for t in o.get('blackout', 'FOMC').split(',') if t.strip()],
        'event_blackout': _load_events(o.get('events'),
                                       [t.strip().upper() for t in o.get('blackout', 'FOMC').split(',') if t.strip()]),
        'time_basis': o.get('time_basis', 'sessions'),   # 'sessions' (252) or 'calendar' (365)
        # `--wing-sigmas none` used to reach float('none') and raise. Every optional
        # numeric flag in this harness now spells "off" the same way, so a sweep can
        # pass the off value in the same position as a number.
        'wing_sigmas': _optional_float(o.get('wing_sigmas')),
        'har_horizon_days': (None if _optional_float(o.get('har_horizon')) is None
                             else int(float(o['har_horizon']))),
        'friction_pct': friction,
    }


def _optional_float(raw):
    """A number, or None for the several spellings of "this gate is off"."""
    if raw is None or str(raw).strip().lower() in ('', 'none', 'null', 'off'):
        return None
    return float(raw)


def _load_events(path, types=('FOMC',)):
    """Read event_date,event rows; keep the dates whose event type is in `types`."""
    if not path:
        default = os.path.join(REPO_ROOT, 'data', 'events.csv')
        path = default if os.path.exists(default) else None
    if not path:
        return []
    import csv
    out = []
    with open(path) as f:
        for row in csv.DictReader(f):
            d = (row.get('event_date') or '').strip()
            if d and (not types or (row.get('event') or '').strip().upper() in types):
                out.append(d)
    return sorted(set(out))


def write_csv(path, rows, columns):
    with open(path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow(r)


def benchmark_buy_hold(store, underlying, days, cash):
    """Buy-and-hold the underlying over the same window, same starting capital."""
    closes = [store.underlying_close(underlying, d) for d in days]
    closes = [c for c in closes if c]
    if len(closes) < 2:
        return None
    shares = cash // closes[0]
    return [cash + shares * (c - closes[0]) for c in closes]


def run_one(store, cfg, start, end):
    eng = Engine(store, cfg)
    res = eng.run(start, end)
    pnls = [t['pnl'] for t in res['trades']]
    days = [row['date'] for row in res['equity_curve']]
    cash_eq = [row['equity'] for row in res['equity_curve']]
    mtm_eq = [row.get('equity_mtm', row['equity']) for row in res['equity_curve']]
    # Both are reported, always. A realised-cash curve cannot show a drawdown that has
    # not been closed yet, so at any real level of concurrency it flatters the book;
    # the marked curve is the one a risk limit should be read against.
    equity = mtm_eq if cfg.get('equity_basis') == 'mtm' else cash_eq
    m = metrics.summarize(equity, pnls, trading_days=len(days))
    m_other = metrics.summarize(mtm_eq if equity is cash_eq else cash_eq, pnls,
                                trading_days=len(days))
    m['_alt_basis'] = 'mtm' if equity is cash_eq else 'cash'
    m['_alt'] = {k: m_other.get(k) for k in
                 ('total_return', 'sharpe', 'max_drawdown', 'annualized_return')}
    return eng, res, m, equity, days


def fmt_row(label, m):
    return (f"| {label} | {m['total_return']:.2%} | {m['annualized_return']:.2%} | "
            f"{m['max_drawdown']:.2%} | {m['sharpe']:.2f} | ${m['final_equity']:,.0f} |")


def main():
    sys.stdout.reconfigure(line_buffering=True)
    o = parse_args(sys.argv[1:])

    db = o.get('db', os.path.join(REPO_ROOT, 'data', 'market.duckdb'))
    names = None
    if o.get('shortlist'):
        path = o['shortlist']
        if not os.path.isabs(path):
            path = os.path.join(REPO_ROOT, path)
        buckets = [b.strip() for b in o.get('buckets', 'CORE').split(',') if b.strip()]
        names = SL.tradable(path, buckets=buckets, exclude_event_mode=True)
        if not names:
            print("ERROR: shortlist produced no tradable names"); sys.exit(1)
        store = Store(db, underlyings=names)
        names = [n for n in names if store._expiries_by.get(n)]
        if not names:
            print("ERROR: none of the shortlist names have option bars yet. "
                  "Run `make ingest-shortlist-options`."); sys.exit(1)
        store.close()
        store = Store(db, underlyings=names)
        book_label = 'book' + str(len(names))
    elif o.get('underlyings'):
        # `--underlyings` was in KNOWN_OPTS and read by nothing: it parsed, it landed in
        # `o`, and the run silently used the single `--underlying` default instead. A
        # named book therefore could not be run at all except through --shortlist.
        names = [n.strip().upper() for n in o['underlyings'].split(',') if n.strip()]
        store = Store(db, underlyings=names)
        missing = [n for n in names if not store._expiries_by.get(n)]
        if missing:
            print(f"ERROR: no option bars for {', '.join(missing)}. "
                  f"Run `make ingest-breadth` or drop them from --underlyings."); sys.exit(1)
        book_label = 'book' + str(len(names))
    else:
        underlying = o.get('underlying', 'SPY').upper()
        names = [underlying]
        store = Store(db, underlying=underlying)
        book_label = underlying

    cov = store.coverage()
    first = cov['option_bars']['first']
    last = cov['option_bars']['last']
    if not first:
        print("ERROR: no option bars in the store. Run `python3 data/ingest.py options` first.")
        sys.exit(1)

    start = dt.date.fromisoformat(o.get('start', first))
    end = dt.date.fromisoformat(o.get('end', last))

    frictions = list(fills.SWEEP_FRICTIONS) if o['sweep'] else \
        [_friction(o.get('friction', fills.DEFAULT_FRICTION_PCT))]

    print(f"Backtest {', '.join(names)}  {start} -> {end}")
    if len(names) == 1:
        print("This P&L is one underlying until other names have option bars in DuckDB.")
    else:
        print(f"Book of {len(names)} names; "
              f"{float(o.get('risk_pct', 0.02)):.1%} risk each, "
              f"cap {int(o.get('concurrent', o.get('max_concurrent', 3)))} concurrent, "
              f"{int(o.get('max_per_name', 1))} per name.")
    print(f"Store: {cov['option_bars']['rows']:,} option bars, "
          f"{cov['option_contracts']:,} contracts, {cov['option_expiries']} expiries\n")

    results = []
    for f in frictions:
        cfg = build_config(o, f, names)
        eng, res, m, equity, days = run_one(store, cfg, start, end)
        results.append({'friction': f, 'cfg': cfg, 'eng': eng, 'res': res,
                        'metrics': m, 'equity': equity, 'days': days})
        print(f"  friction {f:>5.1%}  ->  return {m['total_return']:>8.2%}  "
              f"trades {m['trades']:>4}  win {m['win_rate']:>6.1%}  "
              f"maxDD {m['max_drawdown']:>7.2%}  Sharpe {m['sharpe']:>6.2f}")

    primary = results[len(results) // 2] if o['sweep'] else results[0]
    label = o.get('label', primary['cfg']['structure'])
    stamp = dt.date.today().isoformat()
    run_dir = os.path.join(RUNS_DIR, f"{stamp}_{book_label}_{label}_1Day")
    os.makedirs(run_dir, exist_ok=True)

    bench = benchmark_buy_hold(store, names[0], primary['days'], primary['cfg']['initial_cash'])
    bench_m = metrics.summarize(bench, [], trading_days=len(primary['days'])) if bench else None

    # ------------------------------------------------------------- artifacts
    # The call wing used to be dropped here even though the engine records it, so half
    # of every condor was unauditable from the run folder and P&L attribution was
    # impossible. write_csv uses extrasaction='ignore', which made the loss silent.
    trade_cols = ['entry_date', 'exit_date', 'underlying', 'kind', 'structure', 'side',
                  'expiry', 'dte_at_entry', 'held_days',
                  'short_occ', 'long_occ', 'short_strike', 'long_strike',
                  'put_short_occ', 'call_short_occ', 'call_long_occ',
                  'call_short_strike', 'call_long_strike',
                  'width', 'qty', 'credit', 'debit', 'exit_debit',
                  'short_delta', 'net_delta', 'short_iv', 'iv_rv_ratio',
                  'entry_spot', 'exit_spot', 'entry_fees', 'exit_fees',
                  'max_loss', 'pnl', 'return_on_risk', 'exit_reason', 'portrait']
    write_csv(os.path.join(run_dir, 'trades.csv'), primary['res']['trades'], trade_cols)
    write_csv(os.path.join(run_dir, 'round_trips.csv'), primary['res']['trades'], trade_cols)
    direction = ATTR.directional_split(primary['res']['trades'])
    with open(os.path.join(run_dir, 'direction.json'), 'w') as f:
        json.dump(direction, f, indent=2)
    economics = ECO.book_economics(primary['res']['trades'])
    with open(os.path.join(run_dir, 'economics.json'), 'w') as f:
        json.dump(economics, f, indent=2)
    write_csv(os.path.join(run_dir, 'equity.csv'), primary['res']['equity_curve'],
              ['date', 'equity', 'equity_mtm', 'risk_committed', 'risk_scale',
               'open_positions'])
    if bench:
        write_csv(os.path.join(run_dir, 'benchmark_equity.csv'),
                  [{'date': d, 'equity': e} for d, e in zip(primary['days'], bench)],
                  ['date', 'equity'])

    fingerprint = store.fingerprint(start, end)
    for name, payload in (
        ('config.json', primary['cfg']),
        ('strategy_spec.json', {
            'name': ('short-dated adaptive credit (condor vs put, never call-ranked)'
                     if primary['cfg']['structure'] == 'adaptive'
                     else 'short-dated delta-neutral iron condor'
                     if primary['cfg']['structure'] == 'condor'
                     else 'short-dated defined-risk vertical credit spread'),
            'structure': primary['cfg']['structure'],
            'vol_filter': (f"only enter when implied vol >= "
                           f"{primary['cfg']['min_iv_rv_ratio']}x trailing "
                           f"{primary['cfg']['rv_window']}-session realized vol"
                           if primary['cfg']['min_iv_rv_ratio'] else 'none'),
            'underlying': names[0],
            'underlyings': names,
            'entry': (f"Sell the {primary['cfg']['target_delta']:.2f}-delta strike "
                      f"(+/-{primary['cfg']['delta_tolerance']:.2f}) in the nearest expiry "
                      f"{primary['cfg']['min_dte']}-{primary['cfg']['max_dte']} DTE; buy the "
                      f"strike ${primary['cfg']['width']:.0f} further OTM"
                      + (f" (or {primary['cfg']['width_pct']:.0%} of spot)"
                         if primary['cfg'].get('width_pct') else "")
                      + "."),
            'filters': (f"credit/width between {primary['cfg']['min_credit_ratio']} and "
                        f"{primary['cfg']['max_credit_ratio']}"),
            'exit': (f"take profit at {primary['cfg']['take_profit_pct']:.0%} of credit; "
                     f"stop at {primary['cfg']['stop_loss_mult']}x credit; else hold to expiry."),
            'sizing': (f"contracts sized so defined max loss <= "
                       f"{primary['cfg']['max_risk_per_trade_pct']:.1%} of equity"),
            'concurrency': primary['cfg']['max_concurrent'],
        }),
        ('data_fingerprint.json', fingerprint),
        ('fee_source.json', dict(FEE_SOURCE, extracted_at=dt.datetime.now().isoformat())),
        ('warnings.json', {
            'method_caveats': METHOD_CAVEATS,
            'engine_warnings': primary['res']['warnings'][:200],
            'engine_warning_count': len(primary['res']['warnings']),
            'halted': primary['res']['halted'],
        }),
    ):
        with open(os.path.join(run_dir, name), 'w') as f:
            json.dump(payload, f, indent=2, default=str)

    summary = {
        'strategy_name': f"{book_label} short-dated vertical credit spread",
        'start': str(start), 'end': str(end), 'symbols': names, 'timeframe': '1Day',
        'initial_cash': primary['cfg']['initial_cash'],
        'metrics': primary['metrics'],
        'benchmarks': {'buy_and_hold': bench_m} if bench_m else {},
        'friction_sweep': [
            {'friction_pct': r['friction'], **{k: r['metrics'][k] for k in
             ('total_return', 'annualized_return', 'sharpe', 'max_drawdown',
              'trades', 'win_rate', 'profit_factor', 'final_equity')}}
            for r in results],
        'first_trade': primary['res']['trades'][0] if primary['res']['trades'] else {},
        'last_trade': primary['res']['trades'][-1] if primary['res']['trades'] else {},
        'direction': direction,
        'economics': economics,
        'assumptions': METHOD_CAVEATS,
        'warnings': primary['res']['warnings'][:50],
        'data_fingerprint': fingerprint,
        'fee_source': FEE_SOURCE,
        'artifacts': sorted(os.listdir(run_dir)),
    }
    with open(os.path.join(run_dir, 'summary.json'), 'w') as f:
        json.dump(summary, f, indent=2, default=str)

    write_report(run_dir, book_label, start, end, results, primary, bench_m, cov)
    write_notes(run_dir, book_label, start, end, primary, cov, frictions)

    m = primary['metrics']
    print(f"\n{'-' * 66}\nTeaching Five ({primary['friction']:.0%} friction)")
    print(f"  total return    {m['total_return']:>10.2%}"
          + (f"   (benchmark {bench_m['total_return']:.2%})" if bench_m else ""))
    print(f"  max drawdown    {m['max_drawdown']:>10.2%}")
    print(f"  trades          {m['trades']:>10}")
    print(f"  win rate        {m['win_rate']:>10.1%}")
    print(f"  Sharpe          {m['sharpe']:>10.2f}"
          + (f"   (benchmark {bench_m['sharpe']:.2f})" if bench_m else ""))
    print(f"  direction       up ${direction['pnl_on_up_moves']:,.0f} ({direction['trades_up']})"
          f"  down ${direction['pnl_on_down_moves']:,.0f} ({direction['trades_down']})"
          f"  asym {direction['asymmetry']:.0%}  {direction['verdict']}")
    if economics.get('winrate_gap') is not None:
        print(f"  economics       credit/width {economics['mean_credit_over_width']:.3f}  "
              f"need WR {economics['breakeven_winrate']:.1%}  "
              f"got {economics['actual_winrate']:.1%}  "
              f"gap {economics['winrate_gap']:+.1%}  "
              f"leftover {economics['leftover_share_of_credit']:.1%} of credit")
    print(f"\nArtifacts: {os.path.relpath(run_dir, REPO_ROOT)}")
    store.close()


def write_report(run_dir, underlying, start, end, results, primary, bench_m, cov):
    m = primary['metrics']
    lines = [
        f"# {underlying} short-dated credit spreads -- backtest report",
        "",
        f"**Window:** {start} -> {end}  ·  **Timeframe:** 1Day  ·  "
        f"**Friction:** {primary['friction']:.0%} per leg",
        "",
        "| | Total Return | Ann. Return | Max Drawdown | Sharpe | Final Equity |",
        "|---|---:|---:|---:|---:|---:|",
        fmt_row("**Strategy**", m),
    ]
    if bench_m:
        lines.append(fmt_row(f"{underlying} buy & hold", bench_m))
    lines += ["", "## Friction sweep", "",
              "The result at several execution-cost assumptions. Alpaca serves no historical",
              "option bid/ask, so friction is an assumption, not a measurement -- read the row",
              "that matches what you believe execution actually costs, and note where the edge",
              "disappears.", "",
              "| Friction / leg | Total Return | Trades | Win rate | Profit factor | Max DD | Sharpe |",
              "|---:|---:|---:|---:|---:|---:|---:|"]
    for r in results:
        rm = r['metrics']
        pf = rm['profit_factor']
        lines.append(
            f"| {r['friction']:.0%} | {rm['total_return']:.2%} | {rm['trades']} | "
            f"{rm['win_rate']:.1%} | {'inf' if pf == float('inf') else f'{pf:.2f}'} | "
            f"{rm['max_drawdown']:.2%} | {rm['sharpe']:.2f} |")

    lines += ["", "## Trade statistics", "",
              f"- Trades: **{m['trades']}** ({m['wins']} wins / {m['losses']} losses)",
              f"- Win rate: **{m['win_rate']:.1%}**",
              "- Profit factor: **" + ('inf' if m['profit_factor'] == float('inf') else f"{m['profit_factor']:.2f}") + "**",
              f"- Average trade: **${m['avg_trade']:,.2f}**  "
              f"(win ${m['avg_win']:,.2f} / loss ${m['avg_loss']:,.2f})",
              f"- Expectancy per trade: **${m['expectancy']:,.2f}**",
              f"- Largest win / loss: ${m['largest_win']:,.2f} / ${m['largest_loss']:,.2f}",
              f"- Longest drawdown: {m['max_drawdown_days']} sessions",
              "", "## Direction (no greeks)", "",
              "P&L split by whether the underlying rose or fell between entry and exit.",
              "Daily closes in this store cannot support a Greek decomposition (asynchronous IV);",
              "this split is the measurement that survives. It does not assume a hedge in the",
              "underlying.", ""]
    dsplit = ATTR.directional_split(primary['res']['trades'])
    lines += [f"| | P&L | trades |",
              f"|---|---:|---:|",
              f"| underlying up | ${dsplit['pnl_on_up_moves']:,.0f} | {dsplit['trades_up']} |",
              f"| underlying down | ${dsplit['pnl_on_down_moves']:,.0f} | {dsplit['trades_down']} |",
              "",
              f"Asymmetry **{dsplit['asymmetry']:.0%}** — {dsplit['verdict']}.",
              "", "## Economics (winrate gap)", "",
              "Stable statistics at n≈100: credit leftover, breakeven vs actual winrate,",
              "exit mix, fat-tail share. Do not pick parameters by P&L on this sample.", ""]
    eco = ECO.book_economics(primary['res']['trades'])
    if eco.get('winrate_gap') is not None:
        lines += [
            f"- Credit collected: **${eco['credit_collected_dollars']:,.0f}**; "
            f"P&L left: **${eco['pnl_dollars']:,.0f}** "
            f"({eco['leftover_share_of_credit']:.1%} of credit)",
            f"- Mean credit/width: **{eco['mean_credit_over_width']:.3f}** → "
            f"breakeven WR **{eco['breakeven_winrate']:.1%}**; "
            f"actual **{eco['actual_winrate']:.1%}**; "
            f"gap **{eco['winrate_gap']:+.1%}**",
            f"- Tail (|pnl| > half max_loss): **{eco['tail']['n']}** trades, "
            f"${eco['tail']['pnl_dollars']:,.0f}",
            "",
            "| exit | n | P&L | avg | worst |",
            "|---|---:|---:|---:|---:|",
        ]
        for reason, slot in eco['by_exit_reason'].items():
            lines.append(
                f"| {reason} | {slot['n']} | ${slot['pnl_dollars']:,.0f} | "
                f"${slot['avg_pnl']:,.0f} | ${slot['worst_pnl']:,.0f} |")
    lines += ["", "## Data", "",
              f"- Option bars: {cov['option_bars']['rows']:,} "
              f"({cov['option_bars']['first']} -> {cov['option_bars']['last']})",
              f"- Distinct contracts: {cov['option_contracts']:,} across "
              f"{cov['option_expiries']} expiries",
              f"- Underlying bars: {cov['underlying_bars']['rows']:,}",
              "", "## How to read this", "",
              "Every number above is conditional on assumptions that cannot be verified from",
              "the available data. In order of how much they matter:", ""]
    for c in METHOD_CAVEATS:
        lines.append(f"- {c}")
    lines += ["", "## Disclosure", "", DISCLOSURE, ""]

    with open(os.path.join(run_dir, 'report.md'), 'w') as f:
        f.write("\n".join(lines))


def write_notes(run_dir, underlying, start, end, primary, cov, frictions):
    cfg = primary['cfg']
    notes = f"""# Run notes

**Created:** {dt.datetime.now().isoformat(timespec='seconds')}
**Underlying:** {underlying}   **Window:** {start} -> {end}

## What was run

Short-dated defined-risk vertical credit spreads, {cfg['min_dte']}-{cfg['max_dte']} DTE,
short leg at {cfg['target_delta']:.2f} delta (+/-{cfg['delta_tolerance']:.2f}),
${cfg['width']:.0f} wide, side={cfg['side']}.
Exits: take profit at {cfg['take_profit_pct']:.0%} of credit, stop at {cfg['stop_loss_mult']}x credit,
otherwise held to expiry.
Sizing: max {cfg['max_risk_per_trade_pct']:.1%} of equity at risk per trade,
at most {cfg['max_concurrent']} concurrent positions.
Friction tested: {', '.join(f'{f:.0%}' for f in frictions)} per leg.

## Data lineage

Fetched with the Alpaca CLI into DuckDB via `data/ingest.py`, then read through
`backtest/store.py`. Underlying bars use feed=sip with adjustment=all; option bars come
from the indicative feed (this account is not OPRA-entitled).

Option bars in store: {cov['option_bars']['rows']:,}
({cov['option_bars']['first']} -> {cov['option_bars']['last']}),
{cov['option_contracts']:,} contracts, {cov['option_expiries']} expiries.

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
"""
    with open(os.path.join(run_dir, 'notes.md'), 'w') as f:
        f.write(notes)


if __name__ == "__main__":
    main()
