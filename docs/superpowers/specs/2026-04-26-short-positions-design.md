# Short Positions Support — Design Spec

**Date:** 2026-04-26
**Author:** Claude (brainstorming session) + Marcos (decisions)
**Status:** Approved, pending implementation plan
**Branch:** `feature/short-positions`

---

## Problem Statement

The bot currently trades long-only on Binance Spot Testnet. The validated 90-day backtest produces only 10 trades — too few to generate meaningful PnL signal. The user wants higher trade volume by enabling short entries while preserving the existing long strategy.

## Goal

Add short-direction trading capability under a single-position FSM (one position at a time, can be either long or short). Increase trade volume without doubling concurrent exposure.

## Non-Goals

- Concurrent long+short positions (delta-neutral book) — explicitly deferred. Mode 2 of the brainstorming options is out of scope.
- ATR-based dynamic SL/TP for shorts in this iteration. Fixed-percent SL/TP only.
- Multi-symbol short trading (ETH, SOL). Already discarded for longs; no reason to revisit for shorts here.
- MacroFilter side-aware mode (positive funding rate inverts bullishness for shorts). Acknowledged asymmetry; left for future iteration.

## Pre-Implementation Gate

Before any production code changes, run a backtest validation script and gate deployment on combined-Sharpe ≥ long-only Sharpe.

- **Script:** `backtest/short_validation.py` (new, ~80 lines, modeled on `backtest/engine.py`)
- **Dataset:** Same 90-day BTC/USDT 1m window used for the original sweeps (2026-01-14 → 2026-04-14, 129 602 candles)
- **Configs tested:**
  1. Long-only baseline (RSI<40 AND close>SMA20, SL 2.5 %, TP 4 %)
  2. Short-only mirror (RSI>60 AND close<SMA20, SL 2.5 %, TP 4 %)
  3. Long+Short combined (Mode 1: at most one position at a time; if only one signal fires it opens that side; if both fire — mathematically impossible since RSI<40 and RSI>60 are disjoint — no entry and a warning is logged)
- **Output:** `backtest/results/short_validation.json` — per-config: trades, win rate, PnL USDT, Sharpe (per-trade, non-annualised), max DD
- **Gate:**
  - Sharpe(combined) ≥ Sharpe(long-only) = +0.187 → proceed with implementation
  - Sharpe(combined) < Sharpe(long-only) → halt, run a parameter sweep (RSI threshold ∈ {55, 65, 70} × SL/TP grid) before re-evaluating

## Architecture Changes

### 1. Exchange transport (`exchange/client.py`)

Migrate from Binance Spot Testnet to Binance USDT-M Futures Testnet.

| Aspect | Before | After |
|--------|--------|-------|
| ccxt class | `ccxt.binance` | `ccxt.binanceusdm` |
| ccxt.pro class | `ccxtpro.binance` | `ccxtpro.binanceusdm` |
| `defaultType` option | `'spot'` | `'future'` |
| Endpoint | `testnet.binance.vision` | `testnet.binancefuture.com` (via `set_sandbox_mode(True)`) |
| API credentials | `BINANCE_API_KEY`, `BINANCE_API_SECRET` | `BINANCE_FUTURES_API_KEY`, `BINANCE_FUTURES_API_SECRET` |
| Leverage | n/a (spot) | `1×` set explicitly via `exchange.set_leverage(1, symbol)` at init |
| Margin mode | n/a | `isolated` per symbol |

The `watch_candles` and `fetch_candles` public methods do not change — OHLCV semantics are identical between spot and futures.

`place_order_safe` (when implemented) needs to handle:
- `side='buy'` to open long, `side='sell'` to close long (default behavior)
- `side='sell'` to open short, `side='buy'` to close short
- `reduceOnly=true` flag on closing orders (futures-specific safety: prevents accidentally flipping side via an oversized close)

### 2. Signal layer (`strategy/signals.py`)

Add mirror functions, same signatures as long counterparts, semantics inverted:

