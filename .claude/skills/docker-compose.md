---
name: docker-compose
description: Guardrails for Dockerfile and docker-compose.yml changes in the trading bot — production runs bare-metal on GCP systemd, so containers are a dev/staging tool only.
---

# Docker & Docker Compose conventions

Apply these rules when touching `Dockerfile`, `docker-compose.yml`, or container-related config.

## Intent in this repo

- Production on GCP e2-micro uses **bare-metal Python + systemd**, not Docker.
- The `Dockerfile` exists for development reproducibility, CI, and future staging/paper-trading environments — not for production deploys.
- Do not introduce changes that assume the bot always runs in a container.

## Dockerfile rules

- Use `python:3.13-slim` as the base. Do not pin to a non-slim variant without a reason.
- Multi-stage if build tools are needed; final stage must drop build deps.
- Non-root `USER`. Never `USER root` in the final stage.
- Pin `requirements.txt` by hash in production images. `pip install --no-cache-dir --require-hashes`.
- `HEALTHCHECK` must hit the `/health` endpoint in `api.py`, not a TCP ping.
- Copy only what's needed (`.dockerignore` must exclude `venv/`, `__pycache__/`, `*.log*`, `bot_state.json`, `trades_history.json`, `.env`).

## docker-compose rules

- Never commit a `docker-compose.yml` that hardcodes credentials. Use `${VAR}` and an `.env` file outside version control.
- The `bot` service must mount a named volume for `bot_state.json` and `trades_history.json` — losing these is losing money.
- Database services (if added, e.g. Postgres) must have an explicit volume + a healthcheck; the bot's `depends_on` must use `condition: service_healthy`.
- Port mappings: only expose the dashboard (`api.py`) port. The bot loop itself never needs an exposed port.

## What NOT to do

- Do not run the live-trading bot under `docker run` without an explicit restart policy and a volume for state files.
- Do not use `--network host` in production compose files — it bypasses docker's isolation for no gain here.
- Do not add orchestration (Kubernetes, ECS) manifests without discussing first — this repo's deployment model is systemd.
