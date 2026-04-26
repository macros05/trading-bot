# Short Positions Support — Design Spec (v2: Aggressive Profile)

**Date:** 2026-04-26
**Author:** Claude (brainstorming session) + Marcos (decisions)
**Status:** Approved, pending implementation plan
**Branch:** `feature/short-positions`
**Profile:** **AGGRESSIVE** — sizing, leverage, thresholds, breaker, and SL/TP all loosened from the conservative baseline. Safety invariants (circuit-breaker existence, `place_order_safe` exchange-side confirmation, error-handling preservation, no hardcoded credentials) are unchanged per `CLAUDE.md` "NUNCA violar". This profile increases volume and downside variance without removing the safety net.

## Risk Profile Comparison

| Dial | Conservative baseline | **Aggressive (this spec)** | Notes |
|---|---|---|---|
| Leverage (futures) | 1× | **2×** | Doubles effective position vs collateral; SL % expressed in price stays the same but balance impact doubles |
| Position size | 1 % of balance | **2 %** | Combined with 2× leverage: 4× the dollar exposure per trade vs the original spot bot |
| RSI long threshold | < 40 | **< 45** | Original backtest: 69 trades, Sharpe −0.21 (long-only). More entries, lower per-trade edge |
| RSI short threshold | > 60 (mirror of <40) | **> 55** (mirror of <45) | Symmetric loosening |
| Circuit breaker (daily) | −3 % | **−5 %** | More room before halt; correspondingly larger possible single-day loss |
| Stop-loss | 2.5 % | **3.5 %** | Wider — more breathing room, larger loss when triggered |
| Take-profit | 4.0 % | **6.0 %** | Higher target — preserves R:R ratio (~1:1.7), same as original 1:1.6 |
| `CooldownPeriod` post-SL | (framework added) | **0 minutes (disabled)** | Framework present, set to no-op. Can be re-enabled without code changes |
| `StoplossGuard` (max SL/day) | (framework added) | **10 SL hits/day (effectively disabled)** | Same — framework present, set to permissive |

The protections framework is **added structurally** (so future tuning is a config change, not a code change) but **set to permissive defaults** in this profile. This was an explicit user request to maximize trade volume and accept higher variance.

---

## Problem Statement

The bot currently trades long-only on Binance Spot Testnet. The validated 90-day backtest produces only 10 trades — too few to generate meaningful PnL signal. The user wants higher trade volume by enabling short entries while preserving the existing long strategy.

## Goal

Add short-direction trading capability under a single-position FSM (one position at a time, can be either long or short). Increase trade volume without doubling concurrent exposure.

## Non-Goals

- Concurrent long+short positions (delta-neutral book) — explicitly deferred. Mode 2 of the brainstorming options is out of scope.
- ATR-based dynamic SL/TP for shorts in this iteration. Fixed-percent SL/TP only (3.5 %/6 % aggressive).
- Multi-symbol short trading (ETH, SOL). Already discarded for longs; no reason to revisit for shorts here.
- MacroFilter side-aware mode (positive funding rate inverts bullishness for shorts). Acknowledged asymmetry; left for future iteration.
- Tightening protections (`CooldownPeriod`, `StoplossGuard`) — framework added, defaults set permissive per aggressive profile. Tightening is a future config tweak, not a code change.

## Pre-Implementation Gate

Before any production code changes, run a backtest validation script using the **aggressive profile** and gate deployment on absolute floor.

- **Script:** `backtest/short_validation.py` (new, ~100 lines, modeled on `backtest/engine.py`)
- **Dataset:** Same 90-day BTC/USDT 1m window used for the original sweeps (2026-01-14 → 2026-04-14, 129 602 candles)
- **Configs tested (all using aggressive profile: SL 3.5 %, TP 6 %, sizing 2 %, leverage 2× simulated):**
  1. Long-only aggressive (RSI<45 AND close>SMA20)
  2. Short-only aggressive (RSI>55 AND close<SMA20)
  3. Long+Short combined (Mode 1: at most one position at a time; if only one signal fires it opens that side; if both fire — mathematically impossible since RSI<45 and RSI>55 are disjoint — no entry and a warning is logged)
- **Output:** `backtest/results/short_validation_aggressive.json` — per-config: trades, win rate, PnL USDT, Sharpe (per-trade, non-annualised), max DD, expected vol of returns
- **Gate (recalibrated for aggressive profile — the conservative baseline of Sharpe ≥ +0.187 no longer applies because RSI<45 long-only had Sharpe −0.21 in the original sweep):**
  - **Floor:** Sharpe(combined) ≥ 0 AND PnL(combined) ≥ 0 over 90 days → proceed with implementation
  - **Below floor:** halt; deploying a known money-loser is the one line the aggressive profile does not cross. Parameter sweep (RSI threshold variants × SL/TP grid) before re-evaluating
