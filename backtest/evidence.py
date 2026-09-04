"""
Evidence pack generator: turns the run folders into the artefacts the 2026 audits ask for.

`python3 backtest/evidence.py [--runs runs] [--champion <run folder name>] [--out EVIDENCE.md]`

Produces `EVIDENCE.md` + `evidence.json` containing, for the selected (champion) variant and
for every variant that was tried:

  * net-of-cost headline metrics from each run's `summary.json` (no recomputation);
  * the **Deflated Sharpe Ratio** of the champion, given the number of variants tried
    (multiple-testing correction -- "The Alpha Illusion", failure mode 3);
  * the **Probability of Backtest Overfitting** across all variants by CSCV
    (Bailey/Borwein/Lopez de Prado/Zhu) -- did the selection survive out of sample;
  * a **rolling-window net-of-cost table** for the champion (regime coverage --
    Nguyen & Pham, criterion 5);
  * the data fingerprint, friction assumption and the caveat list from `warnings.json`
    (reproducibility -- Xia et al.'s evidence ledger, Yao & Zheng's reporting checklist).

The point is not a better number. It is that the number now comes with the conditions under
which it may be quoted.
"""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stats as S

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def load_runs(runs_dir):
    out = {}
    for name in sorted(os.listdir(runs_dir)):
        d = os.path.join(runs_dir, name)
        eq, sm = os.path.join(d, 'equity.csv'), os.path.join(d, 'summary.json')
        if not (os.path.isdir(d) and os.path.exists(eq) and os.path.exists(sm)):
            continue
        dates, rets = S.equity_to_returns(eq)
        out[name] = {'dir': d, 'dates': dates, 'returns': rets,
                     'summary': json.load(open(sm))}
    return out


def grid_trials(runs_dir):
    """
    Every configuration the parameter grids actually evaluated.

    The selection pool is NOT "how many run folders happen to be on disk". A grid that
    evaluated 376 cells and reported its best one selected from 376, and counting 3 makes
    the deflation vanish: on the 16-name book, n_trials=3 gives DSR 0.992 ("clears the
    bar") where the honest 376 gives 0.49. Run folders are what someone chose to keep;
    runs/_grid/*.jsonl is what was searched.
    """
    import glob
    srs = []
    for path in glob.glob(os.path.join(runs_dir, '_grid', '*.jsonl')):
        for line in open(path):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if 'error' not in r and isinstance(r.get('sharpe'), (int, float)):
                srs.append(float(r['sharpe']))
    return srs


def build(runs_dir, champion=None, window=63, step=21, trials=None):
    runs = load_runs(runs_dir)
    if not runs:
        raise SystemExit(f"no runs with equity.csv under {runs_dir}")
    champion = champion or max(runs, key=lambda n: runs[n]['summary']['metrics']['sharpe'])
    trial_sharpes = [r['summary']['metrics']['sharpe'] for r in runs.values()]
    grid_sr = grid_trials(runs_dir)
    if grid_sr:
        trial_sharpes = grid_sr + trial_sharpes
    n_trials = int(trials) if trials else len(trial_sharpes)
    ch = runs[champion]
    dsr = S.deflated_sharpe(ch['returns'], n_trials=n_trials, trial_sharpes=trial_sharpes)
    dsr['grid_cells'] = len(grid_sr)
    matrix = {n: r['returns'] for n, r in runs.items()}
    pbo = S.pbo_cscv(matrix, s=8)
    roll = S.rolling_windows(ch['dates'], ch['returns'], window=window, step=step)
    warn = {}
    wpath = os.path.join(ch['dir'], 'warnings.json')
    if os.path.exists(wpath):
        warn = json.load(open(wpath))
    fp = {}
    fpath = os.path.join(ch['dir'], 'data_fingerprint.json')
    if os.path.exists(fpath):
        fp = json.load(open(fpath))
    return {'champion': champion, 'n_variants': len(runs), 'dsr': dsr, 'pbo': pbo,
            'rolling': roll, 'variants': {n: r['summary']['metrics'] for n, r in runs.items()},
            'caveats': warn.get('method_caveats', []), 'fingerprint': fp,
            'window': {'start': ch['summary'].get('start'), 'end': ch['summary'].get('end')}}


