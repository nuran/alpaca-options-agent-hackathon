"""
OCC option symbol construction and parsing.

Alpaca's /v2/options/contracts endpoint only lists *active* contracts -- it returns
nothing for past expiries (verified 2026-08-25). Historical option bars and trades,
however, are served happily for expired contracts. So a backtest cannot enumerate a
historical chain; it has to CONSTRUCT the symbols it wants and probe for data.
Non-existent strikes simply return no bars, which is cheap.

Format: {UNDERLYING}{YYMMDD}{C|P}{STRIKE * 1000, zero-padded to 8}
Example: SPY, 2026-01-16, call, $250 -> SPY260116C00250000
"""
import datetime as dt
import re

OCC_RE = re.compile(r'^(?P<root>[A-Z]+)(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})(?P<right>[CP])(?P<strike>\d{8})$')


def build(underlying, expiry, right, strike):
    """
    Build an OCC symbol.

    underlying: 'SPY'
    expiry:     datetime.date or 'YYYY-MM-DD'
    right:      'C' | 'P' (case-insensitive; 'call'/'put' also accepted)
    strike:     float dollars, e.g. 570.0 or 570.5
    """
    if isinstance(expiry, str):
        expiry = dt.date.fromisoformat(expiry)

    r = right.upper()[0]
    if r not in ('C', 'P'):
        raise ValueError(f"right must be C or P, got {right!r}")

    # Strikes are quoted in thousandths. round() rather than int() because
    # 570.5 * 1000 is 570499.9999... in binary floating point.
    thousandths = int(round(float(strike) * 1000))
    if thousandths <= 0 or thousandths > 99_999_999:
        raise ValueError(f"strike out of representable range: {strike}")

    return f"{underlying.upper()}{expiry:%y%m%d}{r}{thousandths:08d}"


def parse(symbol):
    """Inverse of build(). Returns (underlying, expiry_date, right, strike) or raises."""
    m = OCC_RE.match(symbol.strip().upper())
    if not m:
        raise ValueError(f"not an OCC symbol: {symbol!r}")
    g = m.groupdict()
    expiry = dt.date(2000 + int(g['yy']), int(g['mm']), int(g['dd']))
    return g['root'], expiry, g['right'], int(g['strike']) / 1000.0


def strike_ladder(spot, band_pct=0.08, increment=1.0):
    """
    Candidate strikes within +/- band_pct of spot, snapped to `increment`.

    SPY and QQQ list $1 increments near the money (with $0.50 on some weeklies and
    $5 far out). Generating a $1 ladder and letting non-existent strikes come back
    empty is cheaper than trying to model each underlying's exact strike schedule.
    """
    lo = spot * (1 - band_pct)
    hi = spot * (1 + band_pct)
    first = round(lo / increment) * increment
    out = []
    k = first
    while k <= hi + 1e-9:
        if k > 0:
            out.append(round(k, 2))
        k += increment
    return out


def chain_symbols(underlying, expiry, spot, band_pct=0.08, increment=1.0, rights=('C', 'P')):
    """Every candidate OCC symbol for one underlying/expiry around a given spot."""
    return [
        build(underlying, expiry, r, k)
        for k in strike_ladder(spot, band_pct, increment)
        for r in rights
    ]


