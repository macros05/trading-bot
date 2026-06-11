---
name: prometheus-grafana
description: Guardrails for exposing Prometheus metrics from the bot and api.py, and for wiring Grafana dashboards — aimed at replacing the fragile log-parsing dashboard.
---

# Prometheus + Grafana conventions

Apply these rules when adding metrics instrumentation, creating dashboards, or wiring alert rules.

## Goal

- Replace the current log-parsing approach in the dashboard with structured metrics.
- The frontend should read from a metrics endpoint or a DB — never from `bot.log` regex parses.

## Metric naming

- Lowercase, snake_case, with a `trading_bot_` prefix. Example: `trading_bot_pnl_usdt_total`.
- Units in the metric name: `_seconds`, `_bytes`, `_total`, `_pct`. Never mix.
- Counters end in `_total`. Histograms end in `_seconds` / `_bytes`.
- Labels: low cardinality only. `symbol=BTCUSDT`, `result=WIN|LOSS` — OK. `order_id=...` — **never** (unbounded cardinality kills Prometheus).

## What to export

- `trading_bot_tick_duration_seconds` (histogram) — loop tick latency.
- `trading_bot_last_tick_timestamp_seconds` (gauge) — for staleness alerts.
- `trading_bot_state` (gauge, label=state) — 1 for active state, 0 otherwise.
- `trading_bot_daily_pnl_usdt` (gauge).
- `trading_bot_trade_total{result}` (counter) — incremented on trade close.
- `trading_bot_position_unrealized_pnl_usdt` (gauge, only when in position).

## Exposing metrics

- Use `prometheus_client` (sync) or `prometheus-async` (async).
- Mount `/metrics` on `api.py`. It must be **public** (no auth) on a separate internal-only port, or authenticated behind the same login cookie — never exposed on the internet unauthenticated.
- Registry is a single module-level instance. Do not create a new `CollectorRegistry` per request.

## Scrape config

- Prometheus `scrape_interval` of 15s is the default. The bot loop tick is faster than that, so gauges must be updated on every tick, not only on trade events.
- Set `honor_labels: false` — we control the label set from the exporter.

## Grafana dashboards

- Dashboards are code: commit JSON exports under `deploy/grafana/` and treat edits in the UI as drafts until they're re-exported.
- Panels must show units (USDT, seconds, percent). Unlabeled numbers are a trap.
- Every dashboard has a top-row "staleness" panel that alerts if `time() - trading_bot_last_tick_timestamp_seconds > 300`. This mirrors the `_STALE_SECONDS` rule in `api.py`.

## Alerts

- Critical alerts (bot stale, circuit breaker tripped, exchange errors) page. Everything else is a notification.
- Alert rules live in `deploy/prometheus/alerts.yml`. One file, one review path.
- Always include a `runbook_url` annotation pointing to the repo path for the runbook, even if the runbook is just a paragraph.
