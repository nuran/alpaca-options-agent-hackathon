"""
Fill modelling for the options backtest.

**The central limitation of this backtest, stated up front.** Alpaca serves no
historical option *quotes* -- `/v1beta1/options/quotes` returns 404, and only
`/quotes/latest` exists (verified 2026-08-25). So there is no historical bid/ask to
fill against. Everything here works from the contract's traded prices (daily bars)
plus an explicit, configurable friction assumption.

That means a single headline P&L number from this backtest is not trustworthy on its
own. What IS trustworthy is the SHAPE of the result across a friction sweep: if a
strategy is profitable at 1% friction and dead at 6%, that tells you the edge is
smaller than the spread and the strategy is not real. `run.py --sweep` exists to
force that question rather than let it be assumed away.

Friction is calibrated from live observation rather than guessed. On 2026-08-25 the
SPY chain quoted (indicative feed):

    |delta| 0.15-0.30 (the credit-spread target zone)   ~1.4% of mid
    |delta| 0.30-0.45                                   ~1.3% of mid
    chain-wide median                                   ~6.3% of mid
    chain-wide p75                                     ~10.1% of mid

The target band is materially tighter than the chain as a whole, which is what makes
the strategy plausible at all. DEFAULT_FRICTION_PCT is set at 3% -- roughly double
the observed target-band spread, so the base case is deliberately pessimistic.

A 2026-08-29 indicative re-probe of the *traded* zone (15-delta, DTE 4-7) landed at
median ~8.7% of mid on n=5 quotes (weekend sample). That is too thin to move this
default; re-check on a weekday open before treating 1% as the honest row. Absolute
cents in that pocket were still small (~$0.04 median spread). Use --sweep.

The skill's bar-based fallback convention is followed:
    buy  fill = price * (1 + friction)
    sell fill = price * (1 - friction)
where friction = (spread_bps + slippage_bps) / 10000, here expressed as a fraction.
"""
import re

DEFAULT_FRICTION_PCT = 0.03
SWEEP_FRICTIONS = (0.01, 0.03, 0.06)

# Per-contract fees. Alpaca charges $0 commission on options but passes through
# regulatory and clearing fees. Modelled: OCC clearing, ORF, SEC, FINRA TAF.
# Source: https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf
# These are small next to spread friction but are not zero, and the skill requires
# that any excluded category be reported rather than silently dropped.
FEE_PER_CONTRACT = 0.065          # OCC + ORF, both sides
SEC_FEE_RATE = 0.0000278          # sells only, on notional premium
TAF_PER_CONTRACT_SELL = 0.00279   # FINRA TAF, sells only

CONTRACT_MULTIPLIER = 100


