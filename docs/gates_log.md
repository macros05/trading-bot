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

---

## 2026-05-15 — V7 full simulator multi-symbol sweep (Session 1)

**Run:** commit `ff85eaf` on `feature/lowvol-tuning-telegram-misses`
**Script:** `backtest/sweep_v7_full.py` × 3 symbols
**Data:** BTC/USDT, ETH/USDT, SOL/USDT 1m, 2024-05-25 → 2026-05-15, ~1.04 M candles each
**Harness:** walk-forward 3 mo train / 1 mo test / 1 mo step, fees + slippage, balance $10 000, risk_pct 0.02, leverage 2.
**Simulator:** `backtest/v7_full.py` (long+short + MTF + volatility + session + range + short-trend + pct-trailing — matches live).
**Variants:** 15 filter-ablation configs.
**Output:** `backtest/results/sweep_v7_full_24mo_{btc,eth,sol}.json`

### Result — best variant by cross-symbol Sharpe (`vol_off`)

|  | BTC | ETH | SOL | Combined |
|---|---:|---:|---:|---:|
| Trades | 2 | 3 | 2 | **7** |
| Win rate | 50.0 % | 100 % | 50.0 % | **57.1 %** |
| Per-trade Sharpe | +0.62 | +1.41 | −0.33 | n/a |
| Annualized Sharpe | +0.62 | +1.41 | −0.33 | **avg +0.567** |
| Max DD | 0.20 % | <1 % | <1 % | (per-symbol) |
| PnL % | +2.85 % | ≈+1.5 % | ≈+0.7 % | **+5.09 %** |
| DSR p-value | 0.00 | 0.00 | 0.00 | not significant |

### Result — current live config (`live_v7_post_session0`) for context

|  | BTC | ETH | SOL | Combined |
|---|---:|---:|---:|---:|
| Trades | 1 | 1 | 2 | **4** |
| Win rate | 100 % | 100 % | 50 % | **75 %** |
| Annualized Sharpe | 0.00 (n=1) | 0.00 (n=1) | −0.33 | avg −0.11 |
| PnL % | +3.05 % | small | small | **+2.23 %** |

The live config is statistically equivalent to `vol_off` on Sharpe given the tiny sample, but `vol_off` produced almost twice the trade count with sign-consistent positive PnL across all 3 symbols — applied as the marginal improvement.

### Status vs §12 gates

| # | Gate | Threshold | Observed (vol_off, multi-symbol) | Pass |
|---|---|---|---|---|
| 1 | Walk-forward OOS annualized Sharpe | ≥ 1.5 | avg +0.567 | ❌ |
| 2 | OOS trades | ≥ 200 | 7 | ❌ |
| 3 | WR 95 % CI lower bound > breakeven | > 40 % | 25.8 % | ❌ |
| 4 | Max DD | ≤ 15 % | <1 % | ✅ |
| 5 | Profitable on ≥ 5 / 8 symbols | per spec §3.3 | 2.5 of 3 tested | partial |
| 6 | Paper forward-test 90 d within ±30 % | n/a | not started | ❌ |
| 7 | DSR ≥ 0.95 | ≥ 0.95 | 0.00 | ❌ |

**1 / 7 clean.** Same as Session 0 — the parameter sweep didn't move the gate count because trade frequency is the binding constraint, not WR or Sharpe.

### Variants that catastrophically failed

| Variant | Σ trades | Σ PnL % | Notes |
|---|---:|---:|---|
| `all_filters_off` | 5001 | −285.17 | shorts dominate (≈99 %) and lose |
| `v6_baseline` | 3179 | −298.98 | V6 thresholds + shorts allowed → blow-up |
| `mtf_off+short_trend_off` | 3146 | −243.52 | shorts uncontrolled |
| `mtf_off+rsi_short_50` | 580 | −94.78 | even mild short relax → −95 % |

**Lesson burned in:** the V7 short-trend filter stack is the only thing standing between the strategy and the account going to zero in this market regime. Do not relax.
