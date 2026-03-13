# Mission Control

Cost-aware AI task orchestration platform with:
- FastAPI task API
- Redis + RQ worker execution
- Postgres persistence for tasks, runs, artifacts, and cost telemetry
- Catalog-driven model routing and pricing
- Budget enforcement at both API enqueue time and worker execution time

## Current Features

- Task lifecycle tracking (`queued`, `running`, `success`, `failed`, `failed_permanent`, `blocked_budget`)
- Run-level telemetry (`attempt`, `model`, `tokens_in/out`, `cost_usd`, `wall_time_ms`, `error`)
- Artifact persistence (`content_text` and `content_json`)
- Result retrieval endpoint (`GET /tasks/{task_id}/result`)
- Idempotent task creation via optional `idempotency_key` with dedupe on `(task_type, idempotency_key)`
- Retry policy with transient-error detection, exponential backoff (`next_run_at`), and terminal `failed_permanent` status
- Scheduler service for recurring jobs (`schedules` table + cron-driven task creation)
- API key auth (`X-API-Key`) and Redis token-bucket rate limiting for `POST /tasks`
- Platform endpoints for operations: `/health`, `/ready`, `/metrics`
- Scraping-based `deals_scan_v1` with Best Buy / Newegg / Micro Center collectors and Option A notifications (notify only on qualifying unicorn deals)
- Model Catalog v2:
  - single source of truth for allowed models
  - tier mapping (`cheap`, `standard`, `advanced`)
  - token pricing and routing thresholds
- Decimal-based cost precision (`NUMERIC(12,8)`)
- Structured worker logs with per-run context (`task_id`, `run_id`, `attempt`, `task_type`, `chosen_model`)
- Agent heartbeats + scheduler watchdog for stale worker/scheduler detection (warning events + guarded restart policy)

## Architecture

Services (Docker Compose):
- `api`: FastAPI server
- `worker`: RQ worker executing tasks
- `postgres`: relational storage
- `redis`: queue backend
- `adminer`: optional DB browser
- `scheduler`: cron-like loop that enqueues due schedules and retry-due tasks

Flow:
1. Client creates task via `POST /tasks`.
2. API validates budget + model override, chooses effective model, stores task.
3. API enqueues `worker.run_task(task_id)` to Redis queue.
4. Worker creates/updates `runs`, executes handler, stores telemetry + artifacts.
5. Client fetches state via `/tasks`, `/runs`, `/tasks/{id}/runs`, `/tasks/{id}/result`.

## Repository Layout

- `api/main.py` - API endpoints + SQLAlchemy models (Task, Run, Artifact)
- `api/scheduler.py` - recurring schedule runner
- `api/router.py` - catalog-backed model routing
- `api/models/catalog.py` - catalog loader/validation helpers
- `api/config/models.json` - model catalog config
- `worker/worker.py` - worker runtime, budget enforcement, run lifecycle, observability logs
- `worker/task_handlers/` - task registry and handlers
- `worker/llm/openai_adapter.py` - OpenAI adapter with catalog-driven pricing
- `frontend/` - React + Vite operator console (Tailwind + shadcn/ui)
- `migrations/` - SQL migrations
- `docker-compose.yml` - local orchestration

## Prerequisites

- Docker + Docker Compose
- OpenAI API key (for real LLM execution)
- Node.js 20+ (for local frontend development)

## Environment Setup

Create `.env` in repo root (example values):