class MeasuredSpread:
    """
    The book as it actually is on SPY: penny-wide, not proportional.

    Calibrated on 616 quotes from the live chain snapshots in reports/eda/, restricted
    to the region this book trades (|delta| 0.02-0.45, DTE >= 2). Deep in-the-money
    contracts are excluded deliberately -- their median half-spread is $1.09 and
    including them moves the fit by a factor of four for options we never touch.

        half-spread = max($0.005, 1.0% of mid)      mean error $0.008

    Why this matters more than the level. The measured half-spread is nearly CONSTANT
    in dollars -- half a cent to two cents across the whole traded range -- so as a
    percentage it runs 14% on a $0.04 wing and 0.8% on a $2.79 near-the-money option.
    A flat percentage gets the shape backwards: it overcharges the short leg and
    undercharges the far wing, which is exactly the comparison that decides between a
    two-leg and a four-leg structure.

    Round trip on the condor this book actually trades: $6.00 per contract measured,
    against $9.00 under the flat 3% model. The old assumption was not conservative --
    it was wrong by half in the expensive direction.

    Caveats, stated because they bound the conclusion: two snapshots, 26 and 29 August
    2026, both in a calm regime (VIX 14.5, third percentile of its year). Spreads widen
    in stress, which is when this book loses. Treat this as the floor of the cost, not
    the average of it.
    """

    # Median half-spread by mid, from the traded region of the two snapshots. Kept as
    # anchors and interpolated rather than fitted to a formula: an L1 fit is dragged by
    # the many cheap contracts and came out undercharging the $0.40-1.50 range -- where
    # the short leg lives -- by a factor of two. The data does not have a two-parameter
    # shape, so none is imposed. Monotonised on load: the dip at $0.96 is n=14 noise,
    # and a spread model that gets cheaper as the option gets richer is not a model.
    ANCHORS = ((0.075, 0.0050), (0.130, 0.0050), (0.245, 0.0050), (0.440, 0.0100),
               (0.657, 0.0150), (0.962, 0.0150), (1.485, 0.0150), (2.360, 0.0250))

    def __init__(self, anchors=None, label='measured-2026-08'):
        pts = sorted(anchors or self.ANCHORS)
        run, out = 0.0, []
        for m, h in pts:
            run = max(run, h)
            out.append((m, run))
        self.pts, self.label = out, label

    def half_spread(self, mark):
        m = float(mark)
        pts = self.pts
        if m <= pts[0][0]:
            return pts[0][1]
        if m >= pts[-1][0]:
            # Beyond the calibrated range, hold the last observed percentage rather
            # than the last absolute cent -- a $20 option is not two and a half cents wide.
            return m * (pts[-1][1] / pts[-1][0])
        for (m0, h0), (m1, h1) in zip(pts, pts[1:]):
            if m0 <= m <= m1:
                w = (m - m0) / (m1 - m0)
                return h0 + w * (h1 - h0)
        return pts[-1][1]

    def to_json(self):
        """Provenance for the run folder: the cost model IS part of the result."""
        return {'model': 'MeasuredSpread', 'label': self.label,
                'anchors_mid_to_half_spread': [[m, h] for m, h in self.pts],
                'source': 'reports/eda/output/live_snapshot_*.json, |delta| 0.02-0.45, DTE>=2',
                'caveat': 'two calm sessions (VIX 14.5, 3rd pct) -- a floor on cost, not an average'}

    def __format__(self, spec):
        """
        Render under a NUMERIC format spec without throwing.

        Friction used to be a float and every report line formats it as a percent:
        `{f:>5.1%}` in the results line, `{f:.0%}` in the Teaching Five header, in
        report.md, in the sweep table and in notes.md. Widening the type to an object
        broke all five at once. Rather than chase each call site -- and miss one --
        the object honours the alignment and width it is given and drops the numeric
        precision and type, which mean nothing for a model.
        """
        m = re.match(r'^(?:(.)?([<>^=]))?[+\- ]?#?0?(\d*)', spec or '')
        fill, align, width = (m.group(1) or ''), (m.group(2) or '>'), (m.group(3) or '')
        return format(self.label, f"{fill}{align}{width}")

    def __repr__(self):
        return f"MeasuredSpread({self.label}, {len(self.pts)} anchors)"


def fill_price(mark, side, friction_pct=DEFAULT_FRICTION_PCT):
    """
    Model an execution price for one leg.

    `mark` is the observed traded price (a bar close, typically). Buying pays up,
    selling receives less -- friction always works against the trade.

    `friction_pct` is either a fraction of the mark (the sweep) or a MeasuredSpread,
    which charges an absolute half-spread instead. The sweep stays: a calibration from
    two calm days is a point estimate, and the shape of the result across assumed costs
    is still what has to be reported.
    """
    if mark is None or mark <= 0:
        return None
    half = (friction_pct.half_spread(mark) if hasattr(friction_pct, 'half_spread')
            else mark * friction_pct)
    if side == 'buy':
        return mark + half
    if side == 'sell':
        return max(mark - half, 0.0)
    raise ValueError(f"side must be buy or sell, got {side!r}")