- **Expected trade count:** the aggressive profile should produce 4–8× the trade count of the original conservative config (extrapolating from the original RSI<45 result of 69 trades vs RSI<40's 10 trades, plus mirror shorts). Confirms volume goal independent of profitability.

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
| Leverage | n/a (spot) | **`2×`** set explicitly via `exchange.set_leverage(2, symbol)` at init (aggressive profile) |
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
    rsi_threshold: float = 55.0,    # aggressive profile (mirror of long RSI<45)
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
    stop_loss_pct: float = 0.035,   # aggressive: 3.5 % (was 2.5 %)
    take_profit_pct: float = 0.06,  # aggressive: 6.0 % (was 4.0 %)
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
long_signal  = should_enter(close, sma20, rsi14, rsi_threshold=45)        # aggressive
short_signal = should_enter_short(close, sma20, rsi14, rsi_threshold=55)   # aggressive

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

**Aggressive-profile changes (numbers, not structure):**
- Position sizing: `balance × 2 %` regardless of side (was 1 %)
- Circuit breaker: **−5 % daily PnL** (was −3 %), computed by summing `pnl_usdt` from `trades_history.json` filtered by today's date

The circuit breaker remains structurally present — only its threshold is loosened. `place_order_safe`'s exchange-side confirmation, all existing error handling, and the no-`break`-on-network-error rule all stay intact per `CLAUDE.md`.

`trades_history.json` already aggregates trades chronologically without distinguishing direction. The breaker logic works as-is, provided each closed trade's `pnl_usdt` is computed via the correct calc (`calc_pnl` for long, `calc_pnl_short` for short). With 2× leverage, the `pnl_usdt` calculation must be multiplied by the leverage factor at trade-close time so the breaker accounts for actual balance impact, not nominal price move.

### 5b. Protections framework (`risk/protections.py` — new module)

A composable pre-entry gate, modeled on freqtrade's `protections` system. Lives in a new module so the `RiskManager` stays focused on sizing+breaker.

```python
class Protection(Protocol):
    def is_blocked(self, now_ms: int, trades_history: list[dict]) -> tuple[bool, str | None]:
        ...

class CooldownPeriod:
    """Block entries for `cooldown_seconds` after the most recent stop-loss exit."""
    def __init__(self, cooldown_seconds: int = 0): ...   # aggressive default: 0

class StoplossGuard:
    """Block entries when SL hits in the last `lookback_seconds` >= `max_sl`."""
    def __init__(self, max_sl: int = 10, lookback_seconds: int = 86_400): ...   # aggressive: 10/day

class ProtectionStack:
    def __init__(self, protections: list[Protection]): ...
    def is_blocked(self, ...) -> tuple[bool, str | None]:
        # short-circuits on first block, returns (True, reason) for logging
```

`_on_candles` calls `protection_stack.is_blocked(...)` immediately before `should_enter*` evaluation, after the `MacroFilter` check. A blocked tick logs the reason and returns without evaluating signals.

**Aggressive defaults: both protections instantiated but configured permissively.** Frame is in place; tightening to `cooldown=60min`, `max_sl=3` is a one-line config change in `main.py` later.

Protection blocks must NEVER bypass the circuit breaker — the breaker still wins (it kills entries unconditionally; protections add additional gates on top, never remove them).

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

Target: maintain 0 failures, grow from ~227 → ~260 tests (extra ~10 from protections module).

- `tests/test_signals.py`:
  - `should_enter_short` — boundary cases at RSI 55 (aggressive threshold), SMA crossing
  - `check_exit_short` — SL trip at +3.5 % above entry, TP trip at −6 % below entry
  - `calc_pnl_short` — positive PnL when close < entry, negative when close > entry; with 2× leverage applied, magnitude is doubled
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
  - Circuit breaker fires after mixed long+short closes summing to **−5 %** (aggressive threshold)
  - Position sizing returns `balance × 2 %` for both long and short
  - With leverage=2 set in config, `pnl_usdt` accounting is multiplied correctly so the breaker reflects balance impact, not raw price move
- `tests/test_protections.py` (new):
  - `CooldownPeriod(0)` never blocks (aggressive default behavior)
  - `CooldownPeriod(60)` blocks for 60s after a stop_loss exit, then releases
  - `StoplossGuard(max_sl=10, lookback=86400)` permits 9 SL/day, blocks at 10
  - `StoplossGuard` only counts `stop_loss` exits, not `take_profit`
  - `ProtectionStack` short-circuits on first block, returns the offending protection's reason
  - Protections layer never overrides the circuit breaker (test: breaker active + protections permissive → entries still blocked)
- `tests/test_exchange_client.py`:
  - Mock `ccxt.binanceusdm` — verify `set_leverage(1, 'BTC/USDT')` called at init
  - Verify `set_sandbox_mode(True)` still called
  - Verify `defaultType='future'` in options

### 9. Deployment sequence

1. Branch `feature/short-positions` from `main`
2. Update `config.py` `BOT_CONFIG` to aggressive values:
   ```python
   'risk_pct':         0.02,    # was 0.01
   'stop_loss_pct':    0.035,   # was 0.025
   'take_profit_pct':  0.06,    # was 0.04
   'leverage':         2,       # new field
   'circuit_breaker_pct': 0.05, # new field, was hardcoded 0.03
   'rsi_long_threshold':  45,   # new field, was hardcoded 40
   'rsi_short_threshold': 55,   # new field
   'cooldown_seconds':    0,    # new field, permissive default
   'max_sl_per_day':      10,   # new field, permissive default
   ```
3. Run backtest validation with aggressive config (`python -m backtest.short_validation`)
4. Verify gate: Sharpe(combined) ≥ 0 AND PnL ≥ 0
5. Implement signals → state → protections → loop → risk → exchange (in that order, each behind tests)
6. Full test suite passes
7. Run `safety-invariants-audit` skill (mandatory per CLAUDE.md for changes touching `core/`, `risk/`, `exchange/`, `strategy/`) — confirm no NUNCA-violar rule was crossed (the threshold loosening is allowed; removing the breaker would not be)
8. Generate new futures testnet API keys at `testnet.binancefuture.com`, add to `.env`
9. `docker compose stop trading_bot`
10. Close spot position manually at https://testnet.binance.vision, reset `data/bot_state.json` (user does this — `PreToolUse` hook blocks Claude)
11. `docker compose build trading_bot && docker compose up -d trading_bot`
12. Observe `docker logs -f trading_bot` for 24 h — expected ~5–10 trades/day vs current ~1/9days
13. Merge to `main`

## Risk Surface

The aggressive profile is materially riskier than the conservative baseline. Quantifying:

- **Per-trade dollar exposure:** 2 % sizing × 2× leverage = **4× the original spot bot's exposure**. A SL hit costs ~3.5 % × 4 = 14 % of the *trade's nominal*, but only ~7 % of *balance* (because balance × 2 % × 2× = 4 % of balance, hitting 3.5 % of that = 0.14 % of balance per SL — same math the original 1 %×1×2.5% had at 0.025 %). Wait — recompute: the SL stops the trade when the *price* moves 3.5 %. With leverage 2× and sizing 2 %, balance impact = 2 % × 2× × 3.5 % = **0.14 % of balance per SL**. Original was 1 % × 1× × 2.5 % = 0.025 %. So **a single SL is ~5.6× more painful than before**. Five consecutive SL is now 0.7 % of balance — used to be 0.125 %.
- **Daily breaker headroom:** at −5 %, the bot tolerates ~36 consecutive SL before halting. At previous −3 % with original sizing it was ~120. Despite a looser percentage, the absolute trade count tolerance dropped by ~3×.
- **Strategy risk:** the aggressive short may have negative Sharpe even after combination. Gate of "Sharpe(combined) ≥ 0 AND PnL(combined) ≥ 0" prevents deploying a guaranteed loser, but does not guarantee profitability. Original RSI<45 long-only had Sharpe −0.21; combined version is unproven.
- **Protections deliberately permissive:** `CooldownPeriod(0)` and `StoplossGuard(max_sl=10/day)` are no-ops in practice. A 5-SL revenge-thrashing day is not blocked by these defaults — only by the −5 % daily breaker.
- **Exchange migration risk:** moving from spot to futures touches credentials, account semantics, and order types. Mitigation: testnet only, full test coverage of the new `BinanceClient`, manual spot position closure before switch.
- **State persistence risk:** the `side` field is new; an old bot reading a new state file would crash on the unknown key (not relevant since we don't roll back, but noted).
- **Circuit breaker drift:** if `calc_pnl_short` or the leverage multiplier has a sign/scale bug, the breaker may trip late or not at all. Tests must include leveraged-short-loss-triggers-breaker as an explicit case.
- **MacroFilter asymmetry:** explicitly deferred; documented as known limitation. May produce sub-optimal short entries during NO_TRADE windows that would actually be favorable for shorts.

**Safety invariants preserved (NOT loosened):** circuit breaker exists, `place_order_safe` confirms exchange-side, all error handling kept, no `break` on network errors, no hardcoded credentials, `set_sandbox_mode(True)` still active. Aggressive ≠ unsafe; aggressive = larger blast radius within an intact safety net.

## Open Questions

None remaining at design time. Implementation plan to be drafted by `writing-plans` skill.
