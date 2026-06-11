# Fail-proof loops — trading-bot — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A single bad candle, corrupt data, or a tick-processing bug must never tear down the WebSocket trading loop; a sustained skip-storm must alert + pause instead of livelocking.

**Architecture:** The loop is event-driven — `client.watch_candles(...)` invokes the `_on_candles` callback per candle. Today an exception in the callback body propagates and tears down the stream. We wrap the trading-logic body (after the heartbeat) in a `run_safe_tick` guard: skippable errors are logged + counted + skipped (stream stays up); `CancelledError` and a new `CriticalTradingError` propagate (never swallowed); N consecutive skips → pause + Telegram alert.

**Tech Stack:** Python 3.12 (venv at `venv/`), asyncio, ccxt.pro, pytest + pytest-asyncio. Run tests with `venv/bin/python -m pytest`.

**Paper-mode note:** the bot is paper/testnet today (no real exchange orders). The `CriticalTradingError` critical path covers safety-invariant/state failures now and is the designated channel for order-confirmation failures when real trading (Phase 3) lands. Safety invariants (circuit breaker, reconciliation, error handling) are untouched.

Spec: `docs/superpowers/specs/2026-05-27-failproof-loops-design.md`. Baseline: 452 passed at `781732e`. Editing `core/loop.py` triggers the repo PostToolUse pytest hook and should be reviewed by the `trading-safety-reviewer` subagent.

---

## File structure

- Modify `core/loop.py` — add `CriticalTradingError`, `run_safe_tick`, `_SKIP_STORM_THRESHOLD`, two `_LoopState` fields; wrap the `_on_candles` body; add the skip-storm handler.
- Test `tests/test_loop.py` — guard behavior + escalation (extends existing file).

---

### Task 1: `run_safe_tick` guard around the trading-logic body

**Files:**
- Modify: `core/loop.py` (add exception class + guard + `_LoopState` fields; wrap `_on_candles`)
- Test: `tests/test_loop.py`

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_loop.py
import asyncio
import pytest
from core.loop import run_safe_tick, CriticalTradingError, _LoopState

class _NoopAlert:
    def __init__(self): self.calls = []
    async def __call__(self, count, exc): self.calls.append((count, exc))

@pytest.mark.asyncio
async def test_skippable_exception_does_not_propagate_and_counts():
    st = _LoopState()
    alert = _NoopAlert()
    async def work(): raise ValueError("bad candle")
    # Must NOT raise — the loop survives.
    await run_safe_tick(work, st, alert, threshold=5)
    assert st.consecutive_fail == 1
    assert alert.calls == []  # below threshold

@pytest.mark.asyncio
async def test_success_resets_counter():
    st = _LoopState()
    st.consecutive_fail = 3
    alert = _NoopAlert()
    async def work(): return None
    await run_safe_tick(work, st, alert, threshold=5)
    assert st.consecutive_fail == 0

@pytest.mark.asyncio
async def test_cancelled_error_propagates():
    st = _LoopState()
    alert = _NoopAlert()
    async def work(): raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await run_safe_tick(work, st, alert, threshold=5)

@pytest.mark.asyncio
async def test_critical_error_propagates_and_is_not_counted():
    st = _LoopState()
    alert = _NoopAlert()
    async def work(): raise CriticalTradingError("safety invariant violated")
    with pytest.raises(CriticalTradingError):
        await run_safe_tick(work, st, alert, threshold=5)
    assert st.consecutive_fail == 0  # critical errors are not skippable
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_loop.py -k "safe_tick or skippable or cancelled or critical" -v`
Expected: FAIL — `run_safe_tick` / `CriticalTradingError` / `_LoopState.consecutive_fail` undefined.

- [ ] **Step 3: Implement the exception, the `_LoopState` fields, and the guard**

Add near the top of `core/loop.py` (after imports):

```python
_SKIP_STORM_THRESHOLD = 5  # consecutive skipped ticks before escalation


class CriticalTradingError(Exception):
    """Failure that must NOT be skipped: a tripped safety invariant, state-machine
    corruption, or (Phase 3) an unconfirmed order. Propagates to escalate
    immediately rather than being counted as a skippable tick."""
```

In the `_LoopState` class definition, add the two fields (defaulting to 0/False in its initializer):

```python
        self.consecutive_fail: int = 0
        self.skip_storm_alerted: bool = False
```

Add the guard (module-level, near `_process_tick`):

```python
async def run_safe_tick(work, loop_state, on_skip_storm, *, threshold) -> None:
    """Run one tick's trading logic. Skippable exceptions are logged + counted so
    the candle stream stays alive; CancelledError and CriticalTradingError
    propagate (never swallowed). After `threshold` consecutive skips, fire
    on_skip_storm once until a success resets the counter."""
    try:
        await work()
    except asyncio.CancelledError:
        raise
    except CriticalTradingError:
        raise
    except Exception as exc:
        loop_state.consecutive_fail += 1
        logger.error(
            'tick_skipped error=%s consecutive=%d',
            exc, loop_state.consecutive_fail,
        )
        if (loop_state.consecutive_fail >= threshold
                and not loop_state.skip_storm_alerted):
            loop_state.skip_storm_alerted = True
            await on_skip_storm(loop_state.consecutive_fail, exc)
        return
    loop_state.consecutive_fail = 0
    loop_state.skip_storm_alerted = False