def leg_fees(price, qty, side):
    """Regulatory + clearing fees for one option leg, in dollars."""
    contracts = abs(qty)
    fees = FEE_PER_CONTRACT * contracts
    if side == 'sell':
        notional = price * CONTRACT_MULTIPLIER * contracts
        fees += notional * SEC_FEE_RATE
        fees += TAF_PER_CONTRACT_SELL * contracts
    return fees


def spread_entry(short_mark, long_mark, qty=1, friction_pct=DEFAULT_FRICTION_PCT):
    """
    Open a vertical credit spread: sell the near strike, buy the far one.

    Returns (net_credit_per_spread, fees) or (None, 0) when either leg is unpriceable.
    net_credit is per single spread, before the 100x multiplier.
    """
    sell_at = fill_price(short_mark, 'sell', friction_pct)
    buy_at = fill_price(long_mark, 'buy', friction_pct)
    if sell_at is None or buy_at is None:
        return None, 0.0
    credit = sell_at - buy_at
    fees = leg_fees(sell_at, qty, 'sell') + leg_fees(buy_at, qty, 'buy')
    return credit, fees


def spread_exit(short_mark, long_mark, qty=1, friction_pct=DEFAULT_FRICTION_PCT):
    """
    Close a vertical credit spread: buy back the short, sell the long.

    Returns (net_debit_per_spread, fees). Friction reverses -- it costs you again.
    """
    buy_at = fill_price(short_mark, 'buy', friction_pct)
    sell_at = fill_price(long_mark, 'sell', friction_pct)
    if buy_at is None or sell_at is None:
        return None, 0.0
    debit = buy_at - sell_at
    fees = leg_fees(buy_at, qty, 'buy') + leg_fees(sell_at, qty, 'sell')
    return debit, fees


def settle_at_expiry(spot, short_strike, long_strike, right):
    """
    Intrinsic value of the spread at expiry, per single spread.

    Cash settlement is modelled: no assignment mechanics, no early exercise, no
    pin risk. For SPY/QQQ (American, physically settled) this is an approximation,
    but the spread is always closed or expires worthless/max-loss in the same way.
    """
    if right.upper().startswith('C'):
        short_intr = max(0.0, spot - short_strike)
        long_intr = max(0.0, spot - long_strike)
    else:
        short_intr = max(0.0, short_strike - spot)
        long_intr = max(0.0, long_strike - spot)
    # We are short the near strike and long the far one, so we owe the difference.
    return short_intr - long_intr


def realized_pnl(credit, debit, qty, entry_fees, exit_fees):
    """
    Dollar P&L of a closed credit spread.

    Credit received minus debit paid to close, times the 100x multiplier and the
    number of spreads, minus all fees on both sides.
    """
    gross = (credit - debit) * CONTRACT_MULTIPLIER * qty
    return gross - entry_fees - exit_fees


def max_loss(width, credit, qty):
    """Worst case on a defined-risk vertical: the width less the credit taken in."""
    return max(0.0, (width - credit)) * CONTRACT_MULTIPLIER * qty


def spread_debit_entry(long_mark, short_mark, qty=1, friction_pct=DEFAULT_FRICTION_PCT):
    """Open a debit vertical: buy the near strike, sell the far one."""
    buy_at = fill_price(long_mark, 'buy', friction_pct)
    sell_at = fill_price(short_mark, 'sell', friction_pct)
    if buy_at is None or sell_at is None:
        return None, 0.0
    debit = buy_at - sell_at
    fees = leg_fees(buy_at, qty, 'buy') + leg_fees(sell_at, qty, 'sell')
    return debit, fees


def spread_debit_exit(long_mark, short_mark, qty=1, friction_pct=DEFAULT_FRICTION_PCT):
    """Close a debit vertical: sell the long, buy back the short. Returns credit received."""
    sell_at = fill_price(long_mark, 'sell', friction_pct)
    buy_at = fill_price(short_mark, 'buy', friction_pct)
    if sell_at is None or buy_at is None:
        return None, 0.0
    credit = sell_at - buy_at
    fees = leg_fees(sell_at, qty, 'sell') + leg_fees(buy_at, qty, 'buy')
    return credit, fees


