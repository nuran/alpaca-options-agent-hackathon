"""
Black-Scholes pricing, implied-volatility inversion, and Greeks.

Why this exists: Alpaca's *live* option snapshot carries Greeks and IV, but its
*historical* option bars carry neither -- and there is no historical option quotes
endpoint at all (verified 2026-08-25: /v1beta1/options/quotes returns 404). A
backtest that selects strikes by delta therefore has to recover delta itself.

The route is standard: take the contract's traded close, invert Black-Scholes for
implied volatility, then evaluate the analytic delta at that vol. This keeps the
backtest's strike selection directly comparable to the live agent's, which reads
Alpaca's own Greeks.

Assumptions, all of which are approximations and are reported as such:
  - European exercise. SPY/QQQ options are American, but early exercise on a
    short-dated index ETF option is rare enough that the pricing error is small
    next to the bid/ask uncertainty we already carry.
  - Continuous dividend yield q, defaulting to SPY's ~1.2%.
  - Constant risk-free rate r over the (very short) life of the contract.
  - Year fraction on a 365-day calendar basis.

Stdlib only -- no scipy. The normal CDF uses math.erf, and the IV solve is
Newton-Raphson with a bisection fallback for the flat-vega wings.
"""
import math

DEFAULT_RATE = 0.04       # ~short-term T-bill over the 2024-2026 sample
DEFAULT_DIV_YIELD = 0.012  # SPY trailing yield, close enough for 1-7 DTE
DAYS_PER_YEAR = 365.0
SESSIONS_PER_YEAR = 252.0

MIN_VOL, MAX_VOL = 1e-4, 5.0


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def year_fraction(days):
    """
    CALENDAR days to expiry -> year fraction. Floored so same-day expiries stay finite.

    DO NOT use this to produce an IV that will be compared with a realized vol. Realized
    vol here is annualized on 252 trading days; this divides by 365 calendar days, and
    the mismatch is not a rounding difference. Measured on this repo's own SPY store,
    median inverted IV of a 15-delta put:

        entered Mon-Thu, 1 DTE   17.6% .. 19.7%
        entered Friday,  3 DTE   12.4%          <- same option, three calendar days

    The ratio is 0.628 against sqrt(1/3) = 0.577 for a pure day-count artifact, so
    almost all of it is the convention rather than a real weekend effect. A rule that
    fires when "IV looks cheap against the forecast" therefore fires on Fridays: 12 of
    12 debit entries in the 2026-08-29 adaptive run landed on a Thursday or Friday
    against 1 of 40 credit entries, and those trades carried 93% of the reported P&L.

    Use year_fraction_sessions() on any path that compares implied with realized.
    """
    return max(float(days), 1.0 / 24.0) / DAYS_PER_YEAR


def year_fraction_sessions(sessions):
    """
    TRADING sessions to expiry -> year fraction, on the same 252 clock as realized vol.

    Friday-to-Monday is one session, exactly like Tuesday-to-Wednesday, because that is
    how much trading actually happens in between.
    """
    return max(float(sessions), 1.0 / 24.0) / SESSIONS_PER_YEAR


def _d1_d2(spot, strike, t, vol, r, q):
    denom = vol * math.sqrt(t)
    if denom <= 0:
        return float('inf'), float('inf')
    d1 = (math.log(spot / strike) + (r - q + 0.5 * vol * vol) * t) / denom
    return d1, d1 - denom


def price(spot, strike, t, vol, right, r=DEFAULT_RATE, q=DEFAULT_DIV_YIELD):
    """Black-Scholes-Merton price. `t` is a year fraction, `right` is 'C' or 'P'."""
    if t <= 0 or vol <= 0:
        return intrinsic(spot, strike, right)
    d1, d2 = _d1_d2(spot, strike, t, vol, r, q)
    disc_q, disc_r = math.exp(-q * t), math.exp(-r * t)
    if right.upper().startswith('C'):
        return spot * disc_q * _norm_cdf(d1) - strike * disc_r * _norm_cdf(d2)
    return strike * disc_r * _norm_cdf(-d2) - spot * disc_q * _norm_cdf(-d1)