```python
def should_enter_short(
    close: float,
    sma20: float,
    rsi14: float,
    rsi_threshold: float = 60.0,
    volume: float | None = None,
    volume_sma20: float | None = None,
    volume_factor: float = 1.2,
) -> bool:
    if rsi14 <= rsi_threshold or close >= sma20:
        return False
    if volume is not None and volume_sma20 is not None:
        if volume <= volume_sma20 * volume_factor:
            return False
    return True


def calc_pnl_short(
    close: float,
    entry_price: float,
    qty: float,
) -> tuple[float, float]:
    pnl_usdt = (entry_price - close) * qty
    pnl_pct = (entry_price - close) / entry_price * 100
    return pnl_usdt, pnl_pct


def check_exit_short(
    close: float,
    entry_price: float,
    stop_loss_pct: float = 0.025,
    take_profit_pct: float = 0.04,
) -> str | None:
    change = (entry_price - close) / entry_price
    if change <= -stop_loss_pct:
        return 'stop_loss'
    if change >= take_profit_pct:
        return 'take_profit'
    return None


def update_trailing_stop_short(
    sl_price: float,
    entry_price: float,
    tp_price: float,
    close: float,
    atr_val: float,
) -> float:
    """Mirror of update_trailing_stop. SL only ever moves DOWN for shorts."""
    if tp_price >= entry_price:  # invalid setup
        return sl_price
    progress = (entry_price - close) / (entry_price - tp_price)
    new_sl = sl_price
    if progress >= 0.75:
        new_sl = min(new_sl, close + atr_val)
    elif progress >= 0.50:
        new_sl = min(new_sl, entry_price)
    return new_sl
```

`check_exit_price` is generalized to accept a `side: 'long' | 'short'` param:
- Long: trip SL when `close <= sl_price`, trip TP when `close >= tp_price`
- Short: trip SL when `close >= sl_price`, trip TP when `close <= tp_price`

### 3. State layer (`core/state.py`)

`Position` payload gains a `side` field:

```python
{
    'side': 'long' | 'short',
    'entry_price': float,
    'qty': float,
    'ts': int,
}
```

**Backwards compatibility for `bot_state.json`:** when `_load_state` reads a position payload that lacks `side`, default to `'long'`. This handles the currently-open Apr 24 long position transparently.

The FSM itself does not change — `WAITING_SIGNAL ↔ IN_POSITION`. Direction lives inside the `Position` payload.

### 4. Loop layer (`core/loop.py`)

`_on_candles` decision tree changes when `state == WAITING_SIGNAL`:

```
long_signal  = should_enter(close, sma20, rsi14, rsi_threshold=40)
short_signal = should_enter_short(close, sma20, rsi14, rsi_threshold=60)

if long_signal and short_signal:
    log warning — contradictory signals (RSI<40 AND RSI>60 is mathematically impossible)
    do nothing
elif long_signal:
    open long position
elif short_signal:
    open short position
else:
    no entry
```

When `state == IN_POSITION`, branch on `position.side`:
- `'long'`  → `check_exit_price(..., side='long')`,  `calc_pnl(...)` on close
- `'short'` → `check_exit_price(..., side='short')`, `calc_pnl_short(...)` on close

`MacroFilter` side-asymmetry decision: in this iteration, `NO_TRADE` mode applies to longs only. Shorts ignore the macro filter. Rationale: positive funding rate (longs paying shorts) is structurally bullish for shorts and bearish for longs; the current filter logic was tuned for long-only. Refining the macro→side mapping requires its own backtest sweep and is deferred.

### 5. Risk layer (`risk/manager.py`)

**No structural changes:**
- Position sizing: `balance × 1 %` regardless of side
- Circuit breaker: −3 % daily PnL, computed by summing `pnl_usdt` from `trades_history.json` filtered by today's date

`trades_history.json` already aggregates trades chronologically without distinguishing direction. The breaker logic works as-is, provided each closed trade's `pnl_usdt` is computed via the correct calc (`calc_pnl` for long, `calc_pnl_short` for short).

### 6. Backtest layer

`backtest/engine.py` currently assumes long-only. To support the validation script:

- Extract a per-tick simulation helper that takes `(side, entry_price, close, sl_pct, tp_pct)` and returns `(exit_reason, pnl_usdt)`
- `backtest/short_validation.py` reuses this helper with `side='short'` for config 2 and dispatches per side for config 3 (combined)

