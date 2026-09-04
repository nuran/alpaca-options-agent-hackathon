"""
Operator-locked live-book policy. Not tuned in code — change only with an explicit
decision and a CHANGELOG entry. See STRATEGY.md for the measured ladder.
"""

# Headline cell sizing: FLOOR-12 @ 2% risk, +35.2% ann @3% friction (STRATEGY.md).
# Gap −5% on worst session ≈ −36.4% of equity (backtest/tail_test.py).
LIVE_RISK_PCT = 0.02