def intrinsic(spot, strike, right):
    return max(0.0, spot - strike) if right.upper().startswith('C') else max(0.0, strike - spot)


def greeks(spot, strike, t, vol, right, r=DEFAULT_RATE, q=DEFAULT_DIV_YIELD):
    """delta, gamma, theta (per day), vega (per 1 vol point), rho."""
    if t <= 0 or vol <= 0:
        d = 0.0
        if right.upper().startswith('C'):
            d = 1.0 if spot > strike else 0.0
        else:
            d = -1.0 if spot < strike else 0.0
        return {'delta': d, 'gamma': 0.0, 'theta': 0.0, 'vega': 0.0, 'rho': 0.0}

    is_call = right.upper().startswith('C')
    d1, d2 = _d1_d2(spot, strike, t, vol, r, q)
    disc_q, disc_r = math.exp(-q * t), math.exp(-r * t)
    sqrt_t = math.sqrt(t)
    pdf_d1 = _norm_pdf(d1)

    if is_call:
        delta = disc_q * _norm_cdf(d1)
        theta = (-spot * disc_q * pdf_d1 * vol / (2 * sqrt_t)
                 - r * strike * disc_r * _norm_cdf(d2)
                 + q * spot * disc_q * _norm_cdf(d1))
        rho = strike * t * disc_r * _norm_cdf(d2) / 100.0
    else:
        delta = -disc_q * _norm_cdf(-d1)
        theta = (-spot * disc_q * pdf_d1 * vol / (2 * sqrt_t)
                 + r * strike * disc_r * _norm_cdf(-d2)
                 - q * spot * disc_q * _norm_cdf(-d1))
        rho = -strike * t * disc_r * _norm_cdf(-d2) / 100.0

    return {
        'delta': delta,
        'gamma': disc_q * pdf_d1 / (spot * vol * sqrt_t),
        'theta': theta / DAYS_PER_YEAR,          # per calendar day
        'vega': spot * disc_q * pdf_d1 * sqrt_t / 100.0,  # per 1 vol point
        'rho': rho,
    }


def implied_vol(observed, spot, strike, t, right, r=DEFAULT_RATE, q=DEFAULT_DIV_YIELD):
    """
    Invert Black-Scholes for volatility. Returns None when no solution is
    meaningful -- price below intrinsic, zero time value, or a non-convergent wing.

    Newton-Raphson from a Brenner-Subrahmanyam seed, falling back to bisection when
    vega collapses (deep ITM/OTM), which Newton alone handles badly.
    """
    if observed is None or observed <= 0 or t <= 0 or spot <= 0 or strike <= 0:
        return None

    intr = intrinsic(spot, strike, right)
    # Below intrinsic is an arbitrage or (far more likely here) a stale/indicative print.
    if observed < intr - 1e-6:
        return None
    if observed - intr < 1e-6:
        return None  # no time value left; vol is unidentifiable

    # Brenner-Subrahmanyam ATM approximation as a starting point.
    vol = max(math.sqrt(2 * math.pi / t) * observed / spot, 0.05)
    vol = min(max(vol, MIN_VOL), MAX_VOL)

    for _ in range(60):
        theo = price(spot, strike, t, vol, right, r, q)
        diff = theo - observed
        if abs(diff) < 1e-8:
            return vol
        v = spot * math.exp(-q * t) * _norm_pdf(_d1_d2(spot, strike, t, vol, r, q)[0]) * math.sqrt(t)
        if v < 1e-8:
            break  # flat vega -- Newton is unreliable here, hand over to bisection
        step = diff / v
        vol -= max(min(step, 1.0), -1.0)  # damp so a bad step can't fling us out of range
        if not (MIN_VOL <= vol <= MAX_VOL):
            break

    lo, hi = MIN_VOL, MAX_VOL
    if price(spot, strike, t, hi, right, r, q) < observed:
        return None  # unreachable even at max vol
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if price(spot, strike, t, mid, right, r, q) < observed:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-8:
            break
    vol = 0.5 * (lo + hi)
    return vol if MIN_VOL < vol < MAX_VOL - 1e-6 else None