```env
POSTGRES_USER=mission
POSTGRES_PASSWORD=mission
POSTGRES_DB=mission_control
DATABASE_URL=postgresql+psycopg://mission:mission@postgres:5432/mission_control
REDIS_URL=redis://redis:6379/0
TASK_RUN_HISTORY_DB_PATH=/app/task_run_history.sqlite3
AI_USAGE_DB_PATH=/app/task_run_history.sqlite3
EVENT_LOG_DB_PATH=/app/task_run_history.sqlite3
SYSTEM_METRICS_DB_PATH=/app/task_run_history.sqlite3
AGENT_HEARTBEAT_DB_PATH=/app/task_run_history.sqlite3
CANDIDATE_PROFILE_DB_PATH=/app/task_run_history.sqlite3
DAILY_OPS_REPORT_DB_PATH=/app/task_run_history.sqlite3

DAILY_BUDGET_USD=1.00
BUDGET_BUFFER_USD=0.02
MISSION_CONTROL_DAY_BOUNDARY_TZ=America/New_York
OPENAI_MIN_COST_USD=0.01
RETRY_BASE_SECONDS=30
RETRY_MAX_SECONDS=900
RUNNING_TASK_RECOVERY_ENABLED=true
RUNNING_TASK_STALE_AFTER_SEC=420
RUNNING_TASK_AUTO_KILL_ENABLED=true
RUNNING_TASK_RECOVERY_MAX_PER_CYCLE=25

USE_LLM=true
OPENAI_API_KEY=sk-...
API_KEY=replace-with-strong-key
RATE_LIMIT_CREATE_CAPACITY=120
RATE_LIMIT_CREATE_REFILL_PER_SEC=2.0
SCHEDULER_INTERVAL_SEC=60
WORKER_HEARTBEAT_ENABLED=true
WORKER_HEARTBEAT_INTERVAL_SEC=15
WATCHDOG_ENABLED=true
WATCHDOG_STALE_AFTER_SEC=180
WATCHDOG_WARNING_COOLDOWN_SEC=300
WATCHDOG_ENABLE_RESTART=false
WATCHDOG_RESTART_MIN_BACKOFF_SEC=60
WATCHDOG_RESTART_MAX_BACKOFF_SEC=3600
DAILY_OPS_REPORT_ENABLED=true
DAILY_OPS_REPORT_RUN_HOUR_UTC=0
DAILY_OPS_REPORT_NOTIFY_CHANNELS=
DAILY_OPS_REPORT_NOTIFY_TTL_SEC=172800
AUTONOMOUS_PLANNER_ENABLED=false
AUTONOMOUS_PLANNER_INTERVAL_SEC=300
AUTONOMOUS_PLANNER_EXECUTE=false
AUTONOMOUS_PLANNER_REQUIRE_APPROVAL=true
AUTONOMOUS_PLANNER_APPROVED=false
AUTONOMOUS_PLANNER_MAX_CREATE_PER_CYCLE=1
AUTONOMOUS_PLANNER_MAX_EXECUTE_PER_CYCLE=2
AUTONOMOUS_PLANNER_MAX_PENDING_TASKS=20
AUTONOMOUS_PLANNER_FAILURE_LOOKBACK_MINUTES=60
AUTONOMOUS_PLANNER_FAILURE_ALERT_COUNT_THRESHOLD=5
AUTONOMOUS_PLANNER_FAILURE_ALERT_RATE_THRESHOLD=0.5
AUTONOMOUS_PLANNER_STALE_TASK_AGE_SECONDS=180
AUTONOMOUS_PLANNER_EXECUTE_TASK_COOLDOWN_SECONDS=600
AUTONOMOUS_PLANNER_HEALTH_CPU_MAX_PERCENT=90
AUTONOMOUS_PLANNER_HEALTH_MEMORY_MAX_PERCENT=90
AUTONOMOUS_PLANNER_HEALTH_DISK_MAX_PERCENT=95
AUTONOMOUS_PLANNER_COST_BUDGET_USD=
AUTONOMOUS_PLANNER_TOKEN_BUDGET=
AUTONOMOUS_PLANNER_CREATE_TASK_TYPE=
AUTONOMOUS_PLANNER_CREATE_PAYLOAD_JSON=
AUTONOMOUS_PLANNER_CREATE_TASK_MODEL=
AUTONOMOUS_PLANNER_CREATE_TASK_MAX_ATTEMPTS=3
AUTONOMOUS_PLANNER_CREATE_TASK_COOLDOWN_SECONDS=1800
UNICORN_MAX_ITEMS_IN_MESSAGE=5
UNICORN_NOTIFY_SEVERITY=info
UNICORN_5090_GPU_MAX_PRICE=2000
UNICORN_5090_PC_MAX_PRICE=4000
DEAL_ALERT_STATE_DB_PATH=/app/task_run_history.sqlite3
DEAL_ALERT_COOLDOWN_SECONDS=21600
DEAL_ALERT_MATERIAL_PRICE_CHANGE_PCT=3
DEAL_ALERT_MATERIAL_PRICE_CHANGE_ABS_USD=25
SCRAPE_TIMEOUT_SECONDS=15
SCRAPE_CACHE_TTL_SECONDS=120
SCRAPE_RATE_LIMIT_SECONDS=2
SCRAPE_RETRY_ATTEMPTS=3
NOTIFY_DEDUPE_TTL_SECONDS=21600
NOTIFY_DISCORD_ALLOWLIST=deals_scan_v1,unicorn_deals_poll_v1,unicorn_deals_rank_v1,jobs_digest_v2,ops_report_v1
NOTIFY_DEV_MODE=false

# Optional: force router fallback if specific models are unavailable
# OPENAI_UNAVAILABLE_MODELS=gpt-5-mini,gpt-5
```

Notes:
- `DATABASE_URL` / `REDIS_URL` are container-network addresses (hostnames `postgres` and `redis`).
- `TASK_RUN_HISTORY_DB_PATH` is the SQLite file used for persistent task execution history (default: `task_run_history.sqlite3` in the worker process working directory).
- `AI_USAGE_DB_PATH` is the SQLite file used for model usage logging (`ai_usage` table). If unset, it falls back to `TASK_RUN_HISTORY_DB_PATH`, then `task_run_history.sqlite3`.
- `EVENT_LOG_DB_PATH` is the SQLite file used for structured activity events (`events` table). If unset, it falls back to `AI_USAGE_DB_PATH`, then `TASK_RUN_HISTORY_DB_PATH`.
- `SYSTEM_METRICS_DB_PATH` is the SQLite file used for periodic system snapshots (`system_metrics` table). If unset, it falls back to `EVENT_LOG_DB_PATH`, then `AI_USAGE_DB_PATH`.
- `AGENT_HEARTBEAT_DB_PATH` is the SQLite file used for worker/scheduler heartbeats (`agent_heartbeats` table). If unset, it falls back to `SYSTEM_METRICS_DB_PATH`, then `EVENT_LOG_DB_PATH`.
- `CANDIDATE_PROFILE_DB_PATH` is the SQLite file used for stored resume profile context (`candidate_resume_profile` table). If unset, it falls back to `TASK_RUN_HISTORY_DB_PATH`, then `AI_USAGE_DB_PATH`.
- `DAILY_OPS_REPORT_DB_PATH` is the SQLite file used for generated daily ops reports (`daily_ops_reports` table). If unset, it falls back to `AGENT_HEARTBEAT_DB_PATH`, then `SYSTEM_METRICS_DB_PATH`.
- `WATCHDOG_*` values control stale detection threshold, warning cooldown, and optional guarded restart attempt cadence.
- `RUNNING_TASK_*` values control scheduler-side stale `running` task recovery (including optional RQ stop requests + requeue).
- `DAILY_OPS_REPORT_*` values control daily report generation time and optional notification routing.
- `AUTONOMOUS_PLANNER_*` values control recommendation/execution mode, guardrails, cadence, and optional auto-create behavior.
- Set `USE_LLM=false` to run simulated handler mode (no OpenAI call).
- `deals_scan_v1` scraping behavior is controlled via `SCRAPE_*` vars above.
- Unicorn notifications are price-threshold based for RTX 5090 GPUs/desktops (laptops excluded) via `UNICORN_5090_*` vars above.
- Deal alert suppression uses persistent key state (`source+sku` fallback `source+url`) with cooldown and material-change rules via `DEAL_ALERT_*` vars.
- If you override `NOTIFY_DISCORD_ALLOWLIST`, keep `jobs_digest_v2` included so Jobs v2 digests can pass through `notify_v1`.
- If `API_KEY` is set, include `X-API-Key` for API requests.