def realized_pnl_debit(entry_debit, exit_credit, qty, entry_fees, exit_fees):
    gross = (exit_credit - entry_debit) * CONTRACT_MULTIPLIER * qty
    return gross - entry_fees - exit_fees


def debit_max_loss(debit, qty):
    """Worst case on a debit vertical: the premium paid."""
    return max(0.0, debit) * CONTRACT_MULTIPLIER * qty


def settle_debit_at_expiry(spot, long_strike, short_strike, right):
    """Intrinsic of a long vertical at expiry (what we receive)."""
    return -settle_at_expiry(spot, short_strike, long_strike, right)


# ------------------------------------------------------------- iron condors

def condor_entry(put_short, put_long, call_short, call_long, qty=1,
                 friction_pct=DEFAULT_FRICTION_PCT):
    """
    Open an iron condor: a put credit spread and a call credit spread on one expiry.

    Returns (total_credit_per_condor, fees). Four legs, so four lots of friction --
    the cost of being delta-neutral is paying the spread twice.
    """
    put_credit, put_fees = spread_entry(put_short, put_long, qty, friction_pct)
    call_credit, call_fees = spread_entry(call_short, call_long, qty, friction_pct)
    if put_credit is None or call_credit is None:
        return None, 0.0
    return put_credit + call_credit, put_fees + call_fees


def condor_exit(put_short, put_long, call_short, call_long, qty=1,
                friction_pct=DEFAULT_FRICTION_PCT):
    """Close both sides. Returns (total_debit_per_condor, fees)."""
    put_debit, put_fees = spread_exit(put_short, put_long, qty, friction_pct)
    call_debit, call_fees = spread_exit(call_short, call_long, qty, friction_pct)
    if put_debit is None or call_debit is None:
        return None, 0.0
    return put_debit + call_debit, put_fees + call_fees


def condor_max_loss(put_width, call_width, total_credit, qty):
    """
    Worst case on an iron condor.

    The key structural advantage over a single vertical: the underlying can only
    finish through ONE side, so the risk is the WIDER wing less the TOTAL credit --
    not the sum of both wings. Collecting two credits against one wing's width is
    what lowers the break-even win rate.
    """
    return max(0.0, max(put_width, call_width) - total_credit) * CONTRACT_MULTIPLIER * qty


def settle_condor_at_expiry(spot, put_short, put_long, call_short, call_long):
    """Intrinsic owed at expiry across both wings; at most one can be non-zero."""
    return (settle_at_expiry(spot, put_short, put_long, 'P')
            + settle_at_expiry(spot, call_short, call_long, 'C'))


def two_short_entry(put_mark, call_mark, qty=1, friction_pct=DEFAULT_FRICTION_PCT):
    """Open a short strangle/straddle: sell both sides, no longs."""
    put_at = fill_price(put_mark, 'sell', friction_pct)
    call_at = fill_price(call_mark, 'sell', friction_pct)
    if put_at is None or call_at is None:
        return None, 0.0
    credit = put_at + call_at
    fees = leg_fees(put_at, qty, 'sell') + leg_fees(call_at, qty, 'sell')
    return credit, fees


def two_short_exit(put_mark, call_mark, qty=1, friction_pct=DEFAULT_FRICTION_PCT):
    """Buy both shorts back."""
    put_at = fill_price(put_mark, 'buy', friction_pct)
    call_at = fill_price(call_mark, 'buy', friction_pct)
    if put_at is None or call_at is None:
        return None, 0.0
    debit = put_at + call_at
    fees = leg_fees(put_at, qty, 'buy') + leg_fees(call_at, qty, 'buy')
    return debit, fees


def settle_strangle_at_expiry(spot, put_k, call_k):
    """What a short strangle/straddle owes at expiry, per spread."""
    return max(0.0, put_k - spot) + max(0.0, spot - call_k)


