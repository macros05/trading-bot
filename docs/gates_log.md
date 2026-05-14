# Gates log — spec §11/§12 validation

One entry per attempt at a gate. Always include the run that produced the
numbers (commit + script + JSON output) so the result is reproducible.

---

## 2026-05-14 — V7 long-only baseline (Session 0)

**Run:** commit `8ad30c3` on `feature/lowvol-tuning-telegram-misses`
**Script:** `backtest/sweep_v7.py`
**Data:** BTC/USDT 1m, 2024-05-05 → 2026-04-25, 1,036,808 candles (cache `_cache_btc_1m_24mo.pkl`)
**Harness:** walk-forward 3 mo train / 1 mo test / 1 mo step, fees 0.1 %/side, slippage 5 bps/side, balance $10 000, risk_pct 0.01.
**Variants:** 20 (all long-only, AdvancedParams space).
**Output:** `backtest/results/sweep_v7_24mo.json`

### Result — best variant (`sl_015_tp_030`)

| Metric | Value |
|---|---:|
| Trades (OOS) | 16 |
| Win rate | 62.5 % |
| Win rate Wilson 95 % lower | 38.6 % |
| Fee-adjusted break-even WR | 40.0 % |
| Per-trade Sharpe | 0.4434 |
| Annualized Sharpe (×√trades_per_year, 8/year) | 1.254 |
| Max drawdown | 2.48 % |
| Net PnL (24 mo) | +11.18 % |
| Fees paid | $214 |
| Profit factor | 2.47 |
| Folds with ≥ 1 trade | 13 / 21 |

### Status vs §12 gates

| # | Gate | Threshold | Observed | Pass |
|---|---|---|---|---|
| 1 | Walk-forward OOS annualized Sharpe | ≥ 1.5 | 1.254 | ❌ |
| 2 | OOS trades | ≥ 200 | 16 | ❌ |
| 3 | WR 95 % CI lower bound > fee-adjusted breakeven | > 40.0 % | 38.6 % | ❌ (borderline) |
| 4 | Max drawdown | ≤ 15 % | 2.48 % | ✅ |
| 5 | Profitable on ≥ 5 / 8 symbols | per spec §3.3 | only BTC tested | ❌ |
| 6 | Paper forward-test 90 d within ±30 % of backtest | n/a | not started | ❌ |
| 7 | DSR ≥ 0.95 | spec §6.4 | not computed | ❌ |

**1 / 7 passed.** Not eligible for capital deployment.

### Honest interpretation

- Edge is **suggestive but unproven**. p-value from one-sided z-test against
  null `p ≤ 0.40` is ≈ 0.04 on n = 16, p̂ = 0.625 — barely rejects at α = 0.05.
- 16 trades / 24 months = 8 trades / year. Reaching gate 2 (200 trades)
  naturally takes ~25 years at this rate. Multi-symbol and short-side are
  the only paths to faster sample accumulation.
- The variant accepted into live (`base_champion` ≡ live config after the
  RSI_LONG=40 revert) is the **3rd-best** by SR_ann; `sl_015_tp_030`'s
  marginally better PnL/Sharpe rests on a WR-CI that still sits below
  breakeven, so we don't ship it.

### What this run does NOT validate

- Short positions (the live bot trades both sides; `AdvancedParams` is long-only)
- MTF, volatility, session, stalled, range filters (live has them; harness doesn't)
- Pct-based trailing stop (harness uses ATR-based)
- ETH/SOL/BNB/AVAX/LINK/MATIC/ATOM (sweep ran on BTC only)
- DSR / bootstrap CI

These are scheduled for the next sweep iteration.
