---
name: pydantic
description: Conventions for Pydantic v2 models used in config loading, FastAPI request/response bodies, and internal data contracts in the trading bot.
---

# Pydantic v2 conventions

Apply these rules whenever you define or touch a `BaseModel` in this repo.

## Where to use Pydantic

- **Config**: `config.py` should use `BaseSettings` (from `pydantic-settings`) — not ad-hoc `os.getenv` scattered through modules.
- **FastAPI**: every route request body and response body is a `BaseModel`. No raw dicts.
- **Internal contracts**: functions that cross a module boundary and exchange more than 2 fields take/return a model, not a dict.

## v2 specifics

- `ConfigDict` replaces `class Config:`. Use `model_config = ConfigDict(...)`.
- `@field_validator` (not `@validator`). Always declare `mode="before"` or `mode="after"` explicitly.
- `model_dump()` / `model_validate()` — not `.dict()` / `.parse_obj()`.
- `Field(..., ge=, le=, pattern=)` for constraints. Prefer constraints over custom validators where both would work.

## Money and numbers

- Never use `float` for USDT, prices, or PnL. Use `Decimal` with `Field(..., max_digits=18, decimal_places=8)`.
- Floats are OK for indicators (RSI, SMA) where precision of `< 0.01` doesn't matter.
- Exchange-returned millis timestamps stay as `int` (ms since epoch). Convert to `datetime` only at display boundaries.

## Config (BaseSettings)

- One `Settings` class. Load from `.env` via `model_config = SettingsConfigDict(env_file=".env")`.
- Secrets typed as `SecretStr` — do not expose them in `repr` or logs.
- Default values only for non-secret fields. Secrets must raise on missing.

## Request/response models

- Distinct input/output models (`TradeCreate`, `TradeRead`) if they diverge in any field.
- `response_model` on every route. Even `/health` gets a typed response.
- Do not re-export ORM rows directly — always go through a Pydantic model so the API surface doesn't leak DB columns.

## Validation rules

- Validators raise `ValueError` — FastAPI converts that to a 422. Do not raise `HTTPException` from a validator.
- Prefer `Field` constraints to validators. A validator should encode a rule that can't be expressed declaratively.

## Testing

- Test the model itself only when it has non-trivial validators. Basic shape checks are redundant with FastAPI's own tests.
- `pytest.raises(ValidationError)` to assert rejection cases.
