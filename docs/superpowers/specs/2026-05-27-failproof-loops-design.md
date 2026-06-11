# Fail-proof loops — design (trading-bot + polymarket-copytrade)

**Date:** 2026-05-27
**Status:** Design approved (direction); pending spec review + implementation plan
**Scope decision:** both bots, single design (user: "ambos a la vez")
**Approach:** A — per-iteration isolation + close supervision gaps (user-selected over "only harden supervision")

## 1. Problem & scope

A single exception in a loop body can today either **kill the whole loop**
(trading-bot: one bad candle / processing bug / order error tears down the
WebSocket stream via the `_on_candles` callback) or leave a loop **dead with no
restart** (polymarket: the long-cadence loops `rank`, `daily`, `correlation`,
`liquidity`, `threshold` are launched with raw `asyncio.create_task`, outside
the watchdog).

Goal: **an isolated failure never kills the loop; a systematic failure escalates**
(alert + restart/halt/pause) instead of being swallowed forever or crashing.

### In scope
- Per-iteration isolation in both bots.
- Bring polymarket's long-cadence loops under supervision.
- Escalation policy that distinguishes a one-off bad input (skip) from a
  systematic failure (escalate).

### Non-goals (other robustness fronts the user did NOT select — separate specs)
- Log rotation / memory caps / disk guards.
- Crash-safe state beyond what already exists (atomic JSON writes are present).
- Any architecture rewrite or task-framework migration (YAGNI).