## Task Run History (SQLite)

Worker writes each started run to SQLite table `task_runs` and finalizes it as `succeeded` or `failed`.

- DB file path: `TASK_RUN_HISTORY_DB_PATH` (default `task_run_history.sqlite3`)
- Verification script: `python examples/verify_task_run_history.py`

## AI Usage Logging (SQLite)

Every OpenAI model call is logged to table `ai_usage` with tokens, cost, latency, and status for analytics.

- DB file path: `AI_USAGE_DB_PATH` (fallback: `TASK_RUN_HISTORY_DB_PATH`)
- Verification script: `python examples/verify_ai_usage_logging.py`

## Event Log (SQLite)

System and agent activity is captured in table `events` (`scheduler_started`, `worker_started`, `task_queued`, `task_started`, `task_succeeded`, `task_failed`, `notification_sent`, `scraper_warning`, and related events).

- DB file path: `EVENT_LOG_DB_PATH` (fallback: `AI_USAGE_DB_PATH`, then `TASK_RUN_HISTORY_DB_PATH`)
- Print recent events: `python examples/print_recent_events.py`

## System Metrics (SQLite)

Scheduler records periodic system snapshots to table `system_metrics` (`cpu_percent`, `memory_percent`, `disk_percent`, `load_avg_json`, `created_at`).

- DB file path: `SYSTEM_METRICS_DB_PATH` (fallback: `EVENT_LOG_DB_PATH`, then `AI_USAGE_DB_PATH`)
- Verification script: `python examples/verify_system_metrics.py`

## Deal Alert Dedupe/Cooldown State (SQLite)

`deals_scan_v1` stores per-item alert state in `deal_alert_state` to reduce repeated notifications.

- Stable dedupe key: `source + sku` (fallback `source + normalized_url`)
- Tracks `last_seen_at`, `last_alerted_at`, `cooldown_until`, `last_price`, `last_status`
- Re-alert occurs only when:
  - cooldown expired, or
  - price changed materially, or
  - item status changed significantly

- DB file path: `DEAL_ALERT_STATE_DB_PATH` (fallback: `TASK_RUN_HISTORY_DB_PATH`, then `AI_USAGE_DB_PATH`)
- Verification script: `python examples/verify_deal_alert_dedupe.py`

## Agent Heartbeats + Watchdog (SQLite)

Workers and scheduler periodically upsert `agent_heartbeats` rows:
- `agent_name`
- `last_seen_at`
- `status`
- `metadata_json`

Scheduler watchdog loop checks stale heartbeats and logs warning events:
- `watchdog_agent_stale` when an agent exceeds `WATCHDOG_STALE_AFTER_SEC`
- cooldown protection via `WATCHDOG_WARNING_COOLDOWN_SEC`
- recovery event `watchdog_agent_recovered` when heartbeat resumes

Restart policy:
- default is **log + alert only** (`WATCHDOG_ENABLE_RESTART=false`)
- if restart is enabled, watchdog still uses restart-attempt backoff to avoid endless loops
- current architecture does not execute direct process restarts from scheduler; it relies on existing container restart policies for crash recovery

- DB file path: `AGENT_HEARTBEAT_DB_PATH` (fallback: `SYSTEM_METRICS_DB_PATH`, then `EVENT_LOG_DB_PATH`)
- Verification script: `python examples/verify_agent_heartbeats.py`

## Candidate Resume Profile (SQLite)

Mission Control can store one active resume profile and reuse it in the Jobs v2 pipeline (`jobs_rank_v1` and `jobs_digest_v2`) for fit ranking and digest context.

- Table: `candidate_resume_profile`
- DB file path: `CANDIDATE_PROFILE_DB_PATH` (fallback: `TASK_RUN_HISTORY_DB_PATH`, then `AI_USAGE_DB_PATH`)
- Upload support: PDF and DOCX parsing (plus text-based files)
- API routes:
  - `GET /profile/resume?include_text=true|false`
  - `POST /profile/resume/upload` (PDF, DOCX, or text upload)
  - `PUT /profile/resume`
  - `DELETE /profile/resume`
- Verification script: `python examples/verify_resume_profile.py`

## Daily AI Operations Report

