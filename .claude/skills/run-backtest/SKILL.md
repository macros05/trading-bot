---
name: run-backtest
description: Run a backtest with consistent output naming and a delta vs the previous run. Use when the user says /run-backtest or asks to run/compare backtests. Picks the right runner (cli, advanced, multi_symbol, walk_forward) and writes results to backtest/results/{runner}_v{N+1}.json.
---

# Run Backtest

User command: `/run-backtest`

## Available Runners
1. `backtest/cli.py` — basic single-symbol backtest
2. `backtest/advanced.py` — with fees + slippage
3. `backtest/multi_symbol.py` — BTC / ETH / SOL comparison
4. `backtest/walk_forward.py` — out-of-sample validation

## Usage

Always:

1. Ask which runner if not specified.
2. Name output: `backtest/results/{runner}_v{N+1}.json` (increment from last existing version in that directory).
3. Run: `venv/bin/python -m backtest.cli --symbol BTC/USDT --months 6` (adapt the module path and flags to the chosen runner).
4. After completion, print delta vs previous run:
   - Net PnL change
   - Sharpe change
   - Win rate change
   - MaxDD change
5. Save result JSON with consistent naming.