Result file naming follows the project convention (`/run-backtest` slash command): `backtest/results/short_validation_v{N+1}.json` for subsequent runs.

### 7. Migration of currently-open position

The bot is `IN_POSITION` long since 2026-04-24 06:32 UTC on **spot testnet**. After migrating `exchange/client.py` to futures testnet, this spot position is orphaned (the new client cannot see it).

**Decision:** close it manually via the spot testnet web UI before deploying the new container. Then:

1. `docker compose stop trading_bot`
2. Manually close BTC/USDT spot position at https://testnet.binance.vision
3. Reset `data/bot_state.json` to `{state: WAITING_SIGNAL, position: null, daily_pnl: 0.0, daily_date: ""}`. The `PreToolUse` hook in `.claude/settings.json` blocks Claude from editing this file, so the user does this step manually (the new bot also reconciles against the empty futures account on startup, so this is belt-and-suspenders).
4. `docker compose build trading_bot && docker compose up -d trading_bot`

The backwards-compat `side='long'` default in `_load_state` is still required for safety (in case the manual edit is forgotten).

### 8. Tests

Target: maintain 0 failures, grow from ~227 → ~250 tests.

- `tests/test_signals.py`:
  - `should_enter_short` — boundary cases at RSI 60, SMA crossing
  - `check_exit_short` — SL trip at +2.5 %, TP trip at −4 %
  - `calc_pnl_short` — positive PnL when close < entry, negative when close > entry
  - `update_trailing_stop_short` — SL only moves down; never up
  - Symmetry tests: long inputs negated produce short outputs with PnL signs flipped
- `tests/test_state.py`:
  - Load `bot_state.json` without `side` field → position assumes `'long'`
  - Load with `side='short'` → restored intact
  - Save/reload roundtrip preserves `side`
- `tests/test_loop.py`:
  - Tick where only long signal fires → opens long
  - Tick where only short signal fires → opens short
  - Tick where both fire (synthetic, since impossible in practice) → no entry, warning logged
  - Tick that closes a short via TP → `calc_pnl_short` called, trades_history updated with negative `close - entry`
- `tests/test_risk_manager.py`:
  - Circuit breaker fires after mixed long+short closes summing to −3 %
- `tests/test_exchange_client.py`:
  - Mock `ccxt.binanceusdm` — verify `set_leverage(1, 'BTC/USDT')` called at init
  - Verify `set_sandbox_mode(True)` still called
  - Verify `defaultType='future'` in options

### 9. Deployment sequence

1. Branch `feature/short-positions` from `main`
2. Run backtest validation (`python -m backtest.short_validation`)
3. Verify Sharpe gate
4. Implement signals → state → loop → exchange (in that order, each behind tests)
5. Full test suite passes
6. Run `safety-invariants-audit` skill (mandatory per CLAUDE.md for changes touching `core/`, `risk/`, `exchange/`, `strategy/`)
7. Generate new futures testnet API keys at `testnet.binancefuture.com`, add to `.env`
8. `docker compose stop trading_bot`
9. Close spot position manually, reset `bot_state.json`
10. `docker compose build trading_bot && docker compose up -d trading_bot`
11. Observe `docker logs -f trading_bot` for 24 h — first short entry should be visible
12. Merge to `main`

## Risk Surface

- **Strategy risk:** the mirror short may have negative Sharpe even if combined Sharpe improves the portfolio. The gate of "Sharpe(combined) ≥ Sharpe(long-only)" addresses this.
- **Exchange migration risk:** moving from spot to futures touches credentials, account semantics, and order types. Mitigation: testnet only, full test coverage of the new `BinanceClient`, manual spot position closure before switch.
- **State persistence risk:** the `side` field is new; an old bot reading a new state file would crash on the unknown key (not relevant since we don't roll back, but noted).
- **Circuit breaker drift:** if `calc_pnl_short` has a sign bug, the breaker may not trip in a downside-short scenario. Tests must include a short-loss-triggers-breaker case.
- **MacroFilter asymmetry:** explicitly deferred; documented as known limitation. May produce sub-optimal short entries during NO_TRADE windows that would actually be favorable for shorts.

## Open Questions

None remaining at design time. Implementation plan to be drafted by `writing-plans` skill.