def render(ev):
    m = ev['variants'][ev['champion']]
    L = [f"# Evidence pack — {ev['champion']}", "",
         f"Window {ev['window']['start']} → {ev['window']['end']} · "
         f"{ev['dsr'].get('n_trials', ev['n_variants'])} configurations evaluated · "
         f"net of modelled friction and fees.", "",
         "## Headline (champion)", "",
         "| metric | value |", "|---|---:|",
         f"| total return | {m['total_return']:.2%} |",
         f"| Sharpe | {m['sharpe']:.2f} |",
         f"| max drawdown | {m['max_drawdown']:.2%} |",
         f"| trades | {m['trades']} |",
         f"| win rate | {m['win_rate']:.1%} |",
         f"| profit factor | {m['profit_factor']:.2f} |", "",
         "## Multiple testing", "",
         f"- Selection pool: **{ev['dsr'].get('n_trials', ev['n_variants'])}** "
         f"({ev['dsr'].get('grid_cells', 0)} grid cells + {ev['n_variants']} kept run folders)",
         f"- Observed annualised Sharpe: **{ev['dsr']['sharpe']:.2f}**",
         f"- Benchmark SR\\* (expected max of "
         f"{ev['dsr'].get('n_trials', ev['n_variants'])} trials): **{ev['dsr']['sr_star']:.2f}**",
         f"- Return skew {ev['dsr']['skew']:.2f}, kurtosis {ev['dsr']['kurtosis']:.2f}, "
         f"{ev['dsr']['n']} daily observations",
         f"- **Deflated Sharpe Ratio: {ev['dsr']['dsr']:.3f}** "
         f"({'clears' if ev['dsr']['dsr'] >= 0.95 else 'does NOT clear'} the conventional 0.95 bar)",
         "",
         "## Backtest overfitting (CSCV)", "",
         f"- Variants in the matrix: {ev['pbo']['variants']} · splits: {ev['pbo']['splits']} · "
         f"periods: {ev['pbo']['periods']}",
         f"- NOTE: the matrix below holds only the kept run folders. The PBO over the grid the "
         "parameters were actually selected from (75 variants) is in runs/_grid/overfit.json.",
         f"- **PBO = {ev['pbo']['pbo']:.2f}** — the share of splits where the in-sample winner "
         f"lands in the bottom half out of sample (median logit {ev['pbo']['median_logit']:+.2f})",
         "", "## Rolling windows (net of cost)", "",
         "| start | end | return | Sharpe |", "|---|---|---:|---:|"]
    for w in ev['rolling']:
        L.append(f"| {w['start']} | {w['end']} | {w['return']:+.2%} | {w['sharpe']:.2f} |")
    L += ["", "## All variants tried", "", "| variant | return | Sharpe | trades | win rate |",
          "|---|---:|---:|---:|---:|"]
    for n, v in sorted(ev['variants'].items(), key=lambda kv: -kv[1]['sharpe']):
        star = " ←" if n == ev['champion'] else ""
        L.append(f"| {n}{star} | {v['total_return']:+.2%} | {v['sharpe']:.2f} | {v['trades']} | "
                 f"{v['win_rate']:.1%} |")
    if ev['caveats']:
        L += ["", "## Caveats carried from the run", ""] + [f"- {c}" for c in ev['caveats']]
    if ev['fingerprint']:
        L += ["", "## Data fingerprint", "", "```json", json.dumps(ev['fingerprint'], indent=2)[:1200], "```"]
    L += ["", "---", "",
          "Generated by `backtest/evidence.py`. DSR: Bailey & Lopez de Prado. PBO/CSCV: Bailey, "
          "Borwein, Lopez de Prado & Zhu. Reporting structure follows the 2026 audits "
          "(arXiv 2605.19337, 2605.16895, 2606.08285, 2603.27539)."]
    return "\n".join(L)


def main(argv):
    o = {}
    i = 0
    while i < len(argv):
        if argv[i].startswith('--'):
            o[argv[i][2:]] = argv[i + 1] if i + 1 < len(argv) else True
            i += 2
        else:
            i += 1
    runs_dir = o.get('runs', os.path.join(REPO_ROOT, 'runs'))
    ev = build(runs_dir, o.get('champion'), int(o.get('window', 63)),
               int(o.get('step', 21)), o.get('trials'))
    out_md = o.get('out', os.path.join(REPO_ROOT, 'EVIDENCE.md'))
    open(out_md, 'w').write(render(ev) + "\n")
    open(os.path.splitext(out_md)[0].replace('EVIDENCE', 'evidence') + '.json', 'w').write(
        json.dumps(ev, indent=2, default=str))
    print(f"champion {ev['champion']}: Sharpe {ev['dsr']['sharpe']:.2f}, SR* {ev['dsr']['sr_star']:.2f}, "
          f"DSR {ev['dsr']['dsr']:.3f}, PBO {ev['pbo']['pbo']:.2f} over {ev['n_variants']} variants")
    print(f"wrote {out_md}")


if __name__ == "__main__":
    main(sys.argv[1:])