### Hard invariants the design must not violate (trading-bot "NUNCA VIOLAR")
- Do **not** remove existing error handling — this only adds layers.
- Do **not** `break`/exit a loop on a network error (handled below the callback
  by the client's reconnect + REST fallback).
- A failure to **confirm an order** is NOT a skippable iteration: "I don't know
  my position" must escalate, never be silently skipped.

## 2. Shared concept — the "safe iteration" primitive

Each bot gets one wrapper, `run_safe_iteration(name, fn, ...)`, with **three
outcomes** (today the code only has two: "swallow forever" or "die"):

1. **Success** → reset that loop's consecutive-failure counter.
2. **Skippable exception** (corrupt data, analysis error, transient API error)
   → log with context (loop name, exception, input if available), increment the
   consecutive-failure counter, and **continue** (skip this iteration). The loop
   stays alive.
3. **Critical exception** (explicit allow-list: order-confirmation failure,
   safety-invariant violation, `asyncio.CancelledError`) → **never swallowed**;
   re-raise / escalate immediately.

**Escalation by accumulation:** when consecutive failures (or a burst within a
sliding window) exceed threshold `N`, escalate: Telegram alert (loop name, last
error, count) + a bot-specific action (see §5). First success resets the counter.

The primitive is small, pure where possible, and unit-testable in isolation:
given a `fn` that raises, assert it logs+counts+returns (does not propagate);
given the critical types, assert it re-raises; given N consecutive raises,
assert it escalates; given a success after failures, assert the counter resets.

## 3. Trading-bot application (`core/loop.py`, `exchange/client.py`)

The trading loop is event-driven: `client.watch_candles(...)` invokes the
`_on_candles` callback per candle. Today an exception inside `_on_candles`
propagates up through `_invoke_callback` and tears down the stream
(`trading_loop` returns → `main.py` restarts it in 10s; if the bad condition
persists this becomes a restart livelock).

Changes:
- Wrap the **trading-logic body** of `_on_candles` (the circuit-breaker check,
  macro mode, protections, `_process_tick`) in `run_safe_iteration`. A single
  bad candle or a `_process_tick` bug is logged, counted, and skipped — the WS
  stream stays up. The heartbeat call already at the top of `_on_candles`
  (proves the stream is alive) stays first and outside the guard.
- The **stream layer below the callback is unchanged**: reconnect-with-backoff
  and REST fallback already make network errors non-fatal; the guard is only
  around trading logic, never around the network loop.
- **Critical path is exempt from skipping:** any failure in `place_order_safe`
  confirmation, or a tripped safety invariant, is in the critical allow-list and
  escalates (pause new entries via the existing circuit-breaker/pause path +
  Telegram alert) rather than being skipped — we never trade blind.
- **Escalation:** after `N` consecutive skipped ticks, alert on Telegram and
  pause new entries (existing circuit-breaker/pause mechanism). Because skipping
  keeps the heartbeat fresh, the container/`main.py` watchdog will NOT restart on
  a skip-storm — escalation must be active (alert + pause), not watchdog-implicit.

Builds on the already-present (uncommitted) livelock fix in this repo
(`_heartbeat` writing `last_tick_ms` even when a guard blocks trading,
`_finite_or_none` keeping `bot_health.json` valid). That fix must be verified +
committed before this work lands on top of it (see §7).

## 4. Polymarket application (`src/main.py`)

Two gaps:
- **Unsupervised long loops.** `rank`, `daily`, `correlation`, `liquidity`,
  `threshold` are raw `create_task` — if the coroutine dies, nothing restarts
  it. The reason they were left out: their `sleep` is much larger than
  `watchdog_max_silence_seconds`, so naive supervision would flag them as
  stalled. Fix: extend `supervise()` with a **per-loop silence budget**
  (`max_silence` / `expected_interval` override). Each long loop heartbeats once
  per iteration; the watchdog tolerates that loop's natural cadence + margin.
  Then all loops are supervised with auto-restart + retry window + give-up.
- **Inner guards swallow forever.** Loop bodies do `except Exception: log...;
  continue` — good for survival, but a permanently-broken loop logs silently
  forever with no escalation. Route inner bodies through the same
  `run_safe_iteration` so consecutive failures are counted and escalate
  (reusing the existing watchdog give-up Telegram style).

Polymarket already has the supervisor scaffolding (heartbeat map, retry sliding
window, give-up → alert → clean stop); this extends it to all loops and adds the
"skip-storm" escalation that restart alone doesn't fix.

## 5. Escalation & alerting (shared policy)

- Per-loop `consecutive_failures` counter; threshold `N` (config; default 5).
- Sliding window variant for bursty-but-not-strictly-consecutive failures
  (reuse polymarket's existing `watchdog_retry_window_seconds` style).
- On threshold: Telegram alert — loop name, last error (truncated), count.
- Action depends on failure shape:
  - **Loop coroutine died** → watchdog restarts it (polymarket give-up after M
    restarts in the window → clean stop + alert; trading-bot main-loop restart).
  - **Loop alive but every iteration fails (skip-storm)** → restarting won't
    help → alert + pause trading (trading-bot) / surface prominently
    (polymarket). Do not silently continue forever.
- First success resets the counter.

## 6. Testing strategy

Shared (per bot, unit-level, no live services):
- Poison input → loop survives: a `fn` that raises is logged, counted, skipped;
  the loop does not propagate / die.
- Critical exception → escalates: order-confirmation failure / safety-invariant
  type re-raises, is NOT skipped.
- N consecutive failures → escalation fires (alert hook called / pause set).
- Success after failures → counter resets to 0.

Trading-bot specifics:
- Feed a candle that makes `_process_tick` raise → assert the stream/loop stays
  alive, heartbeat stays fresh, tick skipped; after N → assert pause + alert.
- Assert a simulated `place_order_safe` confirmation failure is NOT swallowed.
- Edits to `core/loop.py` trigger the repo's PostToolUse pytest hook and should
  be reviewed by the `trading-safety-reviewer` subagent + `safety-invariants-audit`.

Polymarket specifics:
- Inject a failing iteration into a supervised long loop → counts + escalates
  after N; success resets.
- A long loop under supervision with its silence budget is NOT falsely flagged
  as stalled within its expected cadence.

## 7. Verification & prerequisites

- **Polymarket** has a local venv → run `./venv/bin/python -m pytest -q`
  (baseline at design time: 110 passed). The exit-slippage + partial-TP
  invariant fixes were committed at `83d7e3d`.
- **Trading-bot** has no local venv on this host; canonical runtime is Docker.
  Before implementing, decide the local verification path (create a venv /
  run in a disposable container / CI) so TDD can run — "don't break anything"
  requires a green suite we can observe.
- **Prerequisite:** the uncommitted livelock fix in `core/loop.py`/`config.py`/
  `api.py`/`analytics/live_db.py` (+ `tests/test_loop.py`) must be verified and
  committed first; this design builds directly on it.
