"""
Event-driven backtest engine for short-dated vertical credit spreads.

One pass per trading day, in this order:

    1. Mark and manage open positions (take-profit, stop-loss, expiry settlement).
    2. If capacity allows, generate candidates for today and open one.
    3. Record the day's equity.

Execution convention follows the vendored Alpaca skill's default: a signal formed
from day T's close is filled using day T's close *with friction applied against us*.
That is a `same_bar` fill, which the skill warns carries look-ahead risk -- and it
genuinely does. It is used here deliberately because daily option bars are the only
history available (no historical quotes exist), and the alternative -- filling at
T+1's open -- would introduce an overnight gap that is larger than the entire edge
being measured. The friction sweep is what bounds the resulting optimism; see
fills.py. This is recorded in every run's warnings.json rather than left implicit.

No look-ahead beyond that: position management on day T only ever reads day T bars,
and expiry settlement uses the underlying close on the expiry date itself.
"""
import datetime as dt

import fills
import strategy


NAKED = frozenset({'short_strangle', 'short_straddle'})


class Position:
    """One open structure: defined-risk spread, calendar, or 2-leg short."""

    def __init__(self, spread, qty, premium, entry_fees, entry_date, entry_spot,
                 side='credit'):
        self.last_mark = None
        self.spread = spread
        self.qty = qty
        self.side = spread.get('side') or side
        self.credit = premium if self.side != 'debit' else 0.0
        self.debit = premium if self.side == 'debit' else 0.0
        self.entry_fees = entry_fees
        self.entry_date = entry_date
        self.entry_spot = entry_spot
        st = spread.get('structure')
        if spread.get('max_loss_unit'):
            self.max_loss = float(spread['max_loss_unit']) * qty
        elif self.side == 'debit':
            self.max_loss = fills.debit_max_loss(premium, qty)
        elif st == 'condor':
            self.max_loss = fills.condor_max_loss(
                spread['put_width'], spread['call_width'], premium, qty)
        else:
            self.max_loss = fills.max_loss(spread['width'], premium, qty)

    @property
    def expiry(self):
        return self.spread['expiry']

    def to_row(self):
        s = self.spread
        return {
            'entry_date': self.entry_date, 'kind': s['kind'], 'expiry': s['expiry'],
            'dte_at_entry': s['dte'], 'short_occ': s['short_occ'], 'long_occ': s['long_occ'],
            'short_strike': s['short_strike'], 'long_strike': s['long_strike'],
            'width': s['width'], 'qty': self.qty, 'credit': self.credit,
            'debit': self.debit, 'side': self.side,
            'short_delta': s['short_delta'], 'short_iv': s['short_iv'],
            'entry_spot': self.entry_spot, 'max_loss': self.max_loss,
            'structure': s.get('structure', 'vertical'),
            'underlying': s.get('underlying'),
            'portrait': s.get('portrait'),
            'setup_trend': s.get('setup_trend'),
            'call_short_strike': s.get('call_short_strike'),
            'call_long_strike': s.get('call_long_strike'),
            'call_short_occ': s.get('call_short_occ'),
            'call_long_occ': s.get('call_long_occ'),
            'put_short_occ': s.get('put_short_occ'),
            'far_expiry': s.get('far_expiry'),
            'net_delta': s.get('net_delta'),
            'iv_rv_ratio': s.get('iv_rv_ratio'),
        }


def event_inside_horizon(events, date, max_dte):
    """
    True if any ISO event date lies in [date, date + max_dte]. Shared with the agent.

    The event day itself counts: FOMC releases at 14:00 ET, so a position opened that
    morning carries the jump; CPI/NFP release pre-market, and treating the day as
    blocked costs at most one entry. Conservative on purpose.
    """
    if not events:
        return False
    hi = date + dt.timedelta(days=int(max_dte))
    for e in events:
        try:
            d = e if isinstance(e, dt.date) else dt.date.fromisoformat(str(e)[:10])
        except ValueError:
            continue
        if date <= d <= hi:
            return True
    return False


