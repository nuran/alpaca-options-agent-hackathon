"""
Performance metrics for the backtest.

Formulas are pinned to the definitions in Alpaca's vendored alpaca-trading-backtest
skill (reference.md, "Metric formulas") so results here are directly comparable to
anything produced by that skill:

  ann_return   = (1 + total_return) ** (252 / trading_days) - 1
  Sharpe       = mean(daily_returns) / stdev(daily_returns) * sqrt(252)
                 with the SAMPLE (N-1) standard deviation, rf = 0, computed from
                 DAILY equity -- not per-bar.
  max_drawdown = min(equity / running_max - 1)
  profit_factor= gross_profit / gross_loss, inf when there are no losers.

The N-1 detail matters: the same skill specifies POPULATION stddev for Bollinger
Bands and SAMPLE stddev for Sharpe. Using the wrong one inflates Sharpe on short
samples, which is exactly the regime this backtest runs in.

Stdlib only.
"""
import math

TRADING_DAYS = 252


def _stdev_sample(xs):
    """Sample (N-1) standard deviation. Returns 0.0 for fewer than 2 points."""
    n = len(xs)
    if n < 2:
        return 0.0
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    return math.sqrt(var)


def daily_returns(equity):
    """Simple period-over-period returns from an equity series."""
    out = []
    for prev, cur in zip(equity, equity[1:]):
        out.append((cur - prev) / prev if prev else 0.0)
    return out


def total_return(equity):
    if len(equity) < 2 or not equity[0]:
        return 0.0
    return equity[-1] / equity[0] - 1.0


def annualized_return(equity, trading_days):
    tr = total_return(equity)
    if trading_days <= 0:
        return 0.0
    base = 1.0 + tr
    if base <= 0:
        return -1.0  # wiped out; the power form is undefined
    return base ** (TRADING_DAYS / trading_days) - 1.0


def sharpe(equity, rf=0.0):
    rets = daily_returns(equity)
    if len(rets) < 2:
        return 0.0
    excess = [r - rf / TRADING_DAYS for r in rets]
    sd = _stdev_sample(excess)
    if sd == 0:
        return 0.0
    return (sum(excess) / len(excess)) / sd * math.sqrt(TRADING_DAYS)


def sortino(equity, rf=0.0):
    """Sharpe's downside-only sibling -- credit strategies have very asymmetric returns."""
    rets = daily_returns(equity)
    if len(rets) < 2:
        return 0.0
    excess = [r - rf / TRADING_DAYS for r in rets]
    downside = [e for e in excess if e < 0]
    if not downside:
        return float('inf')
    dd = math.sqrt(sum(e * e for e in downside) / len(downside))
    if dd == 0:
        return 0.0
    return (sum(excess) / len(excess)) / dd * math.sqrt(TRADING_DAYS)


def max_drawdown(equity):
    """Most negative peak-to-trough excursion, as a fraction (e.g. -0.14)."""
    if not equity:
        return 0.0
    peak = equity[0]
    worst = 0.0
    for e in equity:
        peak = max(peak, e)
        if peak:
            worst = min(worst, e / peak - 1.0)
    return worst


def drawdown_duration(equity):
    """Longest run of periods spent below a prior peak."""
    if not equity:
        return 0
    peak, longest, current = equity[0], 0, 0
    for e in equity:
        if e >= peak:
            peak, current = e, 0
        else:
            current += 1
            longest = max(longest, current)
    return longest


def trade_stats(pnls):
    """Win rate, profit factor, and the usual per-trade aggregates."""
    if not pnls:
        return {
            'trades': 0, 'wins': 0, 'losses': 0, 'win_rate': 0.0,
            'gross_profit': 0.0, 'gross_loss': 0.0, 'profit_factor': 0.0,
            'avg_win': 0.0, 'avg_loss': 0.0, 'avg_trade': 0.0,
            'largest_win': 0.0, 'largest_loss': 0.0, 'expectancy': 0.0,
        }
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    win_rate = len(wins) / len(pnls)
    avg_win = gross_profit / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0
    return {
        'trades': len(pnls),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': win_rate,
        'gross_profit': gross_profit,
        'gross_loss': gross_loss,
        'profit_factor': (gross_profit / gross_loss) if gross_loss else float('inf'),
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'avg_trade': sum(pnls) / len(pnls),
        'largest_win': max(pnls),
        'largest_loss': min(pnls),
        # Expectancy per trade in currency, the number that actually decides sizing.
        'expectancy': win_rate * avg_win - (1 - win_rate) * avg_loss,
    }


