# Short Positions (Aggressive Profile) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add long+short trading capability under a single-position FSM, migrate the exchange client from Binance Spot Testnet to Binance USDT-M Futures Testnet, add a composable protections framework, and apply aggressive risk dials (2× leverage, 2 % sizing, RSI 45/55 thresholds, 3.5 %/6 % SL/TP, −5 % daily breaker) — all while preserving the CLAUDE.md NUNCA-violar safety invariants.

**Architecture:** Pure-function signal layer gains mirror short functions. State payload gains `side` field with backwards-compat default. New `risk/protections.py` module composes pluggable pre-entry gates. Loop branches by side on entry evaluation and exit checking. Exchange client swaps to `ccxt.binanceusdm`. Backtest engine refactors a per-tick helper to support both directions for the gate script.

**Tech Stack:** Python 3.13, asyncio, ccxt 4.5.48 + ccxt.pro, pandas, unittest IsolatedAsyncioTestCase, Docker Compose.

**Reference spec:** [docs/superpowers/specs/2026-04-26-short-positions-design.md](../specs/2026-04-26-short-positions-design.md)

---

## File Structure

**Create:**
- `risk/protections.py` — `CooldownPeriod`, `StoplossGuard`, `ProtectionStack`
- `backtest/short_validation.py` — gate script that runs three configs and emits `short_validation_aggressive.json`
- `tests/test_protections.py` — full coverage of the new module
- `tests/test_short_validation.py` — smoke test for the gate script

**Modify:**
- `config.py` — add 4 new fields (`leverage`, `rsi_short_threshold`, `cooldown_seconds`, `max_sl_per_day`); change values of 5 existing fields
- `strategy/signals.py` — add 4 mirror functions; generalize `check_exit_price` with `side` param
- `core/state.py` — add `side` to position payload with backwards-compat default `'long'`
- `risk/manager.py` — accept `leverage` for PnL scaling; threshold already config-driven
- `core/loop.py` — dual-direction signal eval, side-aware exit, leverage in PnL accounting, protections wiring
- `exchange/client.py` — swap to `ccxt.binanceusdm`, futures testnet endpoint, `set_leverage(2)`
- `backtest/engine.py` — extract per-tick simulation helper accepting `side`
- `main.py` — instantiate `ProtectionStack`, pass to `trading_loop`
- `tests/test_signals.py`, `tests/test_state.py`, `tests/test_loop.py`, `tests/test_risk_manager.py`, `tests/test_exchange_client.py` — extend each
- `.env` — add `BINANCE_FUTURES_API_KEY`, `BINANCE_FUTURES_API_SECRET` (manual; PreToolUse hook blocks Claude)

**Branch:** `feature/short-positions-aggressive` from `main`

---

## Phase A — Config + Backtest Gate

### Task 1: Add aggressive-profile fields to `config.py`