Scheduler generates a deterministic daily report (for the previous UTC day) with:
- tasks completed / failed
- most active task types
- AI tokens + estimated cost
- latest system health snapshot
- notable warning/error events
- rule-based recommendation line (failure spike, cost spike, no successful runs)

Optional notification hook:
- set `DAILY_OPS_REPORT_NOTIFY_CHANNELS=discord` to queue `notify_v1` delivery
- report notifications use `source_task_type=ops_report_v1` (included in default notify allowlist)

Generation + storage:
- report table: `daily_ops_reports`
- DB path: `DAILY_OPS_REPORT_DB_PATH` (fallback: `AGENT_HEARTBEAT_DB_PATH`, then `SYSTEM_METRICS_DB_PATH`)
- sample generator script: `python examples/generate_daily_ops_report.py`
- task/run totals prefer Postgres `runs`/`tasks`; if unavailable, report generation falls back to SQLite `task_runs`

Sample output:

```text
Mission Control Daily AI Ops Report (2026-03-09 UTC)
Tasks: completed=14 failed=2 total_runs=16
Most active tasks: deals_scan_v1 (8), jobs_digest_v1 (5), notify_v1 (3)
AI usage: tokens=48213 estimated_cost_usd=$0.742615 requests=21
Latest system health: cpu=12.4% mem=41.3% disk=56.1% load=0.96 / 0.64 / 0.44 (captured 2026-03-10T00:00:05+00:00)
Notable warnings/errors:
- [WARNING] scraper_warning: Scraper warning: ...
- [ERROR] task_failed: Task failed: deals_scan_v1
Recommendation: Operations are stable. Continue monitoring normal health, cost, and failure trends.
```

## Autonomous Planner (Safe Mode)

Scheduler can run a modular planner loop that inspects:
- pending/running task backlog
- recent run failures
- latest system health
- current AI cost/token usage

Planner decisions are structured as:
- `execute_task`
- `create_task`
- `defer`
- `alert`

Guardrails:
- max tasks created per cycle (`AUTONOMOUS_PLANNER_MAX_CREATE_PER_CYCLE`)
- max tasks executed per cycle (`AUTONOMOUS_PLANNER_MAX_EXECUTE_PER_CYCLE`)
- execute-task cooldown per task (`AUTONOMOUS_PLANNER_EXECUTE_TASK_COOLDOWN_SECONDS`)
- skip execution when system health is poor (`AUTONOMOUS_PLANNER_HEALTH_*`)
- skip execution when budget/token caps are exceeded (`AUTONOMOUS_PLANNER_COST_BUDGET_USD`, `AUTONOMOUS_PLANNER_TOKEN_BUDGET`)
- avoid endless retry behavior by only selecting stale queued tasks with attempts remaining

Modes:
- recommendation mode (default): `AUTONOMOUS_PLANNER_ENABLED=true`, `AUTONOMOUS_PLANNER_EXECUTE=false`
- safe execution mode: `AUTONOMOUS_PLANNER_EXECUTE=true`
- approval gate (optional): keep `AUTONOMOUS_PLANNER_REQUIRE_APPROVAL=true` and set `AUTONOMOUS_PLANNER_APPROVED=true` only when ready

UI controls (no `.env` edits required after startup defaults):
- open `http://localhost:8000/`
- Mission Control UI is organized into tabs: `Task Operations`, `Automation`, and `Telemetry`
- `Task Operations` includes quick builders for Jobs/Deals so payload JSON can be generated from form fields
- use the **Autonomous Planner** card to:
  - enable/disable planner
  - switch recommendation vs execute mode
  - toggle approval requirement
  - approve/revoke execution
  - configure interval and per-cycle limits
  - add/edit/delete automation rules (task templates)
  - run planner once manually
  - enable one-click RTX 5090 preset rule
  - enable one-click Jobs Digest preset rule
- scheduler polls API planner-control endpoints each cycle (`PLANNER_CONTROL_API_*`) so UI changes apply without restarting services

Automation rule behavior:
- rules are persisted in SQLite (`planner_task_templates`)
- planner evaluates enabled rules each cycle
- if a rule has not run within its configured interval and guardrails are healthy, planner proposes `create_task`
- if approval is required and not approved, execution is held with `awaiting_approval` status
- RTX 5090 preset payload includes per-rule unicorn price thresholds (GPU/PC) so target budget can be managed without env edits
- jobs preset payload includes multi-board scraping + expanded watcher criteria (desired titles, include/exclude keywords, locations, work mode, salary floor, experience, source selection, shortlist size, freshness preference)
- planner now materializes a fresh payload per create decision (`planner_generated_at`, `planner_generation_id`, plus optional `{{uuid4}}`/`{{ts_compact}}` placeholders), so autonomous scans do not repeat identical payload blobs

Enable/disable:
- disable planner loop: `AUTONOMOUS_PLANNER_ENABLED=false`
- enable recommendation loop only: `AUTONOMOUS_PLANNER_ENABLED=true` and `AUTONOMOUS_PLANNER_EXECUTE=false`
- enable execution with approval: `AUTONOMOUS_PLANNER_ENABLED=true`, `AUTONOMOUS_PLANNER_EXECUTE=true`, `AUTONOMOUS_PLANNER_REQUIRE_APPROVAL=true`, `AUTONOMOUS_PLANNER_APPROVED=true`

Verification script:
- `python examples/verify_autonomous_planner.py`
- `python examples/verify_planner_controls.py`

## Start the Program

```bash
docker compose up -d --build
```

Check service state:

```bash
docker compose ps
```

## Frontend Development (React + Vite)