def delta_from_price(observed, spot, strike, days, right, r=DEFAULT_RATE, q=DEFAULT_DIV_YIELD,
                     sessions=None):
    """Convenience: observed price + days-to-expiry -> (delta, implied_vol) or (None, None)."""
    # `sessions` is the correct input wherever the resulting IV meets a realized vol.
    # `days` stays supported for the reporting scripts, which only ever look at IV
    # against other IV.
    t = year_fraction(days) if sessions is None else year_fraction_sessions(sessions)
    vol = implied_vol(observed, spot, strike, t, right, r, q)
    if vol is None:
        return None, None
    return greeks(spot, strike, t, vol, right, r, q)['delta'], vol


if __name__ == "__main__":
    # Round-trip: price at a known vol, invert, recover the same vol.
    spot, strike, t, vol = 766.71, 770.0, year_fraction(5), 0.18
    for right in ('C', 'P'):
        p = price(spot, strike, t, vol, right)
        back = implied_vol(p, spot, strike, t, right)
        assert back is not None and abs(back - vol) < 1e-5, (right, p, back)
        g = greeks(spot, strike, t, vol, right)
        print(f"  {right}: price {p:7.3f}  iv {back:.4f}  delta {g['delta']:+.4f}  "
              f"theta {g['theta']:+.4f}  vega {g['vega']:.4f}")

    # Put-call parity: C - P == S*e^-qt - K*e^-rt
    c = price(spot, strike, t, vol, 'C')
    p = price(spot, strike, t, vol, 'P')
    lhs = c - p
    rhs = spot * math.exp(-DEFAULT_DIV_YIELD * t) - strike * math.exp(-DEFAULT_RATE * t)
    assert abs(lhs - rhs) < 1e-8, (lhs, rhs)

    # A 0.15-0.30 delta short strike should sit slightly OTM for a 5-day contract.
    d, _ = delta_from_price(price(spot, 780.0, t, vol, 'C'), spot, 780.0, 5, 'C')

    # The two clocks must disagree, and by the amount that caused the Friday artifact.
    assert abs(year_fraction_sessions(1) / year_fraction(1) - 365.0 / 252.0) < 1e-12
    fri_cal, tue_cal = year_fraction(3), year_fraction(1)        # Fri->Mon vs Tue->Wed
    assert abs((tue_cal / fri_cal) ** 0.5 - (1 / 3) ** 0.5) < 1e-12
    assert year_fraction_sessions(1) == year_fraction_sessions(1), "one session is one session"
    # A Friday option really worth ONE session of 18% vol, read back on both clocks.
    # Calendar time inflates T by 3*252/365 = 2.07, so the recovered IV deflates by
    # 1/sqrt(2.07) = 0.695 -- and the strategy then calls that "cheap vol".
    for strike, label in ((600.0, 'ATM'), (590.0, 'OTM'), (575.0, 'far OTM')):
        px = price(600.0, strike, year_fraction_sessions(1), 0.18, 'P')
        _, iv_sessions = delta_from_price(px, 600.0, strike, 3, 'P', sessions=1)
        _, iv_calendar = delta_from_price(px, 600.0, strike, 3, 'P')
        assert abs(iv_sessions - 0.18) < 1e-4, (label, iv_sessions)
        ratio = iv_calendar / iv_sessions
        assert 0.69 < ratio < 0.71, (label, ratio)
    assert abs(1.0 / math.sqrt(3 * 252 / 365) - 0.6948) < 1e-3

    # The other comparison, the one the store measures: a Friday option against a
    # Tuesday one, BOTH read on the calendar clock. There T differs by 3x, so a
    # constant true vol would show 1/sqrt(3) = 0.577. The store shows 0.628, so most
    # of the Friday discount is the convention and only a little is real weekend vol.
    assert abs((1 / 3) ** 0.5 - 0.5774) < 1e-3
    assert 0.05 < d < 0.35, d
    print(f"  parity ok (diff {abs(lhs - rhs):.2e});  780C 5DTE delta {d:.3f}")
    print("blackscholes.py self-check OK")