**Files:**
- Modify: `config.py`
- Test: `tests/test_config.py` (create if absent — likely doesn't exist)

- [ ] **Step 1: Read current config.py**

```bash
cat config.py
```

- [ ] **Step 2: Edit config.py — replace constants and BOT_CONFIG with aggressive values**

Edit `config.py` to set:

```python
# ── Risk & sizing (AGGRESSIVE PROFILE — see docs/superpowers/specs/2026-04-26-short-positions-design.md)
RISK_PCT = 0.02            # was 0.01
STOP_LOSS_PCT = 0.035      # was 0.025
TAKE_PROFIT_PCT = 0.060    # was 0.040

# ── Leverage (futures only) ──────────────────────────────────────────────────
LEVERAGE = 2               # new — applied at init via exchange.set_leverage()

# ── Circuit breaker ──────────────────────────────────────────────────────────
CIRCUIT_BREAKER_PCT = 0.05 # was 0.03

# ── Signal thresholds ────────────────────────────────────────────────────────
RSI_LONG_THRESHOLD = 45.0  # was 40 (now configurable; aggressive)
RSI_SHORT_THRESHOLD = 55.0 # new — short mirror

# ── Protections (permissive aggressive defaults — framework present, not active)
COOLDOWN_SECONDS = 0       # new — 0 = disabled
MAX_SL_PER_DAY = 10        # new — effectively disabled
```

Then update `BOT_CONFIG` dict to add these keys:

```python
BOT_CONFIG = {
    # ... existing keys ...
    'leverage':              LEVERAGE,
    'rsi_threshold':         RSI_LONG_THRESHOLD,   # rename target of existing key
    'rsi_short_threshold':   RSI_SHORT_THRESHOLD,
    'cooldown_seconds':      COOLDOWN_SECONDS,
    'max_sl_per_day':        MAX_SL_PER_DAY,
    # circuit_breaker_pct already plumbed via existing key
}
```

Keep all the ATR/ADX/trailing stop keys as-is.

- [ ] **Step 3: Verify Python imports `config.py` cleanly**

Run: `python3 -c "from config import BOT_CONFIG; print({k: BOT_CONFIG[k] for k in ('leverage', 'rsi_short_threshold', 'risk_pct', 'stop_loss_pct', 'take_profit_pct', 'circuit_breaker_pct', 'cooldown_seconds', 'max_sl_per_day')})"`

Expected output: a dict containing `leverage=2, rsi_short_threshold=55.0, risk_pct=0.02, stop_loss_pct=0.035, take_profit_pct=0.06, circuit_breaker_pct=0.05, cooldown_seconds=0, max_sl_per_day=10`

- [ ] **Step 4: Commit**

```bash
git add config.py
git commit -m "feat(config): aggressive risk profile + short threshold + protections fields"
```

---

### Task 2: Extract per-tick backtest helper that supports both sides

**Files:**
- Modify: `backtest/engine.py` (extract helper, no behavior change for long-only)
- Test: `tests/test_backtest_engine.py` (extend with side='short' case)

- [ ] **Step 1: Read backtest/engine.py to understand the current per-tick simulation loop**

```bash
sed -n '1,80p' backtest/engine.py
grep -n 'def ' backtest/engine.py
```

- [ ] **Step 2: Write a failing test that calls a not-yet-existing `simulate_tick(side, ...)` for `side='short'`**

Append to `tests/test_backtest_engine.py`:

```python
def test_simulate_tick_short_take_profit():
    """Short closes at TP when price drops below entry × (1 - tp_pct)."""
    from backtest.engine import simulate_tick
    state = {'side': 'short', 'entry_price': 100.0, 'qty': 1.0, 'sl_pct': 0.035, 'tp_pct': 0.06}
    result = simulate_tick(close=93.0, state=state)
    assert result['exit_reason'] == 'take_profit'
    assert result['pnl_usdt'] > 0
    assert abs(result['pnl_usdt'] - 7.0) < 1e-6  # (100 - 93) * 1


def test_simulate_tick_short_stop_loss():
    """Short closes at SL when price rises above entry × (1 + sl_pct)."""
    from backtest.engine import simulate_tick
    state = {'side': 'short', 'entry_price': 100.0, 'qty': 1.0, 'sl_pct': 0.035, 'tp_pct': 0.06}
    result = simulate_tick(close=104.0, state=state)
    assert result['exit_reason'] == 'stop_loss'
    assert result['pnl_usdt'] < 0


def test_simulate_tick_long_unchanged():
    """Long behavior identical to pre-refactor."""
    from backtest.engine import simulate_tick
    state = {'side': 'long', 'entry_price': 100.0, 'qty': 1.0, 'sl_pct': 0.025, 'tp_pct': 0.04}
    assert simulate_tick(close=104.5, state=state)['exit_reason'] == 'take_profit'
    assert simulate_tick(close=97.0,  state=state)['exit_reason'] == 'stop_loss'
    assert simulate_tick(close=101.0, state=state)['exit_reason'] is None
```

- [ ] **Step 3: Run tests to verify failure**

Run: `python3 -m unittest tests.test_backtest_engine -k simulate_tick -v`
Expected: FAIL with `ImportError: cannot import name 'simulate_tick'` or `AttributeError`

- [ ] **Step 4: Add `simulate_tick` to `backtest/engine.py`**

Add at the end of `backtest/engine.py` (near the existing per-tick logic — find it first):

```python
def simulate_tick(close: float, state: dict) -> dict:
    """Pure per-tick exit decision. Side-aware.

    state keys: side ('long'|'short'), entry_price, qty, sl_pct, tp_pct
    Returns: {'exit_reason': 'take_profit'|'stop_loss'|None, 'pnl_usdt': float}
    """
    side = state['side']
    entry = state['entry_price']
    qty = state['qty']
    sl_pct = state['sl_pct']
    tp_pct = state['tp_pct']

    if side == 'long':
        change = (close - entry) / entry
        pnl_usdt = (close - entry) * qty
        if change <= -sl_pct:
            return {'exit_reason': 'stop_loss', 'pnl_usdt': pnl_usdt}
        if change >= tp_pct:
            return {'exit_reason': 'take_profit', 'pnl_usdt': pnl_usdt}
    elif side == 'short':
        change = (entry - close) / entry
        pnl_usdt = (entry - close) * qty
        if change <= -sl_pct:
            return {'exit_reason': 'stop_loss', 'pnl_usdt': pnl_usdt}
        if change >= tp_pct:
            return {'exit_reason': 'take_profit', 'pnl_usdt': pnl_usdt}
    else:
        raise ValueError(f'invalid side: {side!r}')

    return {'exit_reason': None, 'pnl_usdt': 0.0}
```

- [ ] **Step 5: Run tests to verify pass**

Run: `python3 -m unittest tests.test_backtest_engine -k simulate_tick -v`
Expected: 3 PASS

- [ ] **Step 6: Commit**

```bash
git add backtest/engine.py tests/test_backtest_engine.py
git commit -m "refactor(backtest): extract simulate_tick helper supporting long+short"
```

---

### Task 3: Create `backtest/short_validation.py` gate script

**Files:**
- Create: `backtest/short_validation.py`
- Create: `tests/test_short_validation.py`

- [ ] **Step 1: Write a failing smoke test**

Create `tests/test_short_validation.py`:

```python
import json
from pathlib import Path
import unittest


class TestShortValidationScript(unittest.TestCase):
    def test_module_exposes_run_validation(self):
        from backtest import short_validation
        self.assertTrue(callable(getattr(short_validation, 'run_validation', None)))

    def test_run_validation_returns_three_configs(self):
        """run_validation() with mock candles returns long_only, short_only, combined."""
        from backtest.short_validation import run_validation
        # 200 synthetic candles oscillating around 100
        candles = []
        ts = 1700000000000
        price = 100.0
        for i in range(500):
            candles.append({'ts': ts + i * 60_000,
                            'open': price, 'high': price + 1, 'low': price - 1,
                            'close': price + (1 if i % 4 < 2 else -1),
                            'volume': 100.0})
            price = price + (1 if i % 4 < 2 else -1)
        results = run_validation(candles, sl_pct=0.035, tp_pct=0.06,
                                 rsi_long_threshold=45, rsi_short_threshold=55)
        self.assertIn('long_only', results)
        self.assertIn('short_only', results)
        self.assertIn('combined', results)
        for cfg_name, cfg in results.items():
            self.assertIn('trades', cfg)
            self.assertIn('pnl_usdt', cfg)
            self.assertIn('sharpe', cfg)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run test to verify failure**

Run: `python3 -m unittest tests.test_short_validation -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Create `backtest/short_validation.py`**

```python
"""Aggressive-profile gate script for short-positions feature.

Runs three backtest configs over the supplied candles and emits a JSON
report. The deployment gate (per the spec) is:
  Sharpe(combined) >= 0 AND PnL(combined) >= 0

Usage:
    python -m backtest.short_validation
"""
import json
import logging
import math
from pathlib import Path
from typing import Any

import pandas as pd

from backtest.engine import simulate_tick
from strategy.indicators import rsi, sma
from strategy.signals import should_enter

logger = logging.getLogger(__name__)

_OUTPUT = Path('backtest/results/short_validation_aggressive.json')
_RSI_PERIOD = 14
_SMA_PERIOD = 20
_MIN_BARS = max(_RSI_PERIOD, _SMA_PERIOD)


def _should_enter_short(close: float, sma20: float, rsi14: float,
                        rsi_threshold: float) -> bool:
    """Inline mirror of strategy/signals.should_enter_short to keep this
    script standalone for the gate run."""
    return rsi14 > rsi_threshold and close < sma20


def _run_one_config(
    candles: list[dict[str, Any]],
    *,
    enable_long: bool,
    enable_short: bool,
    sl_pct: float,
    tp_pct: float,
    rsi_long_threshold: float,
    rsi_short_threshold: float,
    qty_per_trade: float = 1.0,
) -> dict[str, Any]:
    df = pd.DataFrame(candles)
    sma_series = sma(df, period=_SMA_PERIOD)
    rsi_series = rsi(df, period=_RSI_PERIOD)
    position: dict | None = None
    pnls: list[float] = []
    for i in range(_MIN_BARS, len(df)):
        close = float(df['close'].iloc[i])
        sma_v = float(sma_series.iloc[i])
        rsi_v = float(rsi_series.iloc[i])
        if pd.isna(sma_v) or pd.isna(rsi_v):
            continue
        if position is not None:
            res = simulate_tick(close, position)
            if res['exit_reason'] is not None:
                pnls.append(res['pnl_usdt'])
                position = None
            continue
        long_sig = (enable_long and
                    should_enter(close, sma_v, rsi_v, rsi_threshold=rsi_long_threshold))
        short_sig = (enable_short and
                     _should_enter_short(close, sma_v, rsi_v, rsi_short_threshold))
        if long_sig and short_sig:
            continue  # contradictory; do not enter
        if long_sig:
            position = {'side': 'long', 'entry_price': close,
                        'qty': qty_per_trade, 'sl_pct': sl_pct, 'tp_pct': tp_pct}
        elif short_sig:
            position = {'side': 'short', 'entry_price': close,
                        'qty': qty_per_trade, 'sl_pct': sl_pct, 'tp_pct': tp_pct}
    n = len(pnls)
    pnl_total = sum(pnls)
    if n >= 2:
        mean = pnl_total / n
        var = sum((p - mean) ** 2 for p in pnls) / (n - 1)
        sd = math.sqrt(var)
        sharpe = mean / sd if sd > 0 else 0.0
    else:
        sharpe = 0.0
    wins = sum(1 for p in pnls if p > 0)
    return {
        'trades':   n,
        'win_rate': (wins / n) if n > 0 else 0.0,
        'pnl_usdt': round(pnl_total, 4),
        'sharpe':   round(sharpe, 4),
    }


def run_validation(
    candles: list[dict[str, Any]],
    *,
    sl_pct: float = 0.035,
    tp_pct: float = 0.06,
    rsi_long_threshold: float = 45.0,
    rsi_short_threshold: float = 55.0,
) -> dict[str, dict[str, Any]]:
    long_only = _run_one_config(
        candles, enable_long=True, enable_short=False,
        sl_pct=sl_pct, tp_pct=tp_pct,
        rsi_long_threshold=rsi_long_threshold,
        rsi_short_threshold=rsi_short_threshold,
    )
    short_only = _run_one_config(
        candles, enable_long=False, enable_short=True,
        sl_pct=sl_pct, tp_pct=tp_pct,
        rsi_long_threshold=rsi_long_threshold,
        rsi_short_threshold=rsi_short_threshold,
    )
    combined = _run_one_config(
        candles, enable_long=True, enable_short=True,
        sl_pct=sl_pct, tp_pct=tp_pct,
        rsi_long_threshold=rsi_long_threshold,
        rsi_short_threshold=rsi_short_threshold,
    )
    return {'long_only': long_only, 'short_only': short_only, 'combined': combined}


def _gate_passed(results: dict[str, dict[str, Any]]) -> bool:
    c = results['combined']
    return c['sharpe'] >= 0.0 and c['pnl_usdt'] >= 0.0


async def _main() -> None:
    from exchange.client import BinanceClient
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s %(message)s')
    client = BinanceClient()
    try:
        candles = await client.fetch_candles('BTC/USDT', '1m', limit=1000)
        # for the full 90-day gate, the script should be invoked with the same
        # data fetcher used by backtest/engine.py; this _main is a smoke run.
        results = run_validation(candles)
    finally:
        await client.close()
    _OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    _OUTPUT.write_text(json.dumps(results, indent=2))
    logger.info('gate_results=%s passed=%s', results, _gate_passed(results))


if __name__ == '__main__':
    import asyncio
    asyncio.run(_main())
```

- [ ] **Step 4: Run smoke test**

Run: `python3 -m unittest tests.test_short_validation -v`
Expected: PASS (synthetic candles produce some trades or zero trades — both are valid; the test only asserts the structure)

- [ ] **Step 5: Commit**

```bash
git add backtest/short_validation.py tests/test_short_validation.py
git commit -m "feat(backtest): add short_validation gate script for aggressive-profile deployment"
```

- [ ] **Step 6: Run the actual gate against production data (manual checkpoint)**

Run: `python3 -m backtest.short_validation`

Expected output: `backtest/results/short_validation_aggressive.json` exists with `long_only`, `short_only`, `combined` keys.

Inspect: `cat backtest/results/short_validation_aggressive.json`

**STOP HERE if `combined.sharpe < 0` or `combined.pnl_usdt < 0`.** The aggressive profile cannot be deployed without passing this gate. Bring the result back and re-evaluate the spec (e.g., tighten the protections or fall back to a less aggressive RSI threshold). Do not proceed to Phase B.

---

## Phase B — Pure Signal Functions

### Task 4: Add `should_enter_short` to strategy/signals.py

**Files:**
- Modify: `strategy/signals.py`
- Test: `tests/test_signals.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_signals.py`:

```python
class TestShouldEnterShort(unittest.TestCase):
    def test_returns_true_when_rsi_above_threshold_and_close_below_sma(self):
        from strategy.signals import should_enter_short
        self.assertTrue(should_enter_short(close=99.0, sma20=100.0, rsi14=56.0,
                                           rsi_threshold=55.0))

    def test_returns_false_when_rsi_at_threshold(self):
        from strategy.signals import should_enter_short
        self.assertFalse(should_enter_short(close=99.0, sma20=100.0, rsi14=55.0,
                                            rsi_threshold=55.0))

    def test_returns_false_when_close_at_sma(self):
        from strategy.signals import should_enter_short
        self.assertFalse(should_enter_short(close=100.0, sma20=100.0, rsi14=70.0,
                                            rsi_threshold=55.0))

    def test_returns_false_when_close_above_sma(self):
        from strategy.signals import should_enter_short
        self.assertFalse(should_enter_short(close=101.0, sma20=100.0, rsi14=70.0,
                                            rsi_threshold=55.0))

    def test_default_threshold_is_55(self):
        from strategy.signals import should_enter_short
        self.assertTrue(should_enter_short(close=99.0, sma20=100.0, rsi14=56.0))
        self.assertFalse(should_enter_short(close=99.0, sma20=100.0, rsi14=55.0))

    def test_volume_filter_skipped_when_either_arg_none(self):
        from strategy.signals import should_enter_short
        self.assertTrue(should_enter_short(close=99, sma20=100, rsi14=70,
                                           volume=10, volume_sma20=None))
        self.assertTrue(should_enter_short(close=99, sma20=100, rsi14=70,
                                           volume=None, volume_sma20=10))

    def test_volume_filter_blocks_when_volume_below_threshold(self):
        from strategy.signals import should_enter_short
        self.assertFalse(should_enter_short(close=99, sma20=100, rsi14=70,
                                            volume=5, volume_sma20=10,
                                            volume_factor=1.2))
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m unittest tests.test_signals.TestShouldEnterShort -v`
Expected: FAIL with `ImportError: cannot import name 'should_enter_short'`

- [ ] **Step 3: Add `should_enter_short` to `strategy/signals.py`**

Append after the existing `should_enter` function (around line 23):

```python
def should_enter_short(
    close: float,
    sma20: float,
    rsi14: float,
    rsi_threshold: float = 55.0,
    volume: float | None = None,
    volume_sma20: float | None = None,
    volume_factor: float = 1.2,
) -> bool:
    """Mirror of should_enter for short entries.

    Aggressive profile default threshold 55 (mirror of long <45).
    """
    if rsi14 <= rsi_threshold or close >= sma20:
        return False
    if volume is not None and volume_sma20 is not None:
        if volume <= volume_sma20 * volume_factor:
            return False
    return True
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python3 -m unittest tests.test_signals.TestShouldEnterShort -v`
Expected: 7 PASS

- [ ] **Step 5: Commit**

```bash
git add strategy/signals.py tests/test_signals.py
git commit -m "feat(signals): add should_enter_short (mirror of should_enter)"
```

---

### Task 5: Add `calc_pnl_short` and `check_exit_short`

**Files:**
- Modify: `strategy/signals.py`
- Test: `tests/test_signals.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_signals.py`:

```python
class TestCalcPnlShort(unittest.TestCase):
    def test_positive_pnl_when_close_below_entry(self):
        from strategy.signals import calc_pnl_short
        pnl_usdt, pnl_pct = calc_pnl_short(close=95.0, entry_price=100.0, qty=2.0)
        self.assertAlmostEqual(pnl_usdt, 10.0)
        self.assertAlmostEqual(pnl_pct, 5.0)

    def test_negative_pnl_when_close_above_entry(self):
        from strategy.signals import calc_pnl_short
        pnl_usdt, pnl_pct = calc_pnl_short(close=110.0, entry_price=100.0, qty=1.0)
        self.assertAlmostEqual(pnl_usdt, -10.0)
        self.assertAlmostEqual(pnl_pct, -10.0)

    def test_signs_mirror_long_calc_pnl(self):
        from strategy.signals import calc_pnl, calc_pnl_short
        long_pnl, _ = calc_pnl(close=110.0, entry_price=100.0, qty=1.0)
        short_pnl, _ = calc_pnl_short(close=110.0, entry_price=100.0, qty=1.0)
        self.assertAlmostEqual(long_pnl, -short_pnl)


class TestCheckExitShort(unittest.TestCase):
    def test_stop_loss_trips_above_entry(self):
        from strategy.signals import check_exit_short
        # Aggressive: SL 3.5 % above entry
        self.assertEqual(check_exit_short(close=103.5, entry_price=100.0,
                                          stop_loss_pct=0.035, take_profit_pct=0.06),
                         'stop_loss')

    def test_take_profit_trips_below_entry(self):
        from strategy.signals import check_exit_short
        self.assertEqual(check_exit_short(close=94.0, entry_price=100.0,
                                          stop_loss_pct=0.035, take_profit_pct=0.06),
                         'take_profit')

    def test_returns_none_inside_band(self):
        from strategy.signals import check_exit_short
        self.assertIsNone(check_exit_short(close=99.0, entry_price=100.0,
                                           stop_loss_pct=0.035, take_profit_pct=0.06))
        self.assertIsNone(check_exit_short(close=101.0, entry_price=100.0,
                                           stop_loss_pct=0.035, take_profit_pct=0.06))
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m unittest tests.test_signals.TestCalcPnlShort tests.test_signals.TestCheckExitShort -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Append to `strategy/signals.py`**

```python
def calc_pnl_short(
    close: float,
    entry_price: float,
    qty: float,
) -> tuple[float, float]:
    """Return (pnl_usdt, pnl_pct) for a short position.

    PnL is positive when close < entry (price fell after we sold short).
    """
    pnl_usdt = (entry_price - close) * qty
    pnl_pct = (entry_price - close) / entry_price * 100
    return pnl_usdt, pnl_pct


def check_exit_short(
    close: float,
    entry_price: float,
    stop_loss_pct: float = 0.035,
    take_profit_pct: float = 0.06,
) -> str | None:
    """Return 'stop_loss', 'take_profit', or None for a short position.

    For shorts, SL trips when price rises above entry (loss against the short)
    and TP trips when price falls below entry (profit on the short).
    """
    change = (entry_price - close) / entry_price
    if change <= -stop_loss_pct:
        return 'stop_loss'
    if change >= take_profit_pct:
        return 'take_profit'
    return None
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python3 -m unittest tests.test_signals.TestCalcPnlShort tests.test_signals.TestCheckExitShort -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add strategy/signals.py tests/test_signals.py
git commit -m "feat(signals): add calc_pnl_short and check_exit_short"
```

---

### Task 6: Generalize `check_exit_price` to accept `side`

**Files:**
- Modify: `strategy/signals.py`
- Test: `tests/test_signals.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_signals.py`:

```python
class TestCheckExitPriceSideAware(unittest.TestCase):
    def test_long_default_keeps_existing_semantics(self):
        from strategy.signals import check_exit_price
        # SL=95, TP=104 (long convention sl<entry<tp)
        self.assertEqual(check_exit_price(95.0, 95.0, 104.0), 'stop_loss')
        self.assertEqual(check_exit_price(104.0, 95.0, 104.0), 'take_profit')
        self.assertIsNone(check_exit_price(100.0, 95.0, 104.0))

    def test_short_inverts_comparison(self):
        from strategy.signals import check_exit_price
        # SL=103.5, TP=94 (short convention tp<entry<sl)
        self.assertEqual(check_exit_price(103.5, 103.5, 94.0, side='short'),
                         'stop_loss')
        self.assertEqual(check_exit_price(94.0, 103.5, 94.0, side='short'),
                         'take_profit')
        self.assertIsNone(check_exit_price(100.0, 103.5, 94.0, side='short'))

    def test_invalid_side_raises(self):
        from strategy.signals import check_exit_price
        with self.assertRaises(ValueError):
            check_exit_price(100.0, 95.0, 104.0, side='neutral')
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m unittest tests.test_signals.TestCheckExitPriceSideAware -v`
Expected: FAIL on `side='short'` test (function doesn't accept the param yet)

- [ ] **Step 3: Modify `check_exit_price` in `strategy/signals.py`**

Replace the existing `check_exit_price` function with:

```python
def check_exit_price(
    close: float,
    sl_price: float,
    tp_price: float,
    side: str = 'long',
) -> str | None:
    """Return 'stop_loss', 'take_profit', or None based on absolute prices.

    For long positions, SL trips when close <= sl_price and TP when close >= tp_price.
    For short positions the comparisons invert.
    """
    if side == 'long':
        if close <= sl_price:
            return 'stop_loss'
        if close >= tp_price:
            return 'take_profit'
        return None
    if side == 'short':
        if close >= sl_price:
            return 'stop_loss'
        if close <= tp_price:
            return 'take_profit'
        return None
    raise ValueError(f'invalid side: {side!r}')
```

- [ ] **Step 4: Run all signal tests to verify nothing else broke**

Run: `python3 -m unittest tests.test_signals -v`
Expected: all PASS (existing long-only callers default to `side='long'`)

- [ ] **Step 5: Commit**

```bash
git add strategy/signals.py tests/test_signals.py
git commit -m "refactor(signals): generalize check_exit_price with side param"
```

---

### Task 7: Add `update_trailing_stop_short`

**Files:**
- Modify: `strategy/signals.py`
- Test: `tests/test_signals.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_signals.py`:

```python
class TestUpdateTrailingStopShort(unittest.TestCase):
    def test_no_change_below_50pct_progress(self):
        from strategy.signals import update_trailing_stop_short
        # entry=100, tp=90, sl=110. close=98 → progress = (100-98)/(100-90)=0.20
        self.assertEqual(
            update_trailing_stop_short(sl_price=110.0, entry_price=100.0,
                                       tp_price=90.0, close=98.0, atr_val=1.0),
            110.0,
        )

    def test_moves_to_breakeven_at_50pct(self):
        from strategy.signals import update_trailing_stop_short
        # close=95 → progress = 0.50 → SL moves to entry=100
        self.assertEqual(
            update_trailing_stop_short(sl_price=110.0, entry_price=100.0,
                                       tp_price=90.0, close=95.0, atr_val=1.0),
            100.0,
        )

    def test_trails_at_75pct(self):
        from strategy.signals import update_trailing_stop_short
        # close=92.5 → progress = 0.75 → SL = close + atr = 92.5 + 1 = 93.5
        self.assertEqual(
            update_trailing_stop_short(sl_price=110.0, entry_price=100.0,
                                       tp_price=90.0, close=92.5, atr_val=1.0),
            93.5,
        )

    def test_sl_only_moves_down_for_shorts(self):
        from strategy.signals import update_trailing_stop_short
        # SL already at 95; new candidate at 100 should NOT raise it
        self.assertEqual(
            update_trailing_stop_short(sl_price=95.0, entry_price=100.0,
                                       tp_price=90.0, close=95.0, atr_val=1.0),
            95.0,
        )

    def test_invalid_setup_returns_unchanged(self):
        from strategy.signals import update_trailing_stop_short
        # tp >= entry is invalid for a short
        self.assertEqual(
            update_trailing_stop_short(sl_price=110.0, entry_price=100.0,
                                       tp_price=100.0, close=95.0, atr_val=1.0),
            110.0,
        )
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m unittest tests.test_signals.TestUpdateTrailingStopShort -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Append to `strategy/signals.py`**

```python
def update_trailing_stop_short(
    sl_price: float,
    entry_price: float,
    tp_price: float,
    close: float,
    atr_val: float,
) -> float:
    """Mirror of update_trailing_stop for short positions.

    Short SLs sit ABOVE entry. Tightening means moving the SL DOWN toward entry.
    Stages (progress from entry toward TP):
      >= 50 %  → move SL to breakeven (entry_price)
      >= 75 %  → trail SL at 1 × ATR above *close*

    SL only ever moves down — returns min(sl_price, new_sl).
    """
    if tp_price >= entry_price:
        return sl_price
    progress = (entry_price - close) / (entry_price - tp_price)
    new_sl = sl_price
    if progress >= 0.75:
        new_sl = min(new_sl, close + atr_val)
    elif progress >= 0.50:
        new_sl = min(new_sl, entry_price)
    return new_sl
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python3 -m unittest tests.test_signals -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add strategy/signals.py tests/test_signals.py
git commit -m "feat(signals): add update_trailing_stop_short (mirror)"
```

---

## Phase C — State (`side` field with backwards compat)

### Task 8: Persist `side` in position payload, default to 'long' on legacy load

**Files:**
- Modify: `core/state.py`
- Test: `tests/test_state.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_state.py`:

```python
class TestPositionSideField(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        self._tmp.close()
        self._path = Path(self._tmp.name)

    def tearDown(self):
        self._path.unlink(missing_ok=True)

    def test_legacy_position_loads_with_side_long(self):
        """A bot_state.json without `side` is treated as a long position."""
        import json
        from core.state import StateManager
        legacy = {
            'state': 'IN_POSITION',
            'position': {'entry_price': 100.0, 'qty': 0.1, 'ts': 1700000000000},
        }
        self._path.write_text(json.dumps(legacy))
        sm = StateManager(state_file=self._path)
        pos = sm.get_position()
        self.assertEqual(pos.get('side'), 'long')

    def test_short_position_roundtrip(self):
        from core.state import StateManager, BotState
        sm = StateManager(state_file=self._path)
        sm.set_position({'side': 'short', 'entry_price': 100.0, 'qty': 0.1,
                         'ts': 1700000000000, 'sl_price': 103.5, 'tp_price': 94.0})
        sm.set_state(BotState.IN_POSITION)
        sm2 = StateManager(state_file=self._path)
        self.assertEqual(sm2.get_position().get('side'), 'short')

    def test_set_position_none_clears(self):
        from core.state import StateManager
        sm = StateManager(state_file=self._path)
        sm.set_position({'side': 'short', 'entry_price': 100.0, 'qty': 0.1, 'ts': 1})
        sm.set_position(None)
        self.assertIsNone(sm.get_position())
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m unittest tests.test_state.TestPositionSideField -v`
Expected: FAIL on `test_legacy_position_loads_with_side_long` (legacy positions have no `side`)

- [ ] **Step 3: Modify `core/state.py` `_load` method to back-fill `side`**

In `core/state.py`, in the `_load` method, after `payload: dict[str, Any] = json.loads(raw)` and before validation, add:

```python
            # Backwards-compat: legacy positions had no `side` field. Default to 'long'
            # since that is the only direction the bot supported pre-shorts.
            pos = payload.get('position')
            if pos is not None and 'side' not in pos:
                pos['side'] = 'long'
                logger.info('legacy_position_backfilled side=long entry_price=%.4f',
                            pos.get('entry_price', 0.0))
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python3 -m unittest tests.test_state -v`
Expected: all PASS (including the legacy back-fill, the short roundtrip, and existing tests)

- [ ] **Step 5: Commit**

```bash
git add core/state.py tests/test_state.py
git commit -m "feat(state): support side field on Position with backwards-compat default 'long'"
```

---

## Phase D — Protections Framework

### Task 9: Create `risk/protections.py` with `Protection` Protocol and `CooldownPeriod`

**Files:**
- Create: `risk/protections.py`
- Create: `tests/test_protections.py`

- [ ] **Step 1: Write failing tests for `CooldownPeriod`**

Create `tests/test_protections.py`:

```python
import unittest


class TestCooldownPeriod(unittest.TestCase):
    def test_zero_cooldown_never_blocks(self):
        from risk.protections import CooldownPeriod
        cp = CooldownPeriod(cooldown_seconds=0)
        trades = [{'reason': 'stop_loss', 'exit_ts': 1000}]
        blocked, _ = cp.is_blocked(now_ms=2000, trades_history=trades)
        self.assertFalse(blocked)

    def test_blocks_within_cooldown_window(self):
        from risk.protections import CooldownPeriod
        cp = CooldownPeriod(cooldown_seconds=60)
        # SL closed at 1_000_000 ms; now is 1_030_000 → 30s elapsed; cooldown = 60s
        trades = [{'reason': 'stop_loss', 'exit_ts': 1_000_000}]
        blocked, reason = cp.is_blocked(now_ms=1_030_000, trades_history=trades)
        self.assertTrue(blocked)
        self.assertIn('cooldown', reason.lower())

    def test_releases_after_cooldown(self):
        from risk.protections import CooldownPeriod
        cp = CooldownPeriod(cooldown_seconds=60)
        trades = [{'reason': 'stop_loss', 'exit_ts': 1_000_000}]
        blocked, _ = cp.is_blocked(now_ms=1_061_000, trades_history=trades)
        self.assertFalse(blocked)

    def test_only_stop_loss_triggers_cooldown_not_take_profit(self):
        from risk.protections import CooldownPeriod
        cp = CooldownPeriod(cooldown_seconds=60)
        trades = [{'reason': 'take_profit', 'exit_ts': 1_000_000}]
        blocked, _ = cp.is_blocked(now_ms=1_010_000, trades_history=trades)
        self.assertFalse(blocked)

    def test_no_trades_never_blocks(self):
        from risk.protections import CooldownPeriod
        cp = CooldownPeriod(cooldown_seconds=60)
        blocked, _ = cp.is_blocked(now_ms=1_000_000, trades_history=[])
        self.assertFalse(blocked)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m unittest tests.test_protections -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Create `risk/protections.py`**

```python
"""Composable pre-entry guards on top of the circuit breaker.

The circuit breaker (in risk/manager.py) is the absolute hard stop: once the
daily PnL exceeds -circuit_breaker_pct, no entries are allowed.

Protections are softer, additive gates: cooldown periods, max-SL-per-day, etc.
They never override the breaker — they only ADD reasons to refuse an entry.
"""
import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class Protection(Protocol):
    def is_blocked(
        self, now_ms: int, trades_history: list[dict],
    ) -> tuple[bool, str | None]:
        """Return (True, reason) if entry should be blocked, else (False, None)."""
        ...


class CooldownPeriod:
    """Block entries for `cooldown_seconds` after the most recent stop_loss exit.

    Aggressive-profile default cooldown_seconds=0 disables this guard entirely.
    """

    def __init__(self, cooldown_seconds: int = 0) -> None:
        if cooldown_seconds < 0:
            raise ValueError('cooldown_seconds must be >= 0')
        self._cooldown_ms = cooldown_seconds * 1000

    def is_blocked(
        self, now_ms: int, trades_history: list[dict],
    ) -> tuple[bool, str | None]:
        if self._cooldown_ms == 0:
            return False, None
        last_sl_ts = 0
        for trade in trades_history:
            if trade.get('reason') == 'stop_loss':
                ts = int(trade.get('exit_ts', 0))
                if ts > last_sl_ts:
                    last_sl_ts = ts
        if last_sl_ts == 0:
            return False, None
        elapsed = now_ms - last_sl_ts
        if elapsed < self._cooldown_ms:
            remaining = (self._cooldown_ms - elapsed) // 1000
            return True, f'cooldown active, {remaining}s remaining since last stop_loss'
        return False, None
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python3 -m unittest tests.test_protections -v`
Expected: all 5 PASS

- [ ] **Step 5: Commit**

```bash
git add risk/protections.py tests/test_protections.py
git commit -m "feat(risk): add CooldownPeriod protection (permissive default 0s)"
```

---

### Task 10: Add `StoplossGuard` to `risk/protections.py`

**Files:**
- Modify: `risk/protections.py`
- Modify: `tests/test_protections.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_protections.py`:

```python
class TestStoplossGuard(unittest.TestCase):
    def _make_sl_trades(self, count: int, base_ts: int = 1_000_000_000):
        return [{'reason': 'stop_loss', 'exit_ts': base_ts + i * 60_000}
                for i in range(count)]

    def test_permits_below_threshold(self):
        from risk.protections import StoplossGuard
        guard = StoplossGuard(max_sl=10, lookback_seconds=86_400)
        trades = self._make_sl_trades(9)
        blocked, _ = guard.is_blocked(now_ms=1_001_000_000, trades_history=trades)
        self.assertFalse(blocked)

    def test_blocks_at_threshold(self):
        from risk.protections import StoplossGuard
        guard = StoplossGuard(max_sl=10, lookback_seconds=86_400)
        trades = self._make_sl_trades(10)
        blocked, reason = guard.is_blocked(now_ms=1_001_000_000, trades_history=trades)
        self.assertTrue(blocked)
        self.assertIn('stoploss', reason.lower())

    def test_only_counts_within_lookback(self):
        from risk.protections import StoplossGuard
        guard = StoplossGuard(max_sl=3, lookback_seconds=600)  # 10 min window
        # 5 SL hits 20 min ago — outside window
        old = [{'reason': 'stop_loss', 'exit_ts': 1_000_000_000 + i * 60_000}
               for i in range(5)]
        blocked, _ = guard.is_blocked(now_ms=1_000_000_000 + 1_500_000, trades_history=old)
        self.assertFalse(blocked)

    def test_only_counts_stop_loss_not_take_profit(self):
        from risk.protections import StoplossGuard
        guard = StoplossGuard(max_sl=2, lookback_seconds=86_400)
        trades = [{'reason': 'take_profit', 'exit_ts': 1_000_000_000 + i * 60_000}
                  for i in range(10)]
        blocked, _ = guard.is_blocked(now_ms=1_001_000_000, trades_history=trades)
        self.assertFalse(blocked)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m unittest tests.test_protections.TestStoplossGuard -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Append `StoplossGuard` to `risk/protections.py`**

```python
class StoplossGuard:
    """Block entries when SL hits in the last `lookback_seconds` >= `max_sl`.

    Aggressive-profile default max_sl=10 / lookback_seconds=86_400 (24h) is
    effectively permissive for a strategy expected to do <10 SL/day.
    """

    def __init__(self, max_sl: int = 10, lookback_seconds: int = 86_400) -> None:
        if max_sl < 1:
            raise ValueError('max_sl must be >= 1')
        if lookback_seconds <= 0:
            raise ValueError('lookback_seconds must be positive')
        self._max_sl = max_sl
        self._lookback_ms = lookback_seconds * 1000

    def is_blocked(
        self, now_ms: int, trades_history: list[dict],
    ) -> tuple[bool, str | None]:
        cutoff = now_ms - self._lookback_ms
        sl_count = sum(
            1 for trade in trades_history
            if trade.get('reason') == 'stop_loss'
            and int(trade.get('exit_ts', 0)) >= cutoff
        )
        if sl_count >= self._max_sl:
            return True, f'stoploss_guard: {sl_count} SL hits in last {self._lookback_ms // 1000}s'
        return False, None
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python3 -m unittest tests.test_protections -v`
Expected: 9 PASS

- [ ] **Step 5: Commit**

```bash
git add risk/protections.py tests/test_protections.py
git commit -m "feat(risk): add StoplossGuard protection (permissive default 10/day)"
```

---

### Task 11: Add `ProtectionStack` to `risk/protections.py`

**Files:**
- Modify: `risk/protections.py`
- Modify: `tests/test_protections.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_protections.py`:

```python
class TestProtectionStack(unittest.TestCase):
    def test_empty_stack_never_blocks(self):
        from risk.protections import ProtectionStack
        stack = ProtectionStack([])
        blocked, _ = stack.is_blocked(now_ms=0, trades_history=[])
        self.assertFalse(blocked)

    def test_short_circuits_on_first_block(self):
        from risk.protections import ProtectionStack

        class _AlwaysBlocks:
            def __init__(self, name): self.name = name
            def is_blocked(self, now_ms, trades_history):
                return True, f'block from {self.name}'

        class _NeverBlocks:
            def is_blocked(self, now_ms, trades_history):
                raise AssertionError('should not be called')

        stack = ProtectionStack([_AlwaysBlocks('first'), _NeverBlocks()])
        blocked, reason = stack.is_blocked(now_ms=0, trades_history=[])
        self.assertTrue(blocked)
        self.assertEqual(reason, 'block from first')

    def test_passes_when_all_protections_allow(self):
        from risk.protections import ProtectionStack

        class _NeverBlocks:
            def is_blocked(self, now_ms, trades_history):
                return False, None

        stack = ProtectionStack([_NeverBlocks(), _NeverBlocks()])
        blocked, _ = stack.is_blocked(now_ms=0, trades_history=[])
        self.assertFalse(blocked)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m unittest tests.test_protections.TestProtectionStack -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Append `ProtectionStack` to `risk/protections.py`**

```python
class ProtectionStack:
    """Compose multiple protections; short-circuits on the first block.

    The stack is evaluated in declaration order. The first protection that
    returns blocked=True wins; subsequent protections are not consulted.
    """

    def __init__(self, protections: list[Protection]) -> None:
        self._protections = list(protections)

    def is_blocked(
        self, now_ms: int, trades_history: list[dict],
    ) -> tuple[bool, str | None]:
        for protection in self._protections:
            blocked, reason = protection.is_blocked(now_ms, trades_history)
            if blocked:
                return True, reason
        return False, None
```

- [ ] **Step 4: Run all protection tests**

Run: `python3 -m unittest tests.test_protections -v`
Expected: 12 PASS

- [ ] **Step 5: Commit**

```bash
git add risk/protections.py tests/test_protections.py
git commit -m "feat(risk): add ProtectionStack (composes protections, short-circuits on block)"
```

---

## Phase E — Risk Manager (Leverage in PnL)

### Task 12: Add leverage support to `RiskManager` for accurate PnL accounting

**Files:**
- Modify: `risk/manager.py`
- Modify: `tests/test_risk_manager.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_risk_manager.py`:

```python
class TestRiskManagerLeverage(unittest.TestCase):
    def test_default_leverage_is_one_unchanged_behavior(self):
        from risk.manager import RiskManager
        rm = RiskManager(max_daily_drawdown=0.05)
        rm.register_trade(-0.01)  # -1% pct loss
        self.assertAlmostEqual(rm.get_daily_pnl(), -0.01)

    def test_leverage_two_doubles_pnl_impact(self):
        from risk.manager import RiskManager
        rm = RiskManager(max_daily_drawdown=0.05, leverage=2)
        rm.register_trade(-0.01)  # -1% raw → -2% with 2x leverage
        self.assertAlmostEqual(rm.get_daily_pnl(), -0.02)

    def test_circuit_breaker_with_leverage(self):
        from risk.manager import RiskManager
        rm = RiskManager(max_daily_drawdown=0.05, leverage=2)
        # Three trades of -1% each = -2% × 3 = -6% with 2x; breaker trips at -5%
        rm.register_trade(-0.01)
        rm.register_trade(-0.01)
        self.assertFalse(rm.is_circuit_breaker_active())  # -4% so far
        rm.register_trade(-0.01)
        self.assertTrue(rm.is_circuit_breaker_active())   # -6%, exceeds -5%

    def test_invalid_leverage_raises(self):
        from risk.manager import RiskManager
        with self.assertRaises(ValueError):
            RiskManager(max_daily_drawdown=0.05, leverage=0)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m unittest tests.test_risk_manager.TestRiskManagerLeverage -v`
Expected: FAIL on leverage tests

- [ ] **Step 3: Modify `risk/manager.py`**

Update `RiskManager.__init__` and `register_trade`:

```python
    def __init__(
        self,
        max_daily_drawdown: float = 0.03,
        initial_daily_pnl: float = 0.0,
        leverage: int = 1,
    ) -> None:
        if max_daily_drawdown <= 0:
            raise ValueError('max_daily_drawdown must be positive')
        if leverage < 1:
            raise ValueError('leverage must be >= 1')
        self._max_daily_drawdown = max_daily_drawdown
        self._daily_pnl: float = initial_daily_pnl
        self._leverage = leverage
        logger.info(
            'RiskManager ready max_daily_drawdown=%.2f%% initial_daily_pnl=%.4f leverage=%d',
            max_daily_drawdown * 100, initial_daily_pnl, leverage,
        )

    def register_trade(self, pnl: float) -> None:
        """Accumulate *pnl* into the daily counter, scaled by leverage."""
        scaled = pnl * self._leverage
        self._daily_pnl += scaled
        logger.info(
            'register_trade pnl=%.4f leverage=%d scaled=%.4f daily_pnl=%.4f circuit_breaker=%s',
            pnl, self._leverage, scaled, self._daily_pnl, self.is_circuit_breaker_active(),
        )
```

- [ ] **Step 4: Run all risk tests to verify**

Run: `python3 -m unittest tests.test_risk_manager -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add risk/manager.py tests/test_risk_manager.py
git commit -m "feat(risk): RiskManager.register_trade scales pnl by leverage"
```

---

## Phase F — Loop Integration

### Task 13: Extend `_LoopConfig` with new fields

**Files:**
- Modify: `core/loop.py`
- Test: existing `tests/test_loop.py` (no new test, just the extension; existing tests should keep passing)

- [ ] **Step 1: Read core/loop.py:42-80 to see current `_LoopConfig`**

```bash
sed -n '42,80p' core/loop.py
```

- [ ] **Step 2: Add the new fields to `_LoopConfig` and `_parse_config`**

In `core/loop.py`, modify the `_LoopConfig` dataclass and `_parse_config`:

```python
@dataclass(frozen=True)
class _LoopConfig:
    symbol:               str
    timeframe:            str
    limit:                int
    interval:             float
    balance:              float
    risk_pct:             float
    sl_pct:               float
    tp_pct:               float
    rsi_threshold:        float           # long entry threshold
    rsi_short_threshold:  float           # short entry threshold (new)
    leverage:             int             # new
    use_atr_exits:        bool
    atr_sl_multiplier:    float
    atr_tp_multiplier:    float
    use_trailing_stop:    bool
    use_adx_filter:       bool
    adx_threshold:        float
    use_trend_filter:     bool


def _parse_config(raw: dict[str, Any]) -> _LoopConfig:
    return _LoopConfig(
        symbol               = raw['symbol'],
        timeframe            = raw['timeframe'],
        limit                = raw.get('limit', 200),
        interval             = raw['interval_seconds'],
        balance              = raw.get('paper_balance', 10_000.0),
        risk_pct             = raw.get('risk_pct', 0.02),
        sl_pct               = raw.get('stop_loss_pct', 0.035),
        tp_pct               = raw.get('take_profit_pct', 0.060),
        rsi_threshold        = raw.get('rsi_threshold', 45.0),
        rsi_short_threshold  = raw.get('rsi_short_threshold', 55.0),
        leverage             = raw.get('leverage', 1),
        use_atr_exits        = raw.get('use_atr_exits', False),
        atr_sl_multiplier    = raw.get('atr_sl_multiplier', 1.5),
        atr_tp_multiplier    = raw.get('atr_tp_multiplier', 3.0),
        use_trailing_stop    = raw.get('use_trailing_stop', False),
        use_adx_filter       = raw.get('use_adx_filter', False),
        adx_threshold        = raw.get('adx_threshold', 45.0),
        use_trend_filter     = raw.get('use_trend_filter', False),
    )
```

- [ ] **Step 3: Run loop tests — they should still pass with the new defaults**

Run: `python3 -m unittest tests.test_loop -v 2>&1 | tail -20`
Expected: all PASS (BOT_CONFIG already has the new keys from Task 1)

- [ ] **Step 4: Commit**

```bash
git add core/loop.py
git commit -m "refactor(loop): extend _LoopConfig with rsi_short_threshold and leverage"
```

---

### Task 14: Branch `_handle_waiting_signal` to evaluate both directions

**Files:**
- Modify: `core/loop.py`
- Modify: `tests/test_loop.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_loop.py`:

```python
class TestDualDirectionSignals(unittest.IsolatedAsyncioTestCase):
    """Verify _handle_waiting_signal opens long on long signal, short on short signal,
    nothing on contradictory or no signal."""

    def _cfg(self):
        from core.loop import _parse_config
        return _parse_config({
            'symbol': 'BTC/USDT', 'timeframe': '1m', 'interval_seconds': 60,
            'paper_balance': 10_000.0, 'risk_pct': 0.02,
            'stop_loss_pct': 0.035, 'take_profit_pct': 0.06,
            'rsi_threshold': 45.0, 'rsi_short_threshold': 55.0,
            'leverage': 2,
        })

    async def test_short_signal_opens_short_position(self):
        from unittest.mock import MagicMock
        from core.loop import _handle_waiting_signal
        cfg = self._cfg()
        sm = MagicMock()
        rm = MagicMock()
        rm.position_size.return_value = 1000.0  # notional
        # close=99, sma=100 (close<sma), rsi=70 (>55) → short signal fires
        notif = _handle_waiting_signal(
            sm, rm, cfg, close=99.0, sma_val=100.0, rsi_val=70.0,
            atr_val=None, adx_val=None,
        )
        sm.set_position.assert_called_once()
        opened = sm.set_position.call_args[0][0]
        self.assertEqual(opened['side'], 'short')
        self.assertIn('SHORT', notif or '')

    async def test_long_signal_opens_long_position(self):
        from unittest.mock import MagicMock
        from core.loop import _handle_waiting_signal
        cfg = self._cfg()
        sm = MagicMock()
        rm = MagicMock()
        rm.position_size.return_value = 1000.0
        # close=101, sma=100 (close>sma), rsi=30 (<45) → long signal
        notif = _handle_waiting_signal(
            sm, rm, cfg, close=101.0, sma_val=100.0, rsi_val=30.0,
            atr_val=None, adx_val=None,
        )
        sm.set_position.assert_called_once()
        opened = sm.set_position.call_args[0][0]
        self.assertEqual(opened['side'], 'long')

    async def test_no_signal_no_entry(self):
        from unittest.mock import MagicMock
        from core.loop import _handle_waiting_signal
        cfg = self._cfg()
        sm = MagicMock()
        rm = MagicMock()
        # close=100, sma=100, rsi=50 → neither signal fires
        notif = _handle_waiting_signal(
            sm, rm, cfg, close=100.0, sma_val=100.0, rsi_val=50.0,
            atr_val=None, adx_val=None,
        )
        sm.set_position.assert_not_called()
        self.assertIsNone(notif)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m unittest tests.test_loop.TestDualDirectionSignals -v`
Expected: FAIL — current `_handle_waiting_signal` doesn't open shorts

- [ ] **Step 3: Modify `_handle_waiting_signal` in `core/loop.py`**

Add `should_enter_short` to imports at top of file:

```python
from strategy.signals import (
    calc_pnl,
    calc_pnl_short,
    check_exit_price,
    check_exit_short,
    passes_regime_filters,
    should_enter,
    should_enter_short,
    update_trailing_stop,
    update_trailing_stop_short,
)
```

Then replace `_handle_waiting_signal`:

```python
def _handle_waiting_signal(
    state_manager: StateManager,
    risk_manager: RiskManager,
    cfg: _LoopConfig,
    close: float,
    sma_val: float,
    rsi_val: float,
    atr_val: float | None,
    adx_val: float | None,
) -> str | None:
    long_sig = should_enter(close, sma_val, rsi_val, rsi_threshold=cfg.rsi_threshold)
    short_sig = should_enter_short(close, sma_val, rsi_val, rsi_threshold=cfg.rsi_short_threshold)
    if long_sig and short_sig:
        logger.warning(
            'contradictory_signals close=%.4f rsi=%.2f long_thr=%.1f short_thr=%.1f '
            '— refusing to enter (mathematically impossible)',
            close, rsi_val, cfg.rsi_threshold, cfg.rsi_short_threshold,
        )
        return None
    if not long_sig and not short_sig:
        logger.info(
            'no_signal close=%.4f sma%d=%.4f rsi%d=%.2f long_thr=%.1f short_thr=%.1f',
            close, _SMA_PERIOD, sma_val, _RSI_PERIOD, rsi_val,
            cfg.rsi_threshold, cfg.rsi_short_threshold,
        )
        return None
    side = 'long' if long_sig else 'short'
    if not passes_regime_filters(
        trend_bullish=None,
        adx_val=adx_val,
        adx_threshold=cfg.adx_threshold,
        use_trend_filter=cfg.use_trend_filter,
        use_adx_filter=cfg.use_adx_filter,
    ):
        logger.info(
            'regime_blocked side=%s adx%d=%s threshold=%.1f use_adx=%s',
            side, _ADX_PERIOD,
            f'{adx_val:.1f}' if adx_val is not None else 'n/a',
            cfg.adx_threshold, cfg.use_adx_filter,
        )
        return None
    sl_price, tp_price = _compute_sl_tp_for_side(close, cfg, atr_val, side)
    effective_sl_pct = abs(close - sl_price) / close
    notional = risk_manager.position_size(
        cfg.balance, risk_pct=cfg.risk_pct, sl_pct=effective_sl_pct,
    )
    if notional <= 0:
        return None
    qty = notional / close
    logger.info(
        'entry_signal side=%s close=%.4f sma=%.4f rsi=%.2f sl=%.4f tp=%.4f',
        side, close, sma_val, rsi_val, sl_price, tp_price,
    )
    return _open_position(state_manager, close, qty, sl_price, tp_price, side=side)
```

Add a side-aware `_compute_sl_tp_for_side` next to the existing `_compute_sl_tp`:

```python
def _compute_sl_tp_for_side(
    entry_price: float,
    cfg: _LoopConfig,
    atr_val: float | None,
    side: str,
) -> tuple[float, float]:
    """Side-aware SL/TP: long has sl<entry<tp; short has tp<entry<sl."""
    if cfg.use_atr_exits and atr_val is not None and atr_val > 0:
        if side == 'long':
            return (entry_price - cfg.atr_sl_multiplier * atr_val,
                    entry_price + cfg.atr_tp_multiplier * atr_val)
        return (entry_price + cfg.atr_sl_multiplier * atr_val,
                entry_price - cfg.atr_tp_multiplier * atr_val)
    if side == 'long':
        return (entry_price * (1 - cfg.sl_pct), entry_price * (1 + cfg.tp_pct))
    return (entry_price * (1 + cfg.sl_pct), entry_price * (1 - cfg.tp_pct))
```

Modify `_open_position` to accept and persist `side`:

```python
def _open_position(
    state_manager: StateManager,
    close: float,
    qty: float,
    sl_price: float,
    tp_price: float,
    side: str = 'long',
) -> str:
    position = {
        'side':        side,
        'entry_price': close,
        'qty':         qty,
        'ts':          int(time.time() * 1000),
        'sl_price':    sl_price,
        'tp_price':    tp_price,
    }
    state_manager.set_position(position)
    state_manager.set_state(BotState.IN_POSITION)
    logger.info(
        'paper_trade_open side=%s entry_price=%.4f qty=%.6f value_usdt=%.2f sl=%.4f tp=%.4f',
        side, close, qty, close * qty, sl_price, tp_price,
    )
    direction_emoji = '📈' if side == 'long' else '📉'
    return (
        f'{direction_emoji} <b>{side.upper()} POSITION OPENED</b>\n'
        f'Entry: ${close:,.2f}\n'
        f'Qty: {qty:.6f} BTC\n'
        f'Value: ${close * qty:,.2f} USDT\n'
        f'SL: ${sl_price:,.2f} | TP: ${tp_price:,.2f}'
    )
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python3 -m unittest tests.test_loop -v 2>&1 | tail -30`
Expected: dual-direction tests PASS; existing tests still PASS

- [ ] **Step 5: Commit**

```bash
git add core/loop.py tests/test_loop.py
git commit -m "feat(loop): dual-direction entry — evaluate long+short, open whichever fires"
```

---

### Task 15: Branch `_handle_in_position` and `_close_position` by side

**Files:**
- Modify: `core/loop.py`
- Modify: `tests/test_loop.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_loop.py`:

```python
class TestSideAwareExit(unittest.IsolatedAsyncioTestCase):
    def _cfg(self):
        from core.loop import _parse_config
        return _parse_config({
            'symbol': 'BTC/USDT', 'timeframe': '1m', 'interval_seconds': 60,
            'paper_balance': 10_000.0, 'risk_pct': 0.02,
            'stop_loss_pct': 0.035, 'take_profit_pct': 0.06,
            'rsi_threshold': 45.0, 'rsi_short_threshold': 55.0,
            'leverage': 2,
        })

    async def test_short_take_profit_calls_calc_pnl_short(self):
        """A short hitting TP closes with positive PnL via calc_pnl_short."""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from core.loop import _handle_in_position
        from core.state import BotState, StateManager
        from risk.manager import RiskManager

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sm = StateManager(state_file=tmpdir / 'state.json')
            sm.set_position({
                'side': 'short', 'entry_price': 100.0, 'qty': 1.0,
                'ts': 1, 'sl_price': 103.5, 'tp_price': 94.0,
            })
            sm.set_state(BotState.IN_POSITION)
            rm = RiskManager(max_daily_drawdown=0.05, leverage=2)
            with patch('core.loop._TRADES_FILE', tmpdir / 'trades.json'):
                notif = _handle_in_position(sm, rm, self._cfg(), close=94.0, atr_val=None)
            self.assertIsNotNone(notif)
            self.assertIn('TAKE_PROFIT', notif.upper())
            # Daily PnL should be POSITIVE for a short that hit TP
            self.assertGreater(rm.get_daily_pnl(), 0)

    async def test_short_stop_loss_negative_pnl(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from core.loop import _handle_in_position
        from core.state import BotState, StateManager
        from risk.manager import RiskManager

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sm = StateManager(state_file=tmpdir / 'state.json')
            sm.set_position({
                'side': 'short', 'entry_price': 100.0, 'qty': 1.0,
                'ts': 1, 'sl_price': 103.5, 'tp_price': 94.0,
            })
            sm.set_state(BotState.IN_POSITION)
            rm = RiskManager(max_daily_drawdown=0.05, leverage=2)
            with patch('core.loop._TRADES_FILE', tmpdir / 'trades.json'):
                notif = _handle_in_position(sm, rm, self._cfg(), close=103.5, atr_val=None)
            self.assertIn('STOP_LOSS', notif.upper())
            self.assertLess(rm.get_daily_pnl(), 0)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m unittest tests.test_loop.TestSideAwareExit -v`
Expected: FAIL (current loop assumes long)

- [ ] **Step 3: Modify `_handle_in_position` and `_close_position` in `core/loop.py`**

Replace `_handle_in_position`:

```python
def _handle_in_position(
    state_manager: StateManager,
    risk_manager: RiskManager,
    cfg: _LoopConfig,
    close: float,
    atr_val: float | None,
) -> str | None:
    position = state_manager.get_position()
    if position is None:
        logger.error('state=IN_POSITION but no position found, resetting')
        state_manager.set_state(BotState.WAITING_SIGNAL)
        return None
    position = _ensure_sl_tp(position, cfg)
    side = position.get('side', 'long')

    if cfg.use_trailing_stop and atr_val is not None and atr_val > 0:
        if side == 'long':
            new_sl = update_trailing_stop(
                sl_price=position['sl_price'], entry_price=position['entry_price'],
                tp_price=position['tp_price'], close=close, atr_val=atr_val,
            )
            if new_sl > position['sl_price']:
                logger.info('trailing_stop_raised side=long old_sl=%.4f new_sl=%.4f',
                            position['sl_price'], new_sl)
                position['sl_price'] = new_sl
                state_manager.set_position(position)
        else:
            new_sl = update_trailing_stop_short(
                sl_price=position['sl_price'], entry_price=position['entry_price'],
                tp_price=position['tp_price'], close=close, atr_val=atr_val,
            )
            if new_sl < position['sl_price']:
                logger.info('trailing_stop_lowered side=short old_sl=%.4f new_sl=%.4f',
                            position['sl_price'], new_sl)
                position['sl_price'] = new_sl
                state_manager.set_position(position)

    if side == 'long':
        pnl_usdt, pnl_pct = calc_pnl(close, position['entry_price'], position['qty'])
    else:
        pnl_usdt, pnl_pct = calc_pnl_short(close, position['entry_price'], position['qty'])
    logger.info(
        'unrealized_pnl side=%s pnl=%.4f pnl_pct=%.2f%% sl=%.4f tp=%.4f',
        side, pnl_usdt, pnl_pct, position['sl_price'], position['tp_price'],
    )
    reason = check_exit_price(close, position['sl_price'], position['tp_price'], side=side)
    if reason:
        return _close_position(state_manager, risk_manager, close, reason)
    return None
```

Replace `_close_position`:

```python
def _close_position(
    state_manager: StateManager,
    risk_manager: RiskManager,
    close: float,
    reason: str,
) -> str | None:
    position = state_manager.get_position()
    if position is None:
        logger.error('close_position called but no position in state')
        return None

    side = position.get('side', 'long')
    entry_price: float = position['entry_price']
    qty: float = position['qty']
    if side == 'long':
        pnl_usdt, pnl_pct = calc_pnl(close, entry_price, qty)
    else:
        pnl_usdt, pnl_pct = calc_pnl_short(close, entry_price, qty)
    result = 'WIN' if pnl_usdt >= 0 else 'LOSS'

    trade = {
        'side':        side,
        'entry_price': entry_price,
        'exit_price':  close,
        'qty':         qty,
        'pnl_usdt':    round(pnl_usdt, 4),
        'pnl_pct':     round(pnl_pct, 4),
        'result':      result,
        'reason':      reason,
        'entry_ts':    position['ts'],
        'exit_ts':     int(time.time() * 1000),
    }
    _save_trade(trade)
    risk_manager.register_trade(pnl_pct / 100)

    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    state_manager.set_daily_pnl(risk_manager.get_daily_pnl(), today)

    logger.info(
        'paper_trade_close side=%s reason=%s exit_price=%.4f pnl=%.4f pnl_pct=%.2f%% result=%s',
        side, reason, close, pnl_usdt, pnl_pct, result,
    )
    state_manager.set_position(None)
    state_manager.set_state(BotState.WAITING_SIGNAL)

    emoji = '✅' if pnl_usdt >= 0 else '❌'
    return (
        f'{emoji} <b>{side.upper()} CLOSED ({reason.upper()})</b>\n'
        f'Entry: ${entry_price:,.2f}  →  Exit: ${close:,.2f}\n'
        f'PnL: ${pnl_usdt:+.2f} ({pnl_pct:+.2f}%)\n'
        f'Result: {result}'
    )
```

- [ ] **Step 4: Run loop tests**

Run: `python3 -m unittest tests.test_loop -v 2>&1 | tail -30`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add core/loop.py tests/test_loop.py
git commit -m "feat(loop): side-aware exit checking, PnL calc, and trade record"
```

---

### Task 16: Wire `ProtectionStack` into `_on_candles`

**Files:**
- Modify: `core/loop.py`
- Modify: `main.py`
- Modify: `tests/test_loop.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_loop.py`:

```python
class TestProtectionsIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_protection_skips_signal_evaluation(self):
        """When protections.is_blocked() returns True, _process_tick is skipped."""
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock, patch
        from core.loop import trading_loop
        from data.candles import CandleBuffer
        from core.state import StateManager, BotState
        from risk.manager import RiskManager

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sm = StateManager(state_file=tmpdir / 'state.json')
            rm = RiskManager(max_daily_drawdown=0.05, leverage=1)
            buf = CandleBuffer(maxlen=200)

            class _AlwaysBlock:
                def is_blocked(self, now_ms, trades_history):
                    return True, 'test block'

            from risk.protections import ProtectionStack
            stack = ProtectionStack([_AlwaysBlock()])

            cfg = {
                'symbol': 'BTC/USDT', 'timeframe': '1m', 'interval_seconds': 60,
                'limit': 200, 'paper_balance': 10_000.0,
                'risk_pct': 0.02, 'stop_loss_pct': 0.035, 'take_profit_pct': 0.06,
                'rsi_threshold': 45.0, 'rsi_short_threshold': 55.0, 'leverage': 1,
            }
            client = MagicMock()
            client.watch_candles = MagicMock()

            async def _capture(symbol, timeframe, callback, **kwargs):
                await callback([{'ts': 0, 'open': 100, 'high': 101, 'low': 99,
                                 'close': 100, 'volume': 1.0}])
            client.watch_candles.side_effect = _capture

            with patch('core.loop._TRADES_FILE', tmpdir / 'trades.json'), \
                 patch('core.loop._HEALTH_FILE', tmpdir / 'health.json'):
                await trading_loop(client, buf, sm, rm, cfg, protections=stack)
            # Position should never have been opened
            self.assertEqual(sm.get_state(), BotState.WAITING_SIGNAL)
            self.assertIsNone(sm.get_position())
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m unittest tests.test_loop.TestProtectionsIntegration -v`
Expected: FAIL — `trading_loop` doesn't accept a `protections` kwarg

- [ ] **Step 3: Modify `trading_loop` and `_on_candles` in `core/loop.py`**

Add an import at the top of `core/loop.py`:

```python
from risk.protections import ProtectionStack
```

Modify the `trading_loop` signature:

```python
async def trading_loop(
    client: BinanceClient,
    buffer: CandleBuffer,
    state_manager: StateManager,
    risk_manager: RiskManager,
    config: dict[str, Any],
    macro_filter: MacroFilter | None = None,
    protections: ProtectionStack | None = None,
) -> None:
```

In `_on_candles`, after the macro filter check and before `_process_tick`, add:

```python
        if protections is not None:
            now_ms = int(time.time() * 1000)
            blocked, reason = protections.is_blocked(now_ms, _load_trades())
            if blocked:
                logger.info('protections_blocked reason=%s', reason)
                return
```

- [ ] **Step 4: Modify `main.py` to instantiate and pass `ProtectionStack`**

In `main.py`, after the `RiskManager` is created and before `loop_task` setup, add:

```python
    from risk.protections import CooldownPeriod, ProtectionStack, StoplossGuard
    protections = ProtectionStack([
        CooldownPeriod(cooldown_seconds=BOT_CONFIG.get('cooldown_seconds', 0)),
        StoplossGuard(
            max_sl=BOT_CONFIG.get('max_sl_per_day', 10),
            lookback_seconds=86_400,
        ),
    ])
    logger.info('protections active count=%d', 2)
```

Then update the `trading_loop` invocation to pass `protections=protections`:

```python
            loop_task = asyncio.create_task(
                trading_loop(client, buffer, state_manager, risk_manager,
                             BOT_CONFIG, protections=protections)
            )
```

Also update `RiskManager` instantiation in `main.py` to pass leverage:

```python
    risk_manager = RiskManager(
        max_daily_drawdown=BOT_CONFIG.get('circuit_breaker_pct', 0.05),
        initial_daily_pnl=initial_pnl,
        leverage=BOT_CONFIG.get('leverage', 1),
    )
```

- [ ] **Step 5: Run all loop tests**

Run: `python3 -m unittest tests.test_loop -v 2>&1 | tail -30`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add core/loop.py main.py tests/test_loop.py
git commit -m "feat(loop): wire ProtectionStack into _on_candles and main.py bootstrap"
```

---

## Phase G — Exchange Migration to Futures

### Task 17: Migrate `BinanceClient` to USDT-M Futures Testnet

**Files:**
- Modify: `exchange/client.py`
- Modify: `tests/test_exchange_client.py`

- [ ] **Step 1: Write failing tests for futures config**

Append to `tests/test_exchange_client.py`:

```python
class TestBinanceClientFutures(unittest.TestCase):
    def setUp(self):
        import os
        os.environ['BINANCE_FUTURES_API_KEY'] = 'fake_key'
        os.environ['BINANCE_FUTURES_API_SECRET'] = 'fake_secret'

    def test_uses_binanceusdm_class(self):
        from unittest.mock import patch, MagicMock
        with patch('ccxt.binanceusdm') as mock_class:
            mock_inst = MagicMock()
            mock_class.return_value = mock_inst
            from exchange.client import BinanceClient
            BinanceClient(leverage=2)
            mock_class.assert_called_once()
            ctor_kwargs = mock_class.call_args[0][0]
            self.assertEqual(ctor_kwargs['options']['defaultType'], 'future')

    def test_calls_set_sandbox_mode_true(self):
        from unittest.mock import patch, MagicMock
        with patch('ccxt.binanceusdm') as mock_class:
            mock_inst = MagicMock()
            mock_class.return_value = mock_inst
            from exchange.client import BinanceClient
            BinanceClient(leverage=2)
            mock_inst.set_sandbox_mode.assert_called_once_with(True)

    def test_calls_set_leverage_with_configured_value(self):
        from unittest.mock import patch, MagicMock
        with patch('ccxt.binanceusdm') as mock_class:
            mock_inst = MagicMock()
            mock_class.return_value = mock_inst
            from exchange.client import BinanceClient
            BinanceClient(leverage=2, symbol='BTC/USDT')
            mock_inst.set_leverage.assert_called_once_with(2, 'BTC/USDT')

    def test_raises_when_futures_credentials_missing(self):
        import os
        del os.environ['BINANCE_FUTURES_API_KEY']
        from exchange.client import BinanceClient
        with self.assertRaises(RuntimeError):
            BinanceClient(leverage=2)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m unittest tests.test_exchange_client.TestBinanceClientFutures -v`
Expected: FAIL — current client uses `ccxt.binance` and reads `BINANCE_API_KEY`

- [ ] **Step 3: Modify `exchange/client.py` `BinanceClient.__init__`**

Replace the `__init__` method:

```python
    def __init__(self, leverage: int = 1, symbol: str = 'BTC/USDT') -> None:
        api_key: str | None    = os.getenv('BINANCE_FUTURES_API_KEY')
        api_secret: str | None = os.getenv('BINANCE_FUTURES_API_SECRET')
        if not api_key or not api_secret:
            raise RuntimeError(
                'BINANCE_FUTURES_API_KEY and BINANCE_FUTURES_API_SECRET must be set in .env '
                '(generate at https://testnet.binancefuture.com)'
            )
        self._api_key    = api_key
        self._api_secret = api_secret
        self._leverage   = leverage
        self._symbol     = symbol
        self._exchange = ccxt.binanceusdm({
            'apiKey':  api_key,
            'secret':  api_secret,
            'timeout': 10_000,
            'options': {'defaultType': 'future'},
        })
        self._exchange.set_sandbox_mode(True)
        try:
            self._exchange.set_leverage(leverage, symbol)
            logger.info('futures_leverage_set leverage=%d symbol=%s', leverage, symbol)
        except Exception as exc:
            logger.warning('set_leverage_failed leverage=%d symbol=%s error=%s '
                           '(may be unavailable in dry-run/test contexts)',
                           leverage, symbol, exc)
        self._pro_exchange: Any | None = None
```

Modify `_ensure_pro_exchange` to use `binanceusdm` and futures default:

```python
    def _ensure_pro_exchange(self) -> Any:
        if self._pro_exchange is not None:
            return self._pro_exchange
        try:
            import ccxt.pro as ccxtpro
        except ImportError as exc:
            raise ImportError(
                'ccxt.pro required for WebSocket — run: pip install ccxt[pro]'
            ) from exc
        self._pro_exchange = ccxtpro.binanceusdm({
            'apiKey':  self._api_key,
            'secret':  self._api_secret,
            'timeout': 10_000,
            'options': {'defaultType': 'future'},
        })
        self._pro_exchange.set_sandbox_mode(True)
        return self._pro_exchange
```

- [ ] **Step 4: Run exchange tests**

Run: `python3 -m unittest tests.test_exchange_client -v 2>&1 | tail -20`
Expected: all PASS

- [ ] **Step 5: Update main.py to pass leverage to client**

In `main.py`, change `client = BinanceClient()` to:

```python
    client = BinanceClient(
        leverage=BOT_CONFIG.get('leverage', 1),
        symbol=BOT_CONFIG.get('symbol', 'BTC/USDT'),
    )
```

- [ ] **Step 6: Commit**

```bash
git add exchange/client.py main.py tests/test_exchange_client.py
git commit -m "feat(exchange): migrate BinanceClient to USDT-M Futures Testnet (binanceusdm)

- Reads BINANCE_FUTURES_API_KEY / BINANCE_FUTURES_API_SECRET
- Sets defaultType=future, leverage configurable (default 1, aggressive=2)
- set_sandbox_mode(True) routes to testnet.binancefuture.com"
```

---

## Phase H — Final Validation

### Task 18: Run the full test suite

- [ ] **Step 1: Run all tests**

Run: `python3 -m unittest discover -s tests -v 2>&1 | tail -10`
Expected: `Ran NN tests in X.XXXs / OK` with NN >= 250

- [ ] **Step 2: If any test fails, fix and re-run before proceeding**

Do NOT proceed to deployment with red tests.

- [ ] **Step 3: Inspect git log to confirm clean commit history**

Run: `git log --oneline main..HEAD`

Expected: ~17-18 commits, one per task, descriptive messages.

---

### Task 19: Run `safety-invariants-audit` skill (mandatory)

- [ ] **Step 1: Invoke the skill**

Use the `Skill` tool to invoke `safety-invariants-audit`. The skill will scan `core/`, `risk/`, `exchange/`, `strategy/` for violations of the 6 NUNCA-VIOLAR rules.

- [ ] **Step 2: Address any violations the skill reports**

Expected: zero violations. The aggressive profile loosens *thresholds*, not *invariants*. If the skill flags anything, fix it before deploying.

- [ ] **Step 3: Commit any fixes**

```bash
git add <fixed-files>
git commit -m "fix: address safety-invariants-audit findings"
```

---

### Task 20: Manual deployment (user-driven, not automatable)

This task documents what the user does — Claude cannot perform several of these because the `PreToolUse` hook blocks edits to `bot_state.json` and `.env`.

- [ ] **Step 1: Generate futures testnet API keys**

Visit https://testnet.binancefuture.com, create an account if needed, generate API keys.

- [ ] **Step 2: Add keys to `.env` (user action, not Claude)**

```dotenv
BINANCE_FUTURES_API_KEY=<new-key>
BINANCE_FUTURES_API_SECRET=<new-secret>
```

- [ ] **Step 3: Stop the running bot**

```bash
docker compose stop trading_bot
```

- [ ] **Step 4: Close the open spot position manually**

Visit https://testnet.binance.vision, close the BTC/USDT long position opened on 2026-04-24 at $77,795.63.

- [ ] **Step 5: Reset bot_state.json (user action, not Claude)**

```bash
echo '{"state":"WAITING_SIGNAL","position":null,"daily_pnl":0.0,"daily_date":""}' > data/bot_state.json
```

- [ ] **Step 6: Rebuild and start**

```bash
docker compose build trading_bot
docker compose up -d trading_bot
```

- [ ] **Step 7: Watch logs for 5 minutes**

```bash
docker compose logs -f trading_bot
```

Expected: `tick symbol=BTC/USDT close=... rsi14=... state=WAITING_SIGNAL` lines flowing every ~2 seconds. The first entry signal (long or short) should appear within hours given the loosened thresholds.

- [ ] **Step 8: Confirm dashboard shows new state**

Open the dashboard at port 3001. Verify:
- BTC price live and updating
- State = WAITING_SIGNAL
- No open position
- Trade history empty for the day (the legacy long should be in history)

- [ ] **Step 9: Merge to main after 24 h of stable operation**

```bash
git checkout main
git merge --no-ff feature/short-positions-aggressive
git push origin main
```

---

## Self-Review

**Spec coverage:**

- ✅ Section 1 (exchange) → Task 17
- ✅ Section 2 (signals: should_enter_short, calc_pnl_short, check_exit_short, update_trailing_stop_short, check_exit_price side param) → Tasks 4–7
- ✅ Section 3 (state side field + backwards compat) → Task 8
- ✅ Section 4 (loop dual-direction + side-aware exit + macro filter ignored for shorts) → Tasks 13–15. **Note:** the spec says shorts ignore the macro filter — the current implementation in Task 14 does NOT yet add a side check around the macro filter. Add this as a small addendum: when `macro_mode == NO_TRADE`, only block long signal evaluation, not short. **Fixing inline below in Task 14b.**
- ✅ Section 5 (risk manager: sizing 2 %, breaker −5 %, leverage in PnL) → Task 12 + Task 1 (config)
- ✅ Section 5b (protections framework) → Tasks 9–11, integrated in Task 16
- ✅ Section 6 (backtest gate) → Tasks 2–3
- ✅ Section 7 (migration of open position) → Task 20
- ✅ Section 8 (tests) → covered across all tasks
- ✅ Section 9 (deployment sequence) → Task 20

**Placeholder scan:** No "TBD"/"TODO"/"implement later" found.

**Type consistency:** `side: 'long' | 'short'` consistent across signals, state payload, loop branching, and protections. `leverage` consistent as `int`. `cooldown_seconds`/`max_sl_per_day` consistent across config, protections, and main.py wiring.

**Gap fix — adding Task 14b for the macro filter side asymmetry:**

### Task 14b: Apply macro filter only to longs

**Files:**
- Modify: `core/loop.py`
- Modify: `tests/test_loop.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_loop.py`:

```python
class TestMacroFilterSideAsymmetry(unittest.IsolatedAsyncioTestCase):
    async def test_macro_no_trade_blocks_long_but_allows_short(self):
        """Per spec section 4: shorts ignore the macro filter in this iteration."""
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock, patch, AsyncMock
        from core.loop import trading_loop
        from core.state import StateManager, BotState
        from data.candles import CandleBuffer
        from risk.manager import RiskManager
        from core.macro_filter import NO_TRADE

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sm = StateManager(state_file=tmpdir / 'state.json')
            rm = RiskManager(max_daily_drawdown=0.05, leverage=1)
            buf = CandleBuffer(maxlen=200)

            macro = MagicMock()
            macro.get_mode = AsyncMock(return_value=NO_TRADE)

            cfg = {'symbol': 'BTC/USDT', 'timeframe': '1m', 'interval_seconds': 60,
                   'limit': 200, 'paper_balance': 10_000.0, 'risk_pct': 0.02,
                   'stop_loss_pct': 0.035, 'take_profit_pct': 0.06,
                   'rsi_threshold': 45.0, 'rsi_short_threshold': 55.0, 'leverage': 1}

            # Build 30 candles where the LAST candle has rsi>55 and close<sma → short signal
            candles = [{'ts': i * 60_000, 'open': 100, 'high': 101, 'low': 99,
                        'close': 100, 'volume': 1.0} for i in range(29)]
            candles.append({'ts': 30 * 60_000, 'open': 100, 'high': 101, 'low': 90,
                            'close': 90, 'volume': 1.0})
            client = MagicMock()
            async def _fire(symbol, timeframe, callback, **kw):
                await callback(candles)
            client.watch_candles = _fire

            with patch('core.loop._TRADES_FILE', tmpdir / 'trades.json'), \
                 patch('core.loop._HEALTH_FILE', tmpdir / 'health.json'):
                await trading_loop(client, buf, sm, rm, cfg, macro_filter=macro)

            # Position MAY have opened as a short despite NO_TRADE macro mode.
            # If indicators don't trigger, that's fine — the assertion is that
            # the macro filter did NOT short-circuit before signal evaluation.
            # We assert by checking that macro.get_mode was called AND signal eval ran.
            macro.get_mode.assert_called()
```

- [ ] **Step 2: Run test to verify failure**

Run: `python3 -m unittest tests.test_loop.TestMacroFilterSideAsymmetry -v`
Expected: depends on current behavior — likely the macro filter currently blocks ALL signal evaluation, so the test passes vacuously. **Make the assertion stronger: check that `_handle_waiting_signal` is invoked.**

- [ ] **Step 3: Modify `_on_candles` macro check in `core/loop.py`**

Find the existing macro_filter block:

```python
        if macro_filter is not None:
            mode = await macro_filter.get_mode()
            if mode == NO_TRADE:
                logger.info('macro_mode=NO_TRADE skipping_signal_evaluation')
                return
```

Replace with a side-asymmetric version that stores the macro mode for use inside `_handle_waiting_signal`:

```python
        macro_mode = None
        if macro_filter is not None:
            macro_mode = await macro_filter.get_mode()
        await _process_tick(buffer, state_manager, risk_manager, cfg, candles,
                            macro_mode=macro_mode)
```

Update `_process_tick` signature to accept `macro_mode`, and pass it to `_handle_waiting_signal`. Inside `_handle_waiting_signal`, after computing `long_sig` and `short_sig`, add:

```python
    # Per spec section 4: NO_TRADE blocks longs only; shorts proceed.
    if macro_mode == NO_TRADE and long_sig:
        logger.info('macro_no_trade_blocks_long')
        long_sig = False
    if not long_sig and not short_sig:
        ...
```

- [ ] **Step 4: Adjust the test to assert the new behavior precisely**

Replace the test body's final assertion with: assert that when only the long signal fires under NO_TRADE, no position opens; when only the short signal fires under NO_TRADE, the short position DOES open.

- [ ] **Step 5: Run tests**

Run: `python3 -m unittest tests.test_loop -v 2>&1 | tail -20`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add core/loop.py tests/test_loop.py
git commit -m "feat(loop): macro NO_TRADE blocks longs only; shorts proceed (spec §4)"
```

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-26-short-positions-aggressive.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration. Best for this plan because there are 21 tasks across 8 phases; isolating each task in its own subagent context keeps the main session lean and lets you review after each Phase boundary.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints. Faster end-to-end but harder to course-correct mid-implementation.

Which approach?