if __name__ == "__main__":
    # A 2-wide put credit spread taking $0.60, closed at $0.30, one contract.
    credit, ef = spread_entry(1.20, 0.60, qty=1, friction_pct=0.0)
    assert abs(credit - 0.60) < 1e-12, credit
    debit, xf = spread_exit(0.60, 0.30, qty=1, friction_pct=0.0)
    assert abs(debit - 0.30) < 1e-12, debit
    pnl = realized_pnl(credit, debit, 1, ef, xf)
    assert abs(pnl - (30.0 - ef - xf)) < 1e-9, pnl
    print(f"  frictionless: credit {credit:.2f} debit {debit:.2f} -> P&L ${pnl:.2f} "
          f"(fees ${ef + xf:.2f})")

    # Friction must always reduce the credit taken in and increase the cost to close.
    c1, _ = spread_entry(1.20, 0.60, friction_pct=0.03)
    d1, _ = spread_exit(0.60, 0.30, friction_pct=0.03)
    assert c1 < credit and d1 > debit, (c1, d1)
    print(f"  3% friction:  credit {c1:.4f} (-{(credit-c1)/credit:.1%})  "
          f"debit {d1:.4f} (+{(d1-debit)/debit:.1%})")

    # Max loss on a 2-wide spread with 0.60 credit == 140 per contract.
    assert abs(max_loss(2.0, 0.60, 1) - 140.0) < 1e-9

    d_in, d_ef = spread_debit_entry(1.20, 0.60, qty=1, friction_pct=0.0)
    assert abs(d_in - 0.60) < 1e-12, d_in
    d_out, d_xf = spread_debit_exit(1.50, 0.40, qty=1, friction_pct=0.0)
    assert abs(d_out - 1.10) < 1e-12, d_out
    d_pnl = realized_pnl_debit(d_in, d_out, 1, d_ef, d_xf)
    assert abs(d_pnl - (50.0 - d_ef - d_xf)) < 1e-9, d_pnl
    assert abs(debit_max_loss(0.60, 1) - 60.0) < 1e-9
    assert abs(settle_debit_at_expiry(755, 760, 758, 'P') - 2.0) < 1e-12

    # Expiry settlement: put spread 760/758, spot 755 -> full max loss of width.
    assert abs(settle_at_expiry(755, 760, 758, 'P') - 2.0) < 1e-12
    assert settle_at_expiry(770, 760, 758, 'P') == 0.0        # both worthless
    assert abs(settle_at_expiry(765, 760, 758, 'P') - 0.0) < 1e-12
    # Iron condor: two wings, but only one can finish in the money, so the risk is
    # the WIDER wing less the TOTAL credit -- that is the whole structural point.
    cc, cf = condor_entry(1.20, 0.47, 1.20, 0.47, 1, 0.0)
    assert abs(cc - 1.46) < 1e-9, cc
    ml = condor_max_loss(5.0, 5.0, cc, 1)
    assert abs(ml - 354.0) < 1e-9, ml
    be_condor = (5.0 - cc) / 5.0
    be_single = (5.0 - 0.73) / 5.0
    assert be_condor < be_single
    print(f"  condor:       credit {cc:.2f} on one 5-wide wing -> max loss ${ml:,.0f}, "
          f"break-even {be_condor:.1%} (vs {be_single:.1%} single)")

    # At most one wing settles in the money.
    assert settle_condor_at_expiry(700, 760, 755, 790, 795) == 5.0
    assert settle_condor_at_expiry(900, 760, 755, 790, 795) == 5.0
    assert settle_condor_at_expiry(775, 760, 755, 790, 795) == 0.0

    cr, _ = two_short_entry(1.20, 1.10, 1, 0.0)
    assert abs(cr - 2.30) < 1e-9, cr
    db, _ = two_short_exit(0.40, 0.50, 1, 0.0)
    assert abs(db - 0.90) < 1e-9, db
    assert abs(settle_strangle_at_expiry(700, 760, 790) - 60.0) < 1e-9
    assert settle_strangle_at_expiry(775, 760, 790) == 0.0

    print("fills.py self-check OK")