def event_in_life(events, date, expiry):
    """
    True if an event falls inside the life of THIS trade: [date, expiry], inclusive.

    The day-level `event_inside_horizon` above blocks a whole session using max_dte --
    the CEILING of the DTE range, not the expiry actually chosen. That is right when
    every candidate expires at the ceiling and wrong the moment the range widens: at
    DTE 1-7 it blacks out ~56 sessions a year around eight FOMC dates, at DTE 8-15 it
    blacks out ~120, and the extra ones are days whose candidates would have expired
    BEFORE the event. Measured cost: the DTE 8-15 run produced 59 trades where DTE 1-7
    produced 84, and the difference was blackout, not opportunity -- which is why the
    exposure test could not be read.

    A position is exposed to a jump it is open through. Nothing else.
    """
    if not events:
        return False
    for e in events:
        try:
            d = e if isinstance(e, dt.date) else dt.date.fromisoformat(str(e)[:10])
        except ValueError:
            continue
        if date <= d <= expiry:
            return True
    return False


class Engine:
    def __init__(self, store, config):
        """
        store:  object exposing trading_days / underlying_close / chain_for /
                option_close (see backtest/store.py)
        config: dict of strategy + risk settings
        """
        self.store = store
        self.cfg = config
        self.friction = config.get('friction_pct', fills.DEFAULT_FRICTION_PCT)
        self.exits = strategy.exit_rules(config)

        self.cash = float(config.get('initial_cash', 100_000))
        # 'cash' reproduces every run made before 2026-08-30 byte for byte; 'mtm' marks
        # the open book daily. At 0.4 average open positions the two are nearly the same
        # curve. At ten they are not, and only one of them can see a drawdown that has
        # not been closed yet.
        self.equity_basis = config.get('equity_basis', 'cash')
        self._equity_now = self.cash
        self._ret_window = []      # recent daily marked returns, for the vol overlay
        self.equity_curve = []
        self.trades = []
        self.warnings = []
        self.open_positions = []
        self.halted = False
        self._sess_cache = {}

    # ------------------------------------------------------------------ sizing

    def unit_risk(self, spread, premium):
        """Defined max loss of ONE spread, in dollars."""
        if spread.get('max_loss_unit'):
            return max(float(spread['max_loss_unit']), 0.01)
        if (spread.get('side') or 'credit') == 'debit':
            return max(premium, 0.01) * fills.CONTRACT_MULTIPLIER
        if spread.get('structure') == 'condor':
            risk_width = max(spread['put_width'], spread['call_width'])
        else:
            risk_width = spread['width']
        return max(risk_width - premium, 0.01) * fills.CONTRACT_MULTIPLIER

    def committed_risk(self):
        return sum(p.max_loss for p in self.open_positions)

    def risk_scale(self):
        """
        Multiplier on the per-trade risk budget, from a volatility target.

        Flat leverage on a short-premium book is not flat risk. The book's own
        volatility moves by a factor of three between a calm tape and a stressed one,
        and a fixed percentage of equity therefore buys three times the risk exactly
        when the risk is worst. That is visible in the loading sweep as a cliff: return
        scales with risk per trade up to ~10% and then goes NEGATIVE at 20%, with
        drawdown past -88% -- the signature of variance drag, not of a worse edge
        (Sharpe is unchanged across the whole ladder).

        With `vol_target_pct` set, the budget is scaled by target / trailing realised
        volatility of the marked equity curve, clipped so the overlay can neither
        vanish nor become its own leverage decision. Off by default.
        """
        target = self.cfg.get('vol_target_pct')
        if not target:
            return 1.0
        win = self._ret_window
        if len(win) < 15:
            return 1.0
        mean = sum(win) / len(win)
        var = sum((r - mean) ** 2 for r in win) / (len(win) - 1)
        vol = (var ** 0.5) * (252 ** 0.5)
        if vol <= 1e-9:
            return float(self.cfg.get('vol_scale_max', 2.0))
        scale = float(target) / vol
        return max(float(self.cfg.get('vol_scale_min', 0.25)),
                   min(float(self.cfg.get('vol_scale_max', 2.0)), scale))

    def size_position(self, spread, premium):
        """
        Contracts to trade, from the per-trade risk budget.

        Risk is the defined max loss of the spread, so sizing is exact rather than
        estimated -- the whole point of using verticals rather than naked shorts.

        Two regimes. Without `max_portfolio_risk_pct` the budget is a percentage of
        `current_equity()` -- cash LESS what is already committed -- which is the
        original behaviour and shrinks every position as the book fills. With the cap
        set, the per-trade budget is a percentage of equity and the BOOK cap is what
        limits total exposure, which is the only way to state a risk budget that a
        reader can check against the drawdown.
        """
        per_spread_risk = self.unit_risk(spread, premium)
        cap_pct = self.cfg.get('max_portfolio_risk_pct')
        if cap_pct:
            equity = self._equity_now if self.equity_basis == 'mtm' else self.cash
            scale = self.risk_scale()
            budget = equity * self.cfg.get('max_risk_per_trade_pct', 0.02) * scale
            headroom = max(0.0, equity * float(cap_pct) * scale - self.committed_risk())
            if self.cfg.get('risk_alloc') == 'per_name':
                # Each TRADE taking a fixed percentage of equity is not an allocation --
                # it hands the book to whichever name generates the most candidates. On
                # the sixteen-name book that put 21% of the risk-days in QQQ and 0.2% in
                # TLT, so the breadth that was ingested was never actually deployed.
                # Here the cap is divided equally between the names and a name's tranches
                # divide its own share.
                names = self.cfg.get('underlyings') or [self.cfg['underlying']]
                und = spread.get('underlying') or self.cfg['underlying']
                name_cap = equity * float(cap_pct) * scale / max(1, len(names))
                held = sum(p.max_loss for p in self.open_positions
                           if (p.spread.get('underlying') or self.cfg['underlying']) == und)
                headroom = min(headroom, max(0.0, name_cap - held))
                budget = min(name_cap / max(1, int(self.cfg.get('max_per_name') or 1)),
                             headroom)
            else:
                budget = min(budget, headroom)
        else:
            # The overlay applies here too. Gating it on `max_portfolio_risk_pct` made
            # `--vol-target` a flag that parsed, validated and did nothing whenever the
            # book cap was left unset -- the same silent-default failure as
            # `--concurrent`, reintroduced by the feature that fixed it.
            budget = (self.current_equity()
                      * self.cfg.get('max_risk_per_trade_pct', 0.02)
                      * self.risk_scale())
        qty = int(budget // per_spread_risk)
        return max(0, min(qty, self.cfg.get('max_contracts', 20)))

    def unrealized(self, date):
        """
        P&L of the open book at today's marks, in dollars.

        The equity curve was realised cash only. That is defensible at 0.4 average
        open positions and indefensible at ten: an open loss simply did not exist in
        the drawdown, and the halt could not see it either. A position whose legs have
        no print today is held at its last known mark rather than dropped to zero --
        dropping it would print a phantom profit equal to the whole credit.
        """
        total = 0.0
        for pos in self.open_positions:
            mark, _ = self._mark_to_close(pos, date, None)
            if mark is None:
                mark = pos.last_mark
            else:
                pos.last_mark = mark
            if mark is None:
                continue
            if pos.side == 'debit':
                total += (mark - pos.debit) * fills.CONTRACT_MULTIPLIER * pos.qty
            else:
                total += (pos.credit - mark) * fills.CONTRACT_MULTIPLIER * pos.qty
            total -= pos.entry_fees
        return total

    def current_equity(self):
        """
        Equity available for sizing.

        Realised cash less the capital already committed as defined risk on open
        positions. Sizing off raw cash would let the book stack up correlated
        positions that each look affordable in isolation.
        """
        committed = sum(p.max_loss for p in self.open_positions)
        return self.cash - committed

    # -------------------------------------------------------------- management

    def _close(self, pos, date, close_px, exit_fees, reason, spot):
        if pos.side == 'debit':
            pnl = fills.realized_pnl_debit(pos.debit, close_px, pos.qty, pos.entry_fees, exit_fees)
        else:
            pnl = fills.realized_pnl(pos.credit, close_px, pos.qty, pos.entry_fees, exit_fees)
        self.cash += pnl
        row = pos.to_row()
        row.update({
            'exit_date': date, 'exit_debit': close_px, 'exit_reason': reason,
            'exit_spot': spot, 'entry_fees': pos.entry_fees, 'exit_fees': exit_fees,
            'pnl': pnl,
            'held_days': (date - pos.entry_date).days,
            'return_on_risk': pnl / pos.max_loss if pos.max_loss else 0.0,
        })
        self.trades.append(row)
        return pnl

    def _mark_to_close(self, pos, date, spot):
        """(close_px, exit_fees) or (None, None) if a leg has no print."""
        s = pos.spread
        st = s.get('structure')
        if st in NAKED:
            put_m = self.store.option_close(s['put_short_occ'], date)
            call_m = self.store.option_close(s['call_short_occ'], date)
            if put_m is None or call_m is None:
                return None, None
            return fills.two_short_exit(put_m, call_m, pos.qty, self.friction)
        short_mark = self.store.option_close(s['short_occ'], date)
        long_mark = self.store.option_close(s['long_occ'], date)
        if short_mark is None or long_mark is None:
            return None, None
        if st == 'condor':
            cs = self.store.option_close(s['call_short_occ'], date)
            cl = self.store.option_close(s['call_long_occ'], date)
            if cs is None or cl is None:
                return None, None
            return fills.condor_exit(short_mark, long_mark, cs, cl, pos.qty, self.friction)
        if pos.side == 'debit':
            return fills.spread_debit_exit(long_mark, short_mark, pos.qty, self.friction)
        return fills.spread_exit(short_mark, long_mark, pos.qty, self.friction)

    def _settle(self, pos, date, spot):
        s = pos.spread
        st = s.get('structure')
        if st in NAKED:
            if spot is None:
                return s.get('width', 0.0)
            return fills.settle_strangle_at_expiry(
                spot, s['put_short_strike'], s['call_short_strike'])
        if st == 'calendar':
            right = (s.get('right') or 'C').upper()
            k = s['short_strike']
            if spot is None:
                short_px = 0.0
            elif right.startswith('C'):
                short_px = max(0.0, spot - k)
            else:
                short_px = max(0.0, k - spot)
            long_px = self.store.option_close(s['long_occ'], date) or 0.0
            if pos.side == 'debit':
                return long_px - short_px
            return short_px - long_px
        if pos.side == 'debit':
            if spot is None:
                return 0.0
            return fills.settle_debit_at_expiry(
                spot, s['long_strike'], s['short_strike'], s['right'])
        if spot is None:
            return s['width']
        if st == 'condor':
            return fills.settle_condor_at_expiry(
                spot, s['short_strike'], s['long_strike'],
                s['call_short_strike'], s['call_long_strike'])
        return fills.settle_at_expiry(spot, s['short_strike'], s['long_strike'], s['right'])

    def manage(self, date):
        """Take-profit, stop-loss, and expiry settlement for every open position."""
        still_open = []
        for pos in self.open_positions:
            s = pos.spread
            und = s.get('underlying') or self.cfg['underlying']
            spot = self.store.underlying_close(und, date)

            if date >= pos.expiry:
                if spot is None:
                    self.warnings.append(
                        f"{date}: no underlying close at expiry for {s['short_occ']}; "
                        f"settled conservatively")
                intrinsic = self._settle(pos, date, spot)
                exit_fees = fills.leg_fees(0.0, pos.qty, 'buy') if intrinsic and intrinsic > 0 else 0.0
                self._close(pos, date, intrinsic, exit_fees, 'expiry', spot)
                continue

            mark, exit_fees = self._mark_to_close(pos, date, spot)
            if mark is None:
                still_open.append(pos)
                continue

            if pos.side == 'debit':
                tp = pos.debit * (1.0 + self.exits['take_profit_pct'])
                sl = pos.debit * max(0.0, 1.0 - self.exits['take_profit_pct'])
                if mark >= tp:
                    self._close(pos, date, mark, exit_fees, 'take_profit', spot)
                elif mark <= sl:
                    self._close(pos, date, mark, exit_fees, 'stop_loss', spot)
                elif self.exits['close_at_dte'] and (pos.expiry - date).days <= self.exits['close_at_dte']:
                    self._close(pos, date, mark, exit_fees, 'dte_exit', spot)
                else:
                    still_open.append(pos)
            elif mark <= pos.credit * (1.0 - self.exits['take_profit_pct']):
                self._close(pos, date, mark, exit_fees, 'take_profit', spot)
            elif mark >= pos.credit * self.exits['stop_loss_mult']:
                self._close(pos, date, mark, exit_fees, 'stop_loss', spot)
            elif self.exits['close_at_dte'] and (pos.expiry - date).days <= self.exits['close_at_dte']:
                self._close(pos, date, mark, exit_fees, 'dte_exit', spot)
            else:
                still_open.append(pos)

        self.open_positions = still_open

    # -------------------------------------------------------------------- entry

    def _sessions_to_expiry(self, today, expiry):
        """
        Trading sessions of volatility left, from today's close to expiry.

        The unit the option is actually exposed to. Friday-to-Monday is ONE session, so
        it prices like Tuesday-to-Wednesday rather than three times longer -- which is
        what made a Friday chain invert ~30% cheaper than an identical Tuesday chain
        and turned "IV cheap versus the forecast" into a weekday detector. Measured on
        this store: median 15-delta put IV 17.6-19.7% entered Mon-Thu, 12.4% entered
        Friday. 12 of 12 debit entries in the 2026-08-29 adaptive run were Thu/Fri.
        """
        key = (today, expiry)
        hit = self._sess_cache.get(key)
        if hit is not None:
            return hit if hit >= 0 else None
        try:
            sessions = self.store.trading_days(today, expiry)
        except Exception:
            self._sess_cache[key] = -1
            return None
        if not sessions:
            self._sess_cache[key] = -1
            return None
        out = max(len(sessions) - 1, 0)
        self._sess_cache[key] = out
        return out

    def maybe_open(self, date):
        cap = self.cfg.get('max_concurrent', 3)
        if len(self.open_positions) >= cap:
            return
        names = list(self.cfg.get('underlyings') or [self.cfg['underlying']])
        # One position per (underlying, kind) was the rule. It also silently forbade a
        # second TRANCHE: at DTE 4-7 with a 2-4 session median hold the same name can
        # carry three overlapping expiries, and instead the book sat flat on ~62% of
        # sessions using 0.4 of its 3 slots. The dedupe key now carries the expiry, so
        # what is blocked is re-entering the SAME structure in the SAME expiry, and
        # `max_per_name` -- a stated number rather than an accident of the key --
        # decides how many tranches one name may hold. Default 1 = old behaviour.
        already = {(p.spread.get('underlying') or self.cfg['underlying'], p.spread['kind'],
                    p.spread['expiry']) for p in self.open_positions}
        per_name = {}
        for p in self.open_positions:
            u = p.spread.get('underlying') or self.cfg['underlying']
            per_name[u] = per_name.get(u, 0) + 1
        max_per_name = max(1, int(self.cfg.get('max_per_name') or 1))

        flat_by = self.cfg.get('flat_by')
        adaptive = self.cfg.get('structure') in ('adaptive', 'best', 'picker', 'portrait')
        horizon = max(int(self.cfg.get('max_dte', 7)), int(self.cfg.get('max_dte_s3') or 12))
        events = self.cfg.get('event_blackout') or []
        # A day-level signal for the canon's event_mode. It no longer vetoes the day:
        # the veto is per candidate, against the expiry that candidate actually holds
        # to. The old `if macro and not adaptive: return` also gave adaptive runs no
        # blackout at all, so the two paths were measuring different rules.
        macro = event_inside_horizon(events, date, horizon)

        book = []
        for und in names:
            spot = self.store.underlying_close(und, date)
            if spot is None:
                continue
            min_dte = self.cfg.get('min_dte', 1)
            max_dte = self.cfg.get('max_dte', 7)
            hi = max(int(max_dte), int(self.cfg.get('max_dte_s3') or 12)) if adaptive else int(max_dte)
            if hasattr(self.store, 'chains_in_window'):
                chain = self.store.chains_in_window(und, date, min_dte, hi)
            else:
                expiries = self.store.expiries_after(und, date)
                expiry = strategy.pick_expiry(expiries, date, min_dte, max_dte)
                chain = self.store.chain_for(und, expiry, date) if expiry else []
            if not chain:
                continue
            if flat_by:
                chain = [r for r in chain if r.get('expiry') and r['expiry'] <= flat_by]
                if not chain:
                    continue
            rv = self.store.realized_vol(und, date, self.cfg.get('rv_window', 21))
            basis = self.cfg.get('time_basis', 'sessions')
            gkey = (und, date, min_dte, hi, basis, round(float(spot), 6))
            gcache = getattr(self.store, '_greeks_cache', None)
            enriched_chain = gcache.get(gkey) if gcache is not None else None
            if enriched_chain is None:
                enriched_chain = strategy.enrich_with_greeks(
                    chain, spot, date,
                    sessions_to_expiry=(self._sessions_to_expiry if basis == 'sessions' else None))
                if gcache is not None:
                    gcache[gkey] = enriched_chain
            cfg_u = dict(self.cfg, underlying=und, asof=date, spot=spot,
                         _enriched=[dict(r) for r in enriched_chain],
                         prior_closes=self.store.closes_before(und, date, 21),
                         sessions_to_expiry=(self._sessions_to_expiry
                                             if self.cfg.get('time_basis', 'sessions') == 'sessions'
                                             else None),
                         earnings_in_window=macro)
            if hasattr(self.store, 'ohlc_before'):
                ohlc = self.store.ohlc_before(und, date, 400)
                if ohlc and len(ohlc[0]) >= 90:
                    horizon = cfg_u.get('har_horizon_days') or strategy.CANON.HAR_HORIZON
                    fcache = getattr(self.store, '_feature_cache', None)
                    fkey = (und, date, horizon)
                    hit = fcache.get(fkey) if fcache is not None else None
                    if hit is None:
                        rv_f = strategy.CANON.har_forecast_rv(
                            ohlc[0], ohlc[1], ohlc[2], ohlc[3], horizon)
                        mets = strategy.CANON.ohlcv_metrics(
                            ohlc[0], ohlc[1], ohlc[2], ohlc[3], ohlc[4] if len(ohlc) > 4 else None)
                        if fcache is not None:
                            fcache[fkey] = (rv_f, mets)
                    else:
                        rv_f, mets = hit
                    mets = dict(mets) if mets else mets
                    if rv_f is not None:
                        cfg_u['rv_forecast'] = rv_f
                    if mets:
                        cfg_u['ohlcv_metrics'] = mets
                        cfg_u['event_mode'] = bool(
                            (mets.get('earnings_sig') or 0) >= strategy.CANON.EARNINGS_SIG)
            self._chains_seen += 1
            for spread in strategy.generate_candidates(chain, spot, date, cfg_u, rv,
                                                       stats=self._sel_stats):
                self._candidates_seen += 1
                if event_in_life(events, date, spread['expiry']):
                    self._blocked_by_event += 1
                    continue
                spread['underlying'] = und
                spread['entry_spot'] = spot
                book.append(spread)

        book.sort(key=lambda s: s.get('credit_ratio') or 0, reverse=True)
        for spread in book:
            if len(self.open_positions) >= cap:
                break
            key = (spread['underlying'], spread['kind'], spread['expiry'])
            if key in already:
                continue
            if per_name.get(spread['underlying'], 0) >= max_per_name:
                continue
            spot = spread.get('entry_spot')
            side = spread.get('side') or 'credit'
            st = spread.get('structure')
            if st in NAKED:
                credit, entry_fees = fills.two_short_entry(
                    spread['put_short_close'], spread['call_short_close'], 1, self.friction)
                if credit is None or credit <= 0:
                    continue
                qty = self.size_position(spread, credit)
                if qty <= 0:
                    continue
                _, entry_fees = fills.two_short_entry(
                    spread['put_short_close'], spread['call_short_close'], qty, self.friction)
                self.open_positions.append(
                    Position(spread, qty, credit, entry_fees, date, spot))
                already.add(key)
                per_name[spread['underlying']] = per_name.get(spread['underlying'], 0) + 1
                continue
            if side == 'debit':
                prem, entry_fees = fills.spread_debit_entry(
                    spread['long_close'], spread['short_close'], 1, self.friction)
                if prem is None or prem <= 0:
                    continue
                qty = self.size_position(spread, prem)
                if qty <= 0:
                    continue
                _, entry_fees = fills.spread_debit_entry(
                    spread['long_close'], spread['short_close'], qty, self.friction)
                self.open_positions.append(
                    Position(spread, qty, prem, entry_fees, date, spot, side='debit'))
                already.add(key)
                per_name[spread['underlying']] = per_name.get(spread['underlying'], 0) + 1
                continue
            if spread.get('structure') == 'condor':
                credit, entry_fees = fills.condor_entry(
                    spread['short_close'], spread['long_close'],
                    spread['call_short_close'], spread['call_long_close'], 1, self.friction)
            else:
                credit, entry_fees = fills.spread_entry(
                    spread['short_close'], spread['long_close'], 1, self.friction)
            if credit is None or credit <= 0:
                continue
            qty = self.size_position(spread, credit)
            if qty <= 0:
                continue
            if spread.get('structure') == 'condor':
                _, entry_fees = fills.condor_entry(
                    spread['short_close'], spread['long_close'],
                    spread['call_short_close'], spread['call_long_close'], qty, self.friction)
            else:
                _, entry_fees = fills.spread_entry(
                    spread['short_close'], spread['long_close'], qty, self.friction)
            self.open_positions.append(
                Position(spread, qty, credit, entry_fees, date, spot))
            already.add(key)
            per_name[spread['underlying']] = per_name.get(spread['underlying'], 0) + 1

    # --------------------------------------------------------------------- run

    def run(self, start, end):
        days = self.store.trading_days(start, end)
        if not days:
            raise ValueError(f"no trading days between {start} and {end}")

        self._chains_seen = 0
        self._candidates_seen = 0
        self._blocked_by_event = 0
        self._sel_stats = {}
        peak = self.cash
        for date in days:
            self.manage(date)

            equity_mtm = self.cash + self.unrealized(date)
            prev = self.equity_curve[-1].get('equity_mtm') if self.equity_curve else None
            if prev:
                self._ret_window.append((equity_mtm - prev) / prev)
                if len(self._ret_window) > int(self.cfg.get('vol_window', 21)):
                    self._ret_window.pop(0)
            self._equity_now = equity_mtm
            gauge = equity_mtm if self.equity_basis == 'mtm' else self.cash

            if not self.halted:
                dd = (gauge - peak) / peak if peak else 0.0
                if dd <= -abs(self.cfg.get('max_drawdown_halt_pct', 0.10)):
                    self.halted = True
                    self.warnings.append(
                        f"{date}: drawdown {dd:.1%} breached halt threshold; "
                        f"stopped opening new positions")
                else:
                    self.maybe_open(date)
                    # Re-mark: today's entries are part of today's book.
                    equity_mtm = self.cash + self.unrealized(date)
                    self._equity_now = equity_mtm

            peak = max(peak, gauge)
            self.equity_curve.append({'date': date, 'equity': self.cash,
                                      'equity_mtm': equity_mtm,
                                      'risk_scale': self.risk_scale(),
                                      'risk_committed': self.committed_risk(),
                                      'open_positions': len(self.open_positions)})

        # Anything still open at the end is marked at its defined max loss, which is
        # the conservative reading -- better than assuming a favourable close.
        for pos in list(self.open_positions):
            last = days[-1]
            und = pos.spread.get('underlying') or self.cfg['underlying']
            spot = self.store.underlying_close(und, last)
            close_px = pos.spread.get('width') or 0.0
            if pos.qty and (pos.spread.get('structure') in NAKED or pos.spread.get('max_loss_unit')):
                close_px = pos.max_loss / (pos.qty * fills.CONTRACT_MULTIPLIER)
            self._close(pos, last, close_px, 0.0, 'forced_close_eod', spot)
            self.warnings.append(
                f"{last}: {pos.spread['short_occ']} still open at end of run; "
                f"marked at max loss")
        self.open_positions = []

        # A run that never saw a chain, or never built a candidate, found no DATA -- not
        # no edge. Reporting 0.00% with exit code 0 is the most dangerous failure a
        # research harness can have: it is indistinguishable from a real negative
        # result. It bit during this very change, when a bug dropped every contract and
        # the harness printed "return 0.00%, trades 0" without complaint.
        enriched = self._sel_stats.get('enriched', 0)
        if self._chains_seen and enriched == 0:
            raise RuntimeError(
                f"{self._chains_seen} chains examined over {len(days)} sessions and the "
                f"greeks inversion recovered NOTHING. That is a data or units failure, "
                f"not a strategy result -- check the time basis, the strike ladder's "
                f"reach and the option closes before reading this as 'no edge'.")
        if self._blocked_by_event:
            self.warnings.append(
                f"macro blackout removed {self._blocked_by_event} of "
                f"{self._candidates_seen} candidates ("
                f"{self._blocked_by_event / max(self._candidates_seen, 1):.0%}), judged "
                f"per candidate against its own expiry rather than the DTE ceiling")
        if self._candidates_seen == 0 and self._chains_seen:
            # Legitimate for a deliberately strict config, so a warning rather than a
            # stop -- but it must never be invisible in the run folder.
            self.warnings.append(
                f"{self._chains_seen} chains and {enriched} priced contracts produced "
                f"ZERO candidate structures: every one was rejected before sizing. "
                f"A 0.00% return here means the gates, not the market.")

        return {
            'equity_curve': self.equity_curve,
            'trades': self.trades,
            'warnings': self.warnings,
            'halted': self.halted,
        }