```

- [ ] **Step 4: Wrap the `_on_candles` body in the guard**

In `trading_loop`, refactor `_on_candles` (around line 1001) so the trading logic runs through `run_safe_tick`. The `_heartbeat()` call stays first and OUTSIDE the guard (it proves the stream is alive even during a skip-storm):

```python
    async def _on_candles(candles: list[dict[str, Any]]) -> None:
        nonlocal _circuit_breaker_notified
        _heartbeat()

        async def _work() -> None:
            nonlocal _circuit_breaker_notified
            if risk_manager.is_circuit_breaker_active():
                daily_pnl = risk_manager.get_daily_pnl()
                logger.warning('circuit_breaker=active daily_pnl=%.4f', daily_pnl)
                if not _circuit_breaker_notified:
                    _circuit_breaker_notified = True
                    await notify(
                        f'🛑 <b>CIRCUIT BREAKER TRIPPED</b>\n'
                        f'Daily PnL: {daily_pnl * 100:+.2f}%\n'
                        f'No new trades until midnight reset.'
                    )
                return
            _circuit_breaker_notified = False
            macro_mode = None
            if macro_filter is not None:
                macro_mode = await macro_filter.get_mode()
            if protections is not None:
                now_ms = int(time.time() * 1000)
                blocked, reason = protections.is_blocked(now_ms, _load_trades())
                if blocked:
                    logger.info('protections_blocked reason=%s', reason)
                    return
            await _process_tick(buffer, state_manager, risk_manager, cfg, candles,
                                loop_state, macro_mode=macro_mode)

        await run_safe_tick(
            _work, loop_state, _on_skip_storm, threshold=_SKIP_STORM_THRESHOLD,
        )
```

`_on_skip_storm` is added in Task 2. For this task only, define a temporary no-op so the suite runs:

```python
    async def _on_skip_storm(count: int, exc: Exception) -> None:
        logger.error('skip_storm count=%d last_error=%s', count, exc)
```

- [ ] **Step 5: Run tests**

Run: `venv/bin/python -m pytest tests/test_loop.py -v && venv/bin/python -m pytest -q`
Expected: new guard tests pass; full suite 456 passed (452 + 4).

- [ ] **Step 6: Commit**

```bash
git add core/loop.py tests/test_loop.py
git commit -m "feat(loop): run_safe_tick guard — skip bad ticks, never tear down the stream"
```

---

### Task 2: Skip-storm escalation — pause + alert

**Files:**
- Modify: `core/loop.py` (`_on_skip_storm` writes the pause file + notifies)
- Test: `tests/test_loop.py`

The bot already has a pause mechanism: `_is_paused()` returns `_PAUSE_FILE.exists()`. A skip-storm pauses new entries by creating that file.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_skip_storm_pauses_and_alerts(monkeypatch, tmp_path):
    import core.loop as loop_mod
    pause_file = tmp_path / "PAUSE"
    monkeypatch.setattr(loop_mod, "_PAUSE_FILE", pause_file)
    sent = []
    async def fake_notify(msg): sent.append(msg)
    monkeypatch.setattr(loop_mod, "notify", fake_notify)

    st = loop_mod._LoopState()
    async def work(): raise ValueError("poison")
    # Drive exactly threshold consecutive failures.
    for _ in range(loop_mod._SKIP_STORM_THRESHOLD):
        await loop_mod.run_safe_tick(
            work, st, loop_mod._on_skip_storm_for_test(st),
            threshold=loop_mod._SKIP_STORM_THRESHOLD,
        )
    assert pause_file.exists()
    assert len(sent) == 1
    assert "SKIP-STORM" in sent[0]
```

> Note: `_on_skip_storm` is a closure inside `trading_loop`. To test it in isolation, extract the escalation body into a module-level `_escalate_skip_storm(count, exc)` and have the closure delegate to it. The test calls a tiny helper `_on_skip_storm_for_test(st)` that returns a coroutine calling `_escalate_skip_storm`.

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_loop.py -k skip_storm -v`
Expected: FAIL — `_escalate_skip_storm` / `_on_skip_storm_for_test` undefined.

- [ ] **Step 3: Implement module-level escalation + delegate the closure to it**

Add module-level in `core/loop.py`:

```python
async def _escalate_skip_storm(count: int, exc: Exception) -> None:
    """A sustained skip-storm means restarting won't help (the stream is alive,
    every tick fails). Pause new entries and alert; do not silently continue."""
    logger.error('skip_storm_escalation count=%d last_error=%s', count, exc)
    try:
        _PAUSE_FILE.touch()
    except OSError as e:
        logger.error('skip_storm_pause_write_failed error=%s', e)
    try:
        await notify(
            f'⚠️ <b>SKIP-STORM</b>\n'
            f'{count} ticks consecutivos fallaron. Entradas PAUSADAS.\n'
            f'Último error: <code>{str(exc)[:200]}</code>'
        )
    except Exception:
        pass


def _on_skip_storm_for_test(loop_state):
    async def _h(count, exc):
        await _escalate_skip_storm(count, exc)
    return _h
```

Replace the temporary `_on_skip_storm` no-op from Task 1 (inside `trading_loop`) with delegation:

```python
    async def _on_skip_storm(count: int, exc: Exception) -> None:
        await _escalate_skip_storm(count, exc)
```

- [ ] **Step 4: Run tests**

Run: `venv/bin/python -m pytest tests/test_loop.py -k skip_storm -v && venv/bin/python -m pytest -q`
Expected: skip-storm test passes; full suite 457 passed.

- [ ] **Step 5: Commit**

```bash
git add core/loop.py tests/test_loop.py
git commit -m "feat(loop): skip-storm escalation — pause new entries + Telegram alert"
```

---

## Post-implementation

- [ ] Run the `trading-safety-reviewer` subagent on the `core/loop.py` changes (per CLAUDE.md, required after editing `core/loop.py`).
- [ ] Confirm full suite green in the venv: `venv/bin/python -m pytest -q`.
- [ ] The container picks up changes on next deploy/rebuild (Docker is canonical). Do not hot-edit the running container.