The new React operator UI lives in `frontend/` and uses:
- React + TypeScript
- Vite
- Tailwind CSS + shadcn/ui component primitives
- React Router
- TanStack Query

Route model:
- `/` Home
- `/workflows`
- `/runs`
- `/alerts`
- `/settings`
- `/observability`
- legacy UI route redirects: `/tasks -> /runs`, `/automations -> /workflows`, `/system -> /observability`

Run local frontend dev server:

```bash
cd frontend
npm install
npm run dev
```

Local dev URL:
- `http://localhost:5173/`
- Vite proxies API routes to `http://localhost:8000` (no CORS changes required)

Generate typed OpenAPI contracts:

```bash
cd frontend
npm run generate:openapi
```

Frontend env vars (`frontend/.env.example`):
- `VITE_API_BASE_URL` (default empty, same-origin)
- `VITE_REQUEST_TIMEOUT_MS` (default `15000`)
- `VITE_ENABLE_QUERY_DEVTOOLS` (default `false`)

## Daily Operator Checklist

Use this quick workflow for normal operation and verification.

```bash
# 1) Confirm core services are healthy
docker compose ps

# 2) Confirm worker and scheduler are active
docker compose logs --no-color --tail=60 worker
docker compose logs --no-color --tail=60 scheduler

# 3) Trigger one scrape run
API_KEY=$(grep "^API_KEY=" .env | cut -d= -f2-)
curl -s -X POST http://localhost:8000/tasks \
  -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "task_type":"deals_scan_v1",
    "payload_json":"{\"source\":\"daily-check\",\"collectors_enabled\":true}"
  }' | jq

# 4) Inspect latest tasks and unicorn decision
curl -s -H "X-API-Key: $API_KEY" "http://localhost:8000/tasks?limit=10" | jq
docker compose logs --no-color --tail=160 worker
```

## Open It on the Web

- React operator console (new): http://localhost:8000/app
- Legacy static console (current default): http://localhost:8000/
- Legacy static observability: http://localhost:8000/observability
- API docs (Swagger): http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc
- Adminer (DB UI): http://localhost:8000

Adminer login defaults from `.env`:
- System: `PostgreSQL`
- Server: `postgres`
- Username: `POSTGRES_USER`
- Password: `POSTGRES_PASSWORD`
- Database: `POSTGRES_DB`

## Database Migrations

For a fresh DB, app startup creates tables. For existing DB upgrades, run migrations:

```bash
docker compose up -d postgres

docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < migrations/20260225_add_artifacts_table.sql
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < migrations/20260225_cost_precision_numeric.sql
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < migrations/20260225_reliability_scheduler_auth.sql
```

## API Quickstart

### 1) Create a Task

```bash
API_KEY=$(grep "^API_KEY=" .env | cut -d= -f2-)

curl -s -X POST http://localhost:8000/tasks \
  -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "task_type": "jobs_collect_v1",
    "payload_json": "{\"request\":{\"collectors_enabled\":true,\"profile_mode\":\"resume_profile\",\"query\":\"machine learning engineer\",\"location\":\"United States\",\"sources\":[\"linkedin\",\"indeed\",\"glassdoor\",\"handshake\"],\"desired_title_keywords\":[\"machine learning engineer\",\"ai engineer\"],\"desired_salary_min\":160000,\"experience_levels\":[\"entry\",\"mid\",\"senior\"],\"work_modes\":[\"remote\",\"hybrid\"],\"clearance_required\":\"either\",\"notify_on_empty\":false}}",
    "model": null
  }'
```

### 2) List Tasks

```bash
curl -s -H "X-API-Key: $API_KEY" http://localhost:8000/tasks | jq
```

### 3) Get Task Runs

```bash
curl -s -H "X-API-Key: $API_KEY" http://localhost:8000/tasks/<TASK_ID>/runs | jq
```

### 4) Get Latest Result Artifact

```bash
curl -s -H "X-API-Key: $API_KEY" http://localhost:8000/tasks/<TASK_ID>/result | jq
```

### 5) Get Daily Cost Stats

```bash
curl -s -H "X-API-Key: $API_KEY" http://localhost:8000/stats/today | jq
```

## Endpoints

- `POST /tasks`
  - accepts optional `idempotency_key` and returns existing task when duplicate
  - validates optional `model` override against catalog
  - enforces API-side budget gate
  - routes effective model and enqueues work
- `GET /tasks`
- `GET /tasks/{task_id}`
- `GET /tasks/{task_id}/runs`
- `GET /runs`
- `GET /tasks/{task_id}/result`
  - returns latest artifact (`content_text` and/or `content_json`)
- `POST /schedules`
- `GET /schedules`
- `GET /stats/today`
  - spend, budget, remaining, and run counts
  - "today" window uses `MISSION_CONTROL_DAY_BOUNDARY_TZ` (default: `America/New_York`)
- `GET /telemetry/events`
- `GET /telemetry/ai-usage`
- `GET /telemetry/ai-usage/today`
- `GET /telemetry/ai-usage/summary`
- `GET /telemetry/system-metrics/latest`
- `GET /telemetry/system-metrics`
- `GET /telemetry/heartbeats`
- `GET /telemetry/heartbeats/stale`
- `GET /profile/resume`
- `POST /profile/resume/upload`
- `PUT /profile/resume`
- `DELETE /profile/resume`
- `GET /observability`
  - read-only observability dashboard page linked from the main UI header