def summarize(equity, pnls, trading_days=None):
    """Full metric block for summary.json."""
    n_days = trading_days if trading_days is not None else max(len(equity) - 1, 1)
    stats = trade_stats(pnls)
    stats.update({
        'initial_equity': equity[0] if equity else 0.0,
        'final_equity': equity[-1] if equity else 0.0,
        'total_return': total_return(equity),
        'annualized_return': annualized_return(equity, n_days),
        'sharpe': sharpe(equity),
        'sortino': sortino(equity),
        'max_drawdown': max_drawdown(equity),
        'max_drawdown_days': drawdown_duration(equity),
        'trading_days': n_days,
    })
    return stats


if __name__ == "__main__":
    # Known-answer checks, hand-verifiable.
    eq = [100.0, 110.0, 105.0, 115.0]
    assert abs(total_return(eq) - 0.15) < 1e-12

    # max drawdown: peak 110 -> trough 105 == -4.5454...%
    assert abs(max_drawdown(eq) - (105 / 110 - 1)) < 1e-12

    # Sharpe against a hand-computed sample stdev.
    rets = daily_returns(eq)
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    expected = mean / math.sqrt(var) * math.sqrt(252)
    assert abs(sharpe(eq) - expected) < 1e-12, (sharpe(eq), expected)

    # A flat curve must be 0, not NaN.
    assert sharpe([100.0] * 10) == 0.0
    assert max_drawdown([100.0] * 10) == 0.0

    s = trade_stats([100, -50, 200, -25, 75])
    assert s['trades'] == 5 and s['wins'] == 3
    assert abs(s['win_rate'] - 0.6) < 1e-12
    assert abs(s['profit_factor'] - 375 / 75) < 1e-12
    assert trade_stats([10, 20])['profit_factor'] == float('inf')

    # Total wipeout must not raise on the fractional power.
    assert annualized_return([100.0, 0.0], 10) == -1.0

    print(f"  sharpe {sharpe(eq):.4f}  maxDD {max_drawdown(eq):.4%}  "
          f"PF {s['profit_factor']:.2f}  winrate {s['win_rate']:.0%}")
    print("metrics.py self-check OK")


def market_regression(equity, underlying_px):
    """
    Regress the book's daily returns on the underlying's. Pure function of two series.

    This is the test that decides whether a premium-selling result is a premium or a
    disguised long. Selling 0.30-delta puts into a market that rose 56% is short delta
    by construction: such a book shows a fine Sharpe over 2024-2026 and would have been
    destroyed in 2022, which is not in this sample. Beta near zero with positive alpha
    is a premium being harvested; beta near one with alpha near zero is the index,
    bought through an option chain.

    Returns beta, annualised alpha, the t-statistic of the intercept, and R^2. The
    t-statistic is the point: an alpha of +6.5% a year with t = 0.65 is not an alpha,
    it is the width of the error bar.

    Residual Sharpe is deliberately NOT returned -- least squares forces the residual
    mean to zero, so it is identically zero and carries no information. It was reported
    once here, came out as -1.47, and was believed for several minutes.
    """
    pairs = []
    for i in range(1, min(len(equity), len(underlying_px))):
        if equity[i - 1] and underlying_px[i] and underlying_px[i - 1]:
            pairs.append(((equity[i] - equity[i - 1]) / equity[i - 1],
                          (underlying_px[i] - underlying_px[i - 1]) / underlying_px[i - 1]))
    if len(pairs) < 60:
        return {}
    ys = [p[0] for p in pairs]          # the book
    xs = [p[1] for p in pairs]          # the underlying
    my, mx = sum(ys) / len(ys), sum(xs) / len(xs)
    vxx = sum((x - mx) ** 2 for x in xs)
    if vxx <= 0:
        return {}
    # zip(xs, ys) explicitly. Iterating the pair list binds the BOOK return to the name
    # `x`, crossing it against mx/my and producing a "beta" that is not one -- visible
    # only as an R^2 of -1.23, which least squares with an intercept cannot produce.
    beta = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / vxx
    alpha_d = my - beta * mx
    resid = [y - (alpha_d + beta * x) for x, y in zip(xs, ys)]
    n = len(resid)
    sr = (sum(r * r for r in resid) / (n - 2)) ** 0.5 if n > 2 else 0.0
    se = sr * math.sqrt(1.0 / n + mx * mx / vxx) if sr > 0 else 0.0
    return {'beta_spy': beta,
            'alpha_ann': (1.0 + alpha_d) ** TRADING_DAYS - 1.0,
            'alpha_t': (alpha_d / se) if se else 0.0,
            'r2': 1.0 - sum(r * r for r in resid) / sum((y - my) ** 2 for y in ys)}
