---
name: sqlalchemy-alembic
description: Conventions for migrating bot_state.json and trades_history.json to a relational database using SQLAlchemy 2.x + Alembic migrations.
---

# SQLAlchemy + Alembic conventions

Apply these rules whenever you add or modify persistence that replaces the current JSON files (`bot_state.json`, `trades_history.json`).

## Why we'd migrate

- JSON file persistence has no concurrency guarantees — a crash mid-write corrupts state.
- Query patterns (PnL aggregates, win-rate, equity curve) get expensive as `trades_history.json` grows.
- A relational store gives us transactions, backups, and indexed queries.

## Model shape

- Use SQLAlchemy 2.x declarative style (`DeclarativeBase`, `Mapped[...]`, `mapped_column(...)`). No legacy `Column(...)`.
- One model per table, one file per bounded context (`models/trades.py`, `models/state.py`).
- Mirror the existing JSON field names 1:1 for the first migration — cosmetic renames belong in a separate PR.
- Timestamps as `Mapped[datetime]` with `server_default=func.now()`. Never store them as millis in new schemas; keep millis only on fields that the exchange returns in millis.
- Numeric money fields: `Numeric(18, 8)` — **never** `Float`. Rounding errors on PnL compound.

## Session rules

- Sessions go through `Depends(...)` in FastAPI routes — never module-level.
- The bot loop uses a single long-lived engine but a fresh `Session()` per tick. No shared session across ticks.
- Always `session.commit()` explicitly; never rely on autocommit. Wrap writes in a `try/except` that rolls back on `SQLAlchemyError`.

## Migrations (Alembic)

- One migration per logical change. No squashing once a migration is on `main`.
- Autogenerate is a starting point, not the output. Review every generated migration and rewrite `op.add_column` to include the default so backfill is part of the same transaction.
- `upgrade()` and `downgrade()` must both work on a non-empty DB. If `downgrade()` is destructive, raise in it and document why.
- Never edit a migration after it's been run in any environment. Create a new one.

## Migration path from JSON

- Write a one-off script under `scripts/migrate_json_to_db.py` that reads both JSON files and inserts idempotently (use the exchange order id as a unique key where possible).
- Keep the JSON writers active for one release as a fallback. Remove them only after the DB has been authoritative for at least 7 days.
- The state reconciler on startup must still prefer the exchange's truth over DB state, same as today — the DB does not change that rule.

## Testing

- Unit tests use an in-memory SQLite with the same schema. Mark tests that rely on Postgres-only features and skip them on SQLite.
- Integration tests that touch a real Postgres run in CI only, behind a marker.
- Never mock the ORM in tests that verify transactional behavior — use a real session with `rollback()` in teardown.
