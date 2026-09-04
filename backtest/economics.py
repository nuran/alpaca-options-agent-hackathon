"""
Book economics that survive n=132: winrate gap, leftover credit, exit mix, tail.

P&L at this sample size is noise around a stable structure (stop-mult jumps of 0.25
flip the headline by six points). The quantities here move smoothly and answer the
winrate-gap session without choosing a parameter by return:

  - credit collected vs P&L left (leftover share)
  - breakeven winrate = 1 - credit/width vs realised winrate (the gap)
  - P&L by exit_reason
  - share of losses from the fat tail (|pnl| > half of max_loss)

Wired into every run as economics.json. Does not gate or fail the run.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict


CONTRACT = 100.0
TAIL_FRAC = 0.5  # |pnl| > this fraction of max_loss counts as the fat tail


def _f(row, *keys, default=None):
    for k in keys:
        if k in row and row[k] not in (None, ''):
            try:
                return float(row[k])
            except (TypeError, ValueError):
                continue
    return default


def trade_credit_width(row):
    """
    Per-trade credit and width in dollars of premium (not dollars of P&L).

    Prefer filled credit; fall back to raw fields. Width is the defined-risk wing.
    """
    credit = _f(row, 'credit', 'raw_credit')
    width = _f(row, 'width')
    qty = _f(row, 'qty', default=1.0) or 1.0
    return credit, width, qty


def book_economics(trade_rows):
    """
    Aggregate economics for a finished book. Empty book -> zeros and an explicit note.
    """
    rows = list(trade_rows or [])
    if not rows:
        return {
            'trades': 0,
            'note': 'no trades',
            'credit_collected_dollars': 0.0,
            'pnl_dollars': 0.0,
            'leftover_share_of_credit': None,
            'mean_credit_over_width': None,
            'breakeven_winrate': None,
            'actual_winrate': None,
            'winrate_gap': None,
            'by_exit_reason': {},
            'tail': {'n': 0, 'pnl_dollars': 0.0, 'share_of_gross_loss': None,
                     'share_of_gross_profit': None},
        }

    credit_dollars = 0.0
    ratios = []
    pnl_total = 0.0
    wins = losses = 0
    gross_profit = gross_loss = 0.0
    by_exit = defaultdict(lambda: {'n': 0, 'pnl': 0.0, 'worst': None})
    tail_n = 0
    tail_pnl = 0.0

    for r in rows:
        credit, width, qty = trade_credit_width(r)
        pnl = _f(r, 'pnl', default=0.0) or 0.0
        pnl_total += pnl
        if pnl > 0:
            wins += 1
            gross_profit += pnl
        elif pnl < 0:
            losses += 1
            gross_loss += -pnl

        if credit is not None and qty:
            credit_dollars += credit * CONTRACT * qty
        if credit is not None and width and width > 0:
            ratios.append(credit / width)

        reason = str(r.get('exit_reason') or 'unknown')
        slot = by_exit[reason]
        slot['n'] += 1
        slot['pnl'] += pnl
        slot['worst'] = pnl if slot['worst'] is None else min(slot['worst'], pnl)

        max_loss = _f(r, 'max_loss')
        if max_loss and max_loss > 0 and pnl < 0 and abs(pnl) > TAIL_FRAC * max_loss:
            tail_n += 1
            tail_pnl += pnl

    n = len(rows)
    actual_wr = wins / n if n else None
    mean_ratio = (sum(ratios) / len(ratios)) if ratios else None
    breakeven = (1.0 - mean_ratio) if mean_ratio is not None else None
    gap = (actual_wr - breakeven) if (actual_wr is not None and breakeven is not None) else None
    leftover = (pnl_total / credit_dollars) if credit_dollars else None

    by_exit_out = {}
    for reason, slot in sorted(by_exit.items()):
        by_exit_out[reason] = {
            'n': slot['n'],
            'pnl_dollars': round(slot['pnl'], 2),
            'avg_pnl': round(slot['pnl'] / slot['n'], 2) if slot['n'] else 0.0,
            'worst_pnl': round(slot['worst'] if slot['worst'] is not None else 0.0, 2),
        }

    return {
        'trades': n,
        'wins': wins,
        'losses': losses,
        'credit_collected_dollars': round(credit_dollars, 2),
        'pnl_dollars': round(pnl_total, 2),
        'leftover_share_of_credit': round(leftover, 4) if leftover is not None else None,
        'mean_credit_over_width': round(mean_ratio, 4) if mean_ratio is not None else None,
        'breakeven_winrate': round(breakeven, 4) if breakeven is not None else None,
        'actual_winrate': round(actual_wr, 4) if actual_wr is not None else None,
        'winrate_gap': round(gap, 4) if gap is not None else None,
        '_gap_note': ('negative gap = realised winrate below breakeven for this '
                      'credit/width; half of an 8pt gap at 3% friction was execution '
                      'cost in the 2026-08-29 measurement'),
        'by_exit_reason': by_exit_out,
        'tail': {
            'threshold': f'|pnl| > {TAIL_FRAC:.0%} of max_loss',
            'n': tail_n,
            'pnl_dollars': round(tail_pnl, 2),
            'share_of_trades': round(tail_n / n, 4) if n else None,
            'share_of_gross_loss': (round((-tail_pnl) / gross_loss, 4)
                                   if gross_loss > 0 else None),
            'share_of_gross_profit': (round((-tail_pnl) / gross_profit, 4)
                                     if gross_profit > 0 and tail_pnl < 0 else None),
        },
    }


def from_run_dir(run_dir):
    path = os.path.join(run_dir, 'trades.csv')
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    import csv
    rows = list(csv.DictReader(open(path)))
    return book_economics(rows)


if __name__ == '__main__':
    if len(sys.argv) > 1 and not sys.argv[1].startswith('-'):
        eco = from_run_dir(sys.argv[1])
        print(json.dumps(eco, indent=2))
        out = os.path.join(sys.argv[1], 'economics.json')
        json.dump(eco, open(out, 'w'), indent=2)
        print(f"wrote {out}")
    else:
        # Small book: credit/width ≈ 0.185 -> need WR ≈ 81.5%; actual 75% -> gap ≈ -6.5pts.
        rows = [
            {'credit': 0.90, 'width': 5.0, 'qty': 1, 'pnl': 300.0,
             'max_loss': 410.0, 'exit_reason': 'take_profit'},
            {'credit': 0.90, 'width': 5.0, 'qty': 1, 'pnl': 280.0,
             'max_loss': 410.0, 'exit_reason': 'take_profit'},
            {'credit': 0.90, 'width': 5.0, 'qty': 1, 'pnl': -700.0,
             'max_loss': 410.0, 'exit_reason': 'stop_loss'},
            {'credit': 1.00, 'width': 5.0, 'qty': 1, 'pnl': 50.0,
             'max_loss': 400.0, 'exit_reason': 'expiry'},
        ]
        eco = book_economics(rows)
        assert eco['trades'] == 4
        assert abs(eco['mean_credit_over_width'] - 0.185) < 1e-6, eco
        assert abs(eco['breakeven_winrate'] - 0.815) < 1e-6
        assert eco['actual_winrate'] == 0.75
        assert eco['winrate_gap'] < 0
        assert eco['tail']['n'] == 1
        # Session shape: credit/width ≈ 0.193, need ~81% WR, get 73% -> negative gap.
        rows2 = (
            [{'credit': 0.965, 'width': 5.0, 'qty': 1, 'pnl': 200.0,
              'max_loss': 403.5, 'exit_reason': 'take_profit'}] * 96
            + [{'credit': 0.965, 'width': 5.0, 'qty': 1, 'pnl': -800.0,
                'max_loss': 403.5, 'exit_reason': 'stop_loss'}] * 36
        )
        eco2 = book_economics(rows2)
        assert eco2['trades'] == 132
        assert abs(eco2['mean_credit_over_width'] - 0.193) < 1e-3, eco2
        assert abs(eco2['breakeven_winrate'] - 0.807) < 1e-2, eco2
        assert abs(eco2['actual_winrate'] - round(96 / 132, 4)) < 1e-9
        assert eco2['winrate_gap'] < -0.05, eco2
        assert eco2['tail']['n'] == 36
        print(f"economics.py self-check OK  "
              f"(gap {eco2['winrate_gap']:.1%}, leftover {eco2['leftover_share_of_credit']})")