- `GET /app`
  - React SPA operator console entrypoint (Phase A dual-run path)
- `GET /app/{path}`
  - React SPA client-side route fallback
- `GET /legacy`
  - legacy static operator console alias
- `GET /legacy/observability`
  - legacy static observability alias
- `GET /planner/config`
- `PATCH /planner/config`
- `POST /planner/config/reset`
- `GET /planner/templates`
- `POST /planner/templates`
- `PATCH /planner/templates/{template_id}`
- `DELETE /planner/templates/{template_id}`
- `POST /planner/templates/presets/rtx5090`
- `POST /planner/templates/presets/jobs-digest`
- `POST /planner/run-once`
- `GET /health`
- `GET /ready`
- `GET /metrics`

## Observability Backend (Read-Only)

Dedicated FastAPI app for dashboard-friendly telemetry reads:
- File: `api/observability_api.py`
- Routes are GET-only and read from SQLite helper modules (`task_runs`, `events`, `ai_usage`, `system_metrics`)

Run locally from repo root:

```bash
source .venv/bin/activate
uvicorn api.observability_api:app --host 0.0.0.0 --port 8010
```

Dashboard/UI:
- `http://localhost:8010/`
- Observability UI tabs: `Overview`, `Task Activity`, `Agents & Planner`
- API docs: `http://localhost:8010/docs`

Troubleshooting:
- If the browser tab keeps spinning, avoid `--reload` for this app. Reload mode can restart repeatedly when `task_run_history.sqlite3` changes.
- Check server logs for startup/runtime errors:
  - `uvicorn api.observability_api:app --host 0.0.0.0 --port 8010 --log-level debug`

Quick checks:

```bash
curl -s http://localhost:8010/health | jq
curl -s "http://localhost:8010/api/task-runs?limit=5" | jq
curl -s "http://localhost:8010/api/task-runs/<TASK_RUN_ID>" | jq
curl -s "http://localhost:8010/api/events?limit=20" | jq
curl -s http://localhost:8010/api/ai-usage/today | jq
curl -s http://localhost:8010/api/system/latest | jq
curl -s http://localhost:8010/api/heartbeats | jq
curl -s "http://localhost:8010/api/heartbeats/stale?stale_after_seconds=180" | jq
curl -s http://localhost:8010/api/summary/today | jq
```

## Task Types (Current)

- Jobs v2 pipeline stages:
  - `jobs_collect_v1` -> multi-source collection and request normalization
  - `jobs_normalize_v1` -> canonicalization + dedupe + drop-reason tracking
  - `jobs_rank_v1` -> deterministic fit scoring + LLM ranking context
  - `jobs_shortlist_v1` -> shortlist selection + future action seeds
  - `jobs_digest_v2` -> digest artifact + optional `notify_v1` enqueue
- `jobs_digest_v1` compatibility shim:
  - accepts legacy payload shape and forwards into `jobs_collect_v1`
  - emits deprecation metadata in result artifact
- `deals_scan_v1`
  - scraping collectors (Best Buy, Newegg, Micro Center) + normalized deal artifact + unicorn-triggered notifications
- `slides_outline_v1`
  - deterministic outline + optional LLM refinement

### jobs_collect_v1 source field support

All default collectors (`linkedin`, `indeed`, `glassdoor`, `handshake`) support the same stage-1 input contract:
- `titles`
- `keywords`
- `excluded_keywords`
- `locations` (multi-location search)
- `work_mode_preference` (`remote` / `hybrid` / `onsite`)
- `minimum_salary`
- `experience_level`
- `result_limit_per_source`
- `enabled_sources` (routing control)

Filter behavior summary:
- query shaping: `titles`, `keywords`, `locations`
- post-collection filtering: `excluded_keywords`, `work_mode_preference`, `minimum_salary`, `experience_level`
- per-source metadata preserved in each job under `source_metadata` (includes source search URL when available)

### Jobs Watcher Preset Configuration (UI + API)

`POST /planner/templates/presets/jobs-digest` accepts both expanded and legacy fields.

Expanded fields:
- `desired_titles`, `keywords`, `excluded_keywords`, `preferred_locations`
- `remote_preference` (`remote` / `hybrid` / `onsite`)
- `minimum_salary`
- `experience_level`
- `enabled_sources`
- `result_limit_per_source`
- `shortlist_count`
- `freshness_preference` (`off` / `prefer_recent` / `strong_prefer_recent`)

Stage impact mapping:
- collection: titles/keywords/excluded keywords/locations/work mode/min salary/experience/sources/result limit
- ranking + shortlist: titles/keywords/locations/work mode/min salary/experience/freshness preference
- digest size: `shortlist_count`

Compatibility/migration notes:
- legacy fields (`desired_title`, `location`, `boards`, `desired_salary_min`, `experience_levels`) are still accepted
- backend normalizes legacy + expanded values into the canonical jobs request (`titles`, `locations`, `enabled_sources`, `shortlist_max_items`, `shortlist_freshness_*`)
- planner templates already saved with legacy payloads continue to execute; editing in the new Workflows Jobs form writes expanded fields

Validation rules:
- sources must be a subset of `linkedin`, `indeed`, `glassdoor`, `handshake`
- work mode values must be `remote`, `hybrid`, or `onsite` (with `on-site` normalized)
- `result_limit_per_source`: integer `1..100`
- `shortlist_count`: integer `1..10`
- `minimum_salary` and `desired_salary_*`: positive numbers, with `max >= min`
- freshness preference must be `off`, `prefer_recent`, or `strong_prefer_recent`

