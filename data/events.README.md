# data/events.csv -- macro event blackout calendar

Used by `backtest/engine.py` (via `run.py --events`, default this file) and mirrored for the
live agent in `agent/agent_rules.json -> schedule.event_blackout`. A listed date inside
`[today, today + max_dte]` blocks new entries in both.

Columns: `event_date` (ISO), `event` (FOMC / CPI / NFP), `source`.

**Provenance matters.** FOMC decision days come from the Fed's published calendars
(2024-2026). CPI and NFP dates outside the contest window are RULE-BASED APPROXIMATIONS
(`*_APPROX`: first Friday for NFP, the 12th for CPI) -- the BLS shifts releases around
holidays. Before relying on a backtest that uses this file, replace the approximate rows
with the BLS schedule (https://www.bls.gov/schedule/) and set `source` accordingly.
`make preflight` warns while any `*_APPROX` row lies inside the next 30 days.
