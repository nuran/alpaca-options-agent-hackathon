"""US-ADAPT v2 — pre-selection funnel + adaptive defined-risk options algorithm on Alpaca.

Modules
-------
db          DuckDB connection, point-in-time tables (bars, chain_snapshots, vix, ledger)
data        Alpaca REST + Cboe fetchers (no alpaca-py dependency)
vol         Yang-Zhang / Garman-Klass / HAR forecast / MAE / gap stats
regime      VIX percentile, term structure, k-adaptation, regime state
preselect   L0-L3 funnel -> shortlist with ShortVol / LongConvex scores
structures  S1-S5 candidate builder, executable pricing, EV under forecast distribution
scan        daily GREEN/CONVEX scan -> ranked, budgeted order intents
manage      15-min adaptive management loop (TP by DTE, delta breach, RV break, ex-div, time)
"""
__version__ = "2.0.0"