### jobs_normalize_v1 output and dedupe

`jobs_normalize_v1` now produces:
- `jobs_normalized.v1` artifact payload (normalized but pre-dedupe jobs)
- `jobs_deduped.v1` artifact payload (deduped jobs + duplicate groups)
- pipeline result artifact `jobs.normalize.v1` containing both payloads and backward-compatible `normalized_jobs`

Normalized common job shape includes:
- `title`, `company`, `location`, `remote_type`, `salary_min`, `salary_max`, `salary_text`
- `source`, `source_url`, `description_snippet`, `posted_at`, `experience_level`

Dedupe strategy:
- primary key: `company + normalized_title + normalized_location`
- optional fuzzy merge in same company/location bucket
- ambiguous near-matches are recorded under `ambiguous_duplicate_cases` instead of being auto-merged

### jobs_rank_v1 LLM scoring

`jobs_rank_v1` performs structured LLM scoring (when `USE_LLM=true` and `rank_policy.llm_enabled=true`) and writes `jobs_scored.v1` payload data inside the rank artifact.

Per-job structured scores:
- `resume_match_score`
- `title_match_score`
- `salary_score`
- `location_score`
- `seniority_score`
- `overall_score`
- `explanation` / `explanation_summary`

Reliability controls:
- strict JSON output contract with per-batch validation
- per-batch retries for malformed/partial LLM output
- deterministic fallback scoring when runtime LLM is disabled or per-job LLM score is missing

Ranking quality controls:
- anti-repetition penalties (company/title flood control)
- source diversity adjustments
- low-signal explanation penalties

Model and cost tradeoffs:
- cheap tier (`gpt-5-nano`): best for high-volume coarse screening
- standard tier (`gpt-5-mini`): balanced default for daily ranking
- advanced tier (`gpt-5`): higher-quality reasoning for smaller high-priority sets

### jobs_shortlist_v1 top-N selection

`jobs_shortlist_v1` accepts scored jobs from:
- `jobs.rank.v1` (`jobs_scored_artifact.jobs_scored`)
- `jobs_scored.v1` (direct compatibility)

Outputs:
- `jobs_top.v1` payload (embedded in `jobs.shortlist.v1` as `jobs_top_artifact`)
- shortlist summary metadata (`shortlist_summary_metadata`)
- anti-repetition summary and rejection reasons

Selection logic:
- score-first ranking (`overall_score` / `score`)
- source diversity and per-source caps
- duplicate-group repeat suppression
- per-company cap and near-duplicate title suppression within company
- optional freshness weighting (`freshness_weight_enabled`, `freshness_max_bonus`)

### jobs_digest_v2 report generation

`jobs_digest_v2` accepts:
- `jobs.shortlist.v1` (preferred path, reads embedded `jobs_top_artifact`)
- `jobs_top.v1` (direct compatibility path)

Digest generation:
- LLM-heavy structured generation with JSON output contract
- malformed/partial output retries (`digest_policy.llm_max_retries`)
- default retries are speed-oriented (`1`) and can be overridden globally with `JOBS_DIGEST_LLM_MAX_RETRIES_DEFAULT`
- deterministic fallback by default when LLM output remains malformed/empty
- optional strict mode (`digest_policy.strict_llm_output=true`) to fail fast instead

Artifacts produced inside the stage result:
- `jobs_digest.json.v1` (`file_name: jobs_digest.json`) for storage/UI
- `jobs_digest.md.v1` (`file_name: jobs_digest.md`) for readable report preview

Executive summary + per-job digest entry includes:
- pipeline counts (`collected`, `deduped`, `shortlisted`) when available
- strongest hiring patterns and best-fit roles
- rank/title/company/location/salary/source/why-it-fits/tradeoffs

Notification/UI shaping:
- `summary_for_ui` and `notification_seed` are embedded in `jobs_digest.json.v1`
- concise Discord-safe excerpt and top picks are used for `notify_v1`
- `jobs_digest_v2` reuses shared `notify_v1` (no jobs-specific notifier task)
- Discord payload includes headline, shortlist counts, top jobs, and task/run result reference (`/tasks/{task_id}/result` or absolute URL when `artifact_base_url`/`MISSION_CONTROL_API_BASE_URL` is set)
- digest artifacts are written before enqueueing `notify_v1`, so notify failure does not remove completed digest/report artifacts

Add a new task type by:
1. adding `worker/task_handlers/<new_task>_v1.py` with `execute(task, db) -> dict`
2. registering it in `HANDLERS` inside `worker/worker.py`

## Deals Scan v1 Rules

- Collectors:
  - Best Buy, Newegg, Micro Center (scraping-only; no Best Buy developer API dependency)
  - Conservative scrape controls: timeout, retries, rate limiting, short HTML cache TTL
- Normalization:
  - Every collector emits normalized deals (`source`, `title`, `url`, `price`, `old_price`, `discount_pct`, `sku`, `in_stock`, `scraped_at`, `raw`)
- Target filtering before unicorn detection:
  - Keep only RTX 5090 GPU listings and desktop/prebuilt RTX 5090 systems
  - Exclude peripherals/accessories and exclude laptops/notebooks
- Unicorn qualification:
  - Price-only for targets:
    - RTX 5090 GPU: `price <= UNICORN_5090_GPU_MAX_PRICE`
    - RTX 5090 desktop/prebuilt: `price <= UNICORN_5090_PC_MAX_PRICE`
