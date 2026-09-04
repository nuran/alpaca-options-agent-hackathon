# Alpaca Options Agent -- hackathon submission.
#
# Two live books on two paper accounts, one backtest engine shared by both, and a
# market-data store you rebuild from the committed Parquet. See README.md.

PY := .venv/bin/python

.PHONY: help setup restore test backtest sweep evidence \
        preflight-c shadow-c live-c preflight-b shadow-b live-b clean

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup:  ## Create the venv and install dependencies
	python3 -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -r requirements.txt
	@echo "Next: copy the dotenv example to a local dotenv file and add your Alpaca paper keys"
	@echo "Then: brew install alpacahq/tap/cli && alpaca doctor"

restore:  ## Rebuild data/market.duckdb from the committed Parquet (offline, ~10s)
	$(PY) data/restore.py

test:  ## Every self-check, the engine tests, and the agent failure-injection tests
	@$(PY) data/occ.py
	@$(PY) backtest/attribution.py
	@$(PY) backtest/economics.py
	@$(PY) backtest/blackscholes.py
	@$(PY) backtest/metrics.py
	@$(PY) backtest/fills.py
	@$(PY) backtest/strategy.py
	@$(PY) backtest/playbook.py
	@$(PY) backtest/stats.py
	@$(PY) agent/structures.py
	@$(PY) agent/veto.py
	@$(PY) agent/effects.py
	@$(PY) agent/tape.py
	@$(PY) agent/canon.py
	@$(PY) agent/skills.py
	@$(PY) agent/usadapt_bridge.py
	@$(PY) data/asset_selector.py
	@$(PY) backtest/test_engine.py
	@echo
	@$(PY) agent/test_agent.py

# ---- backtest (run `make restore` first) ------------------------------------
#
# --drawdown-halt 0.99 disables the halt FOR THE BACKTEST ONLY. Live it is armed at
# 10% (see each rulebook's `risk` block, and the test that pins it). A halt fires once
# and stops the run, which truncates the sample and makes two runs incomparable -- the
# question a backtest answers is "does this edge survive friction", not "when would it
# have stopped". README section 5 reports exactly these invocations.

backtest:  ## Account C's book at the default 3% friction
	$(PY) backtest/run.py --shortlist data/shortlist.csv --buckets CORE \
	  --structure vertical --side put --min-dte 4 --max-dte 7 \
	  --delta 0.15 --delta-tolerance 0.08 --width 5 --iv-rv 1.2 \
	  --risk-pct 0.02 --concurrent 3 --max-per-name 1 --vol-target 0.25 \
	  --take-profit 0.5 --stop-loss 2.0 --drawdown-halt 0.99 --label account-c

sweep:  ## Both books across the 1% / 3% / 6% friction sweep -- the honest read
	$(PY) backtest/run.py --shortlist data/shortlist.csv --buckets CORE \
	  --structure vertical --side put --min-dte 4 --max-dte 7 \
	  --delta 0.15 --delta-tolerance 0.08 --width 5 --iv-rv 1.2 \
	  --risk-pct 0.02 --concurrent 3 --max-per-name 1 --vol-target 0.25 \
	  --take-profit 0.5 --stop-loss 2.0 --drawdown-halt 0.99 --sweep --label account-c
	$(PY) backtest/run.py --shortlist data/shortlist.csv --buckets CORE \
	  --structure vertical --side put --min-dte 1 --max-dte 7 \
	  --delta 0.15 --delta-tolerance 0.08 --width 5 --iv-rv 1.2 \
	  --risk-pct 0.02 --concurrent 3 --max-per-name 1 --vol-target 0.25 \
	  --take-profit 0.5 --stop-loss 2.0 --drawdown-halt 0.99 --sweep --label account-b

evidence:  ## Deflated Sharpe, PBO (CSCV) and rolling windows over the runs on disk
	$(PY) backtest/evidence.py --champion $(shell ls -t runs | head -1)

# ---- live agent (paper only; no code path here constructs --live) ------------

preflight-c:  ## Verify every live dependency for account C before trading
	$(PY) agent/preflight.py --profile putcr-core6-d47

shadow-c:  ## One cycle for account C with every order dry-run
	$(PY) agent/agent_loop.py --profile putcr-core6-d47 --shadow --once

live-c:  ## One cycle for account C against the paper broker
	$(PY) agent/agent_loop.py --profile putcr-core6-d47 --once

preflight-b:  ## Verify every live dependency for account B before trading
	$(PY) agent/preflight.py --profile putcr-core6

shadow-b:  ## One cycle for account B with every order dry-run
	$(PY) agent/agent_loop.py --profile putcr-core6 --shadow --once

live-b:  ## One cycle for account B against the paper broker
	$(PY) agent/agent_loop.py --profile putcr-core6 --once

clean:  ## Remove the rebuildable store (keeps runs/ and the decision journals)
	rm -f data/market.duckdb data/market.duckdb.wal