def union_ladder(spots, band_pct=0.08, increment=1.0):
    """
    Union of the per-day ladders for a list of daily closes, sorted.

    A single ladder anchored on one day's price is wrong for a multi-day window,
    because the price moves inside it. Measured on this repo's own store: anchoring
    the whole window on the close 12 days before expiry left a median reach of only
    +4.4% above the eventual price, and 47 of 636 expiries settled outside the
    ingested strike range entirely -- with the loss tail sitting on the truncated
    (upside) side.

    `band_pct` may be one number for the whole window, or one per spot. Per-spot is
    what a long window needs: a session 40 days from expiry has to reach far enough to
    hold a 0.15-delta short (about 1 sigma-root-T out), while a session one day from
    expiry needs almost nothing. One flat band sized for the far end would download an
    enormous ladder for the near end, and one sized for the near end would truncate the
    far end -- which is the defect this whole function exists to fix, arriving from the
    other direction.

    Each day contributes a ladder around ITS OWN close, so every strike in the result
    was near the money on some day the backtest can trade. That keeps the selection
    contemporaneous: a strike is downloaded because it was tradable on a day in the
    window, not because the price later went there. The union is only the download
    set; what a backtest may use on day d is still whatever printed on day d.
    """
    if isinstance(band_pct, (int, float)):
        bands = [band_pct] * len(spots)
    else:
        bands = list(band_pct)
        if len(bands) != len(spots):
            raise ValueError(f"{len(spots)} spots but {len(bands)} bands")
    out = set()
    for s, b in zip(spots, bands):
        if s and b:
            out.update(strike_ladder(s, b, increment))
    return sorted(out)


def chain_symbols_union(underlying, expiry, spots, band_pct=0.08, increment=1.0,
                        rights=('C', 'P')):
    """Candidate OCC symbols covering every daily close in the approach window.

    `band_pct` is a scalar or one value per spot; see union_ladder.
    """
    return [
        build(underlying, expiry, r, k)
        for k in union_ladder(spots, band_pct, increment)
        for r in rights
    ]


if __name__ == "__main__":
    # Round-trip self-check.
    s = build("SPY", "2026-01-16", "C", 250)
    assert s == "SPY260116C00250000", s
    assert parse(s) == ("SPY", dt.date(2026, 1, 16), "C", 250.0)
    assert build("SPY", dt.date(2025, 3, 7), "p", 570.5) == "SPY250307P00570500"
    # 766.71 +/- 1% == 759.04 .. 774.38, snapped to whole dollars.
    ladder = strike_ladder(766.71, band_pct=0.01, increment=1.0)
    assert ladder[0] == 759.0 and ladder[-1] == 774.0, ladder
    assert all(b - a == 1.0 for a, b in zip(ladder, ladder[1:])), ladder

    # union_ladder: a window that drifts must still cover BOTH ends at full band.
    spots = [700.0, 710.0, 725.0]                      # +3.6% drift across the window
    u = union_ladder(spots, band_pct=0.02, increment=1.0)
    assert u[0] == round(700.0 * 0.98), u[:3]          # low end anchored on the first day
    assert u[-1] == int(725.0 * 1.02), u[-3:]          # high end on the last
    assert all(b - a == 1.0 for a, b in zip(u, u[1:])), "union left a hole"
    # ...and the single-anchor version does NOT: this is the defect being fixed.
    one = strike_ladder(spots[0], band_pct=0.02, increment=1.0)
    assert one[-1] < u[-1], "single-anchor ladder should fall short of the drifted price"
    assert max(one) < 725.0, "the last day's price is outside a ladder anchored on the first"
    assert union_ladder([None, 700.0], 0.01, 1.0) == strike_ladder(700.0, 0.01, 1.0)
    assert union_ladder([], 0.01, 1.0) == []

    # Per-spot bands: a far-from-expiry session reaches wide, a near one stays narrow,
    # and the union is bounded by the widest -- not by the widest applied everywhere.
    per = union_ladder([700.0, 700.0], [0.01, 0.05], 1.0)
    assert per[0] == round(700 * 0.95) and per[-1] == int(700 * 1.05), (per[0], per[-1])
    assert len(per) < len(union_ladder([700.0, 700.0], 0.05, 1.0)) + 1
    try:
        union_ladder([700.0, 710.0], [0.01], 1.0)
        raise AssertionError("mismatched band count was accepted")
    except ValueError:
        pass
    print(f"occ.py self-check OK  ({s}, ladder around 766.71 -> {ladder[0]}..{ladder[-1]}, "
          f"union over {spots} -> {u[0]}..{u[-1]} vs single-anchor {one[0]}..{one[-1]})")