- Notification behavior (Option A):
  - Notify only when `unicorn_count >= 1`
  - Per-item suppression state reduces repeats using:
    - stable key `source+sku` (fallback `source+url`)
    - cooldown window `DEAL_ALERT_COOLDOWN_SECONDS`
    - re-alert overrides for material price/status changes
  - `notify_v1` payload uses:
    - `channels=["discord"]`
    - `include_header=false`
    - `include_metadata=false`
    - deterministic dedupe key `unicorn:<YYYYMMDD-HH>:<hash>` (secondary guard)
    - `dedupe_ttl_seconds` from `NOTIFY_DEDUPE_TTL_SECONDS` (default `21600`)
    - notify task idempotency key derived from `dedupe_key` to avoid duplicate queue rows
  - Message format is concise deal lines (`title`, `price`, `url`)

## Model Catalog v2

Catalog file:
- `api/config/models.json`
- `worker/config/models.json`

Contains:
- `models` (allowed model IDs + pricing per 1M input/output tokens)
- `tiers` (`cheap`, `standard`, `advanced`)
- `routing_rules`

Routing rules currently include:
- minimum remaining budget thresholds for standard/advanced
- payload-size gate forcing cheap tier

You can change model/tier/pricing/rules by editing catalog JSON only.

## Budget and Cost Behavior

- Budget is enforced in two places:
  - API gate before enqueue
  - worker gate before execution
- If blocked, task status becomes `blocked_budget`; run history records failure reason.
- Task retries use `max_attempts`; transient failures are requeued with exponential backoff.
- Permanent exhaustion/failure transitions tasks to `failed_permanent`.
- Cost precision uses `Decimal` and DB `NUMERIC(12,8)` to avoid float drift.

## Observability

Worker logs are structured dictionaries and include for traceability:
- `task_id`
- `run_id`
- `attempt`
- `task_type`
- `chosen_model`

Important events:
- budget precheck / execution gate
- model chosen
- telemetry collected (`tokens_in`, `tokens_out`, `cost_usd`)
- final run status
- OpenAI request IDs are captured when available and included in run completion/error logs.

View logs:

```bash
docker compose logs -f worker
docker compose logs -f api
docker compose logs -f scheduler
```

## Testing

API tests:

```bash
docker compose run --rm --build api pytest -q tests/test_router.py tests/test_main.py tests/test_budget.py
```

Worker tests:

```bash
docker compose run --rm --build worker sh -lc 'pip install --no-cache-dir pytest && pytest -q worker/tests'
```

Deals pipeline focused tests:

```bash
docker compose run --rm --build worker sh -lc 'pip install --no-cache-dir pytest && pytest -q worker/tests/test_target_filter.py worker/tests/test_deals_scan_unicorn_notify.py worker/tests/test_deals_scrape_collectors.py'
```

Quick target-filter dry-run:

```bash
docker compose run --rm --build worker sh -lc 'python -c "from task_handlers.deals_scan_v1 import filter_target_items; deals=[{\"title\":\"RTX 5090 Graphics Card\"},{\"title\":\"Gaming Laptop RTX 5090\"},{\"title\":\"RTX 5090 Water Block\"},{\"title\":\"Gaming Desktop PC with RTX 5090\"}]; kept=filter_target_items(deals); print(\"input=\",len(deals),\"kept=\",len(kept),\"titles=\",[d[\"title\"] for d in kept])"'
```

Manual deal scan + unicorn notify test:

```bash
# 1) Restart worker after any env changes
docker compose up -d --build worker

# 2) Set API key header if API_KEY is configured
API_KEY=$(grep "^API_KEY=" .env | cut -d= -f2-)

# 3) Trigger a scraping scan
curl -s -X POST http://localhost:8000/tasks \
  -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "task_type":"deals_scan_v1",
    "payload_json":"{\"source\":\"manual-scrape\",\"collectors_enabled\":true}"
  }' | jq

# 4) Inspect recent tasks + latest artifact
curl -s -H "X-API-Key: $API_KEY" "http://localhost:8000/tasks?limit=20" | jq

# 5) Worker logs show unicorn decision details (deals_count, unicorn_count, alertable_unicorn_count, notify_enqueued)
docker compose logs --no-color --tail=160 worker
```

Create recurring scrape schedule (every 15 minutes):

```bash
API_KEY=$(grep "^API_KEY=" .env | cut -d= -f2-)

curl -s -X POST http://localhost:8000/schedules \
  -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "task_type":"deals_scan_v1",
    "payload_json":"{\"source\":\"scheduled-scrape\",\"collectors_enabled\":true}",
    "cron":"*/15 * * * *",
    "enabled":true,
    "max_attempts":3
  }' | jq

curl -s -H "X-API-Key: $API_KEY" "http://localhost:8000/schedules" | jq
```

## Troubleshooting

- Worker not processing tasks:
  - check `docker compose logs -f worker`
  - verify Redis is up: `docker compose ps redis`
- Model override rejected:
  - call uses model not listed in catalog
- Result endpoint returns 404:
  - task has not produced an artifact yet
- Budget blocks unexpectedly:
  - inspect `/stats/today` and worker budget log events
- OpenAI errors:
  - validate `OPENAI_API_KEY`
  - optionally set `USE_LLM=false` for simulated mode

## Security Note

Compose now binds exposed service ports to localhost only (`127.0.0.1`) for safer defaults.
For remote access, use SSH forwarding or Tailscale rather than public exposure.
