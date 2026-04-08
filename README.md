# Mission Control

Mission Control is a local-first AI task orchestration system built to run continuously on a mini-PC or server.

Always-on mini-PC deployment details live in [docs/MINI_PC_ALWAYS_ON_DEPLOYMENT.md](docs/MINI_PC_ALWAYS_ON_DEPLOYMENT.md).

It includes:
- a FastAPI API
- a durable RQ worker
- a scheduler for recurring and retry-due work
- Postgres for tasks, runs, and artifacts
- Redis for queueing
- a React operator UI

The current first-class workflows are:
- Jobs v2 search, ranking, shortlist, digest, and notification
- Deals scanning and notification
- Planner-driven recurring workflows
- Runs, Alerts, and Observability views for debugging and operations

## How It Runs

Mission Control is meant to stay up on the mini-PC even when your laptop disconnects.

Durable services:
- `api`
- `worker`
- `scheduler`
- `postgres`
- `redis`
- `adminer` (optional database UI, disabled by default unless you use the `ops` Compose profile)

Recommended run mode:

```bash
scripts/ops/mission-control.sh start
```

That starts everything in detached mode with `restart: unless-stopped`.

## Quick Start

### 1. Prerequisites

- Docker + Docker Compose
- Node.js 20+ if you want to run the frontend dev server
- an OpenAI API key if you want real LLM execution

### 2. Create `.env`

Create a `.env` file in the repo root.

Minimum useful local setup:

```env
POSTGRES_USER=mission
POSTGRES_PASSWORD=mission
POSTGRES_DB=mission_control

DATABASE_URL=postgresql+psycopg://mission:mission@postgres:5432/mission_control
REDIS_URL=redis://redis:6379/0

API_KEY=replace-with-a-strong-key
OPENAI_API_KEY=sk-...
USE_LLM=true

DAILY_BUDGET_USD=1.00
BUDGET_BUFFER_USD=0.02
MISSION_CONTROL_DAY_BOUNDARY_TZ=America/New_York

SCRAPE_TIMEOUT_SECONDS=15
SCRAPE_RATE_LIMIT_SECONDS=2
SCRAPE_RETRY_ATTEMPTS=3

NOTIFY_DEDUPE_TTL_SECONDS=21600
NOTIFY_DISCORD_ALLOWLIST=deals_scan_v1,unicorn_deals_poll_v1,unicorn_deals_rank_v1,jobs_digest_v2,ops_report_v1
NOTIFY_DEV_MODE=false
```

Important notes:
- `DATABASE_URL` and `REDIS_URL` should use the Docker service hostnames `postgres` and `redis`.
- Set `USE_LLM=false` if you want the pipeline to run without OpenAI calls.
- If you override `NOTIFY_DISCORD_ALLOWLIST`, keep `jobs_digest_v2` included or Jobs digests will not flow through `notify_v1`.
- The compose stack mounts `./data` into the containers and stores the SQLite telemetry/history files there.

### 3. Start the stack

```bash
scripts/ops/mission-control.sh start
scripts/ops/mission-control.sh ps
```

### 4. Confirm the core services are healthy

```bash
scripts/ops/mission-control.sh health
```

## Open the UI

Mission Control now has two ways to use the UI:

- recommended for the always-on deployment: the built React app served by the API at `/app/`
- optional for frontend development: the Vite dev server on port `5173`

### Recommended: built UI served by the API

Open:

- local on the mini-PC: `http://localhost:8000/app/`
- legacy pages: `http://localhost:8000/legacy` and `http://localhost:8000/legacy/observability`
- Swagger docs: `http://localhost:8000/docs`

### If you connect from your laptop over SSH

The Docker services bind to `127.0.0.1`, so your laptop needs SSH port forwarding.

Recommended SSH command:

```bash
ssh -L 8000:127.0.0.1:8000 -L 8080:127.0.0.1:8080 your_user@mini-pc
```

Then open on your laptop:

- Mission Control UI: `http://localhost:8000/app/`
- Adminer: `http://localhost:8080/`

This is the simplest and most reliable way to use Mission Control from a laptop.

If you want browser access over Tailscale without keeping an SSH session open, configure Tailscale Serve or another tailnet-only forwarding layer on the mini-PC that proxies to `127.0.0.1:8000`.

### If you use VS Code Remote SSH and want the direct numeric URL

If you normally SSH into the mini-PC from VS Code and then `cmd` + click the link that Vite prints in the terminal, use the Vite dev server with a public bind address:

```bash
cd frontend
npm run dev -- --host 0.0.0.0 --port 5173 --strictPort
```

Vite will print links like:

- `http://192.168.18.210:5173/`
- `http://100.110.193.90:5173/`

In this setup, the direct numeric URL is the right one to open from your laptop browser.

For this machine, the Tailscale address is usually the easiest:

- `http://100.110.193.90:5173/`

Why this works:
- `--host 0.0.0.0` makes the dev server reachable from outside the mini-PC
- `--strictPort` prevents Vite from silently switching to `5174`, `5175`, and so on
- the numeric URL is often easier than relying on `localhost` forwarding when you are already working through VS Code Remote SSH

## Frontend Development

Only use this when you are actively working on the React frontend.

### Start the backend first

```bash
docker compose up -d api worker scheduler redis postgres
```

### Run the Vite dev server

On the mini-PC:

```bash
cd frontend
npm install
npm run dev -- --host 0.0.0.0 --port 5173
```

Recommended improvement:

```bash
npm run dev -- --host 0.0.0.0 --port 5173 --strictPort
```

This is the best choice if you want the terminal to print one stable clickable numeric URL.

If you are connecting from your laptop and want localhost-style forwarding instead, start SSH with both forwards:

```bash
ssh -L 8000:127.0.0.1:8000 -L 5173:127.0.0.1:5173 your_user@mini-pc
```

Then open on your laptop:

`http://localhost:5173/`

Notes:
- The frontend dev server proxies API calls to `http://localhost:8000`.
- If the tab appears to load forever, it is usually because the port was not forwarded, Vite was not started with `--host 0.0.0.0`, or Vite moved to a different port because `5173` was already in use.
- If you prefer the direct-IP method, open the numeric `Network:` URL Vite prints in the terminal instead of `localhost`.

Useful frontend commands:

```bash
cd frontend
npm run build
npm run test
npm run generate:openapi
```

## Daily Operator Flow

The normal UI path is:

1. `Workflows`
   Configure or review recurring automations, especially the Jobs watcher.
2. `Runs`
   Inspect stage-by-stage execution artifacts for collect, normalize, rank, shortlist, digest, and notify, with source focus on LinkedIn and Indeed.
3. `Alerts`
   Review grouped failures, intentional notify skips, weak LinkedIn/Indeed coverage, and direct next actions.
4. `Observability`
   Check API, worker, scheduler, Redis, heartbeat, and runtime health signals.

The Jobs watcher is designed to be operated from the UI without hand-editing JSON.

## Common Commands

### Start or rebuild everything

```bash
scripts/ops/mission-control.sh start
```

### Restart one service

```bash
scripts/ops/mission-control.sh restart
```

### Follow logs

```bash
scripts/ops/mission-control.sh logs api
scripts/ops/mission-control.sh logs worker
scripts/ops/mission-control.sh logs scheduler
```

### Stop everything

```bash
scripts/ops/mission-control.sh stop
```

### Verify runtime health

```bash
scripts/ops/mission-control.sh health
```

## API Quickstart

Read the API key from `.env`:

```bash
API_KEY=$(grep "^API_KEY=" .env | cut -d= -f2-)
```

### Create a Jobs v2 collection task

```bash
curl -s -X POST http://localhost:8000/tasks \
  -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "task_type": "jobs_collect_v1",
    "payload_json": "{\"request\":{\"collectors_enabled\":true,\"profile_mode\":\"resume_profile\",\"sources\":[\"linkedin\",\"indeed\"],\"titles\":[\"Machine Learning Engineer\"],\"desired_title_keywords\":[\"machine learning engineer\",\"ai engineer\"],\"keywords\":[\"python\",\"llm\"],\"excluded_keywords\":[\"staff\"],\"locations\":[\"Remote\",\"New York, NY\"],\"work_modes\":[\"remote\",\"hybrid\"],\"desired_salary_min\":160000,\"experience_levels\":[\"entry\",\"mid\",\"senior\"],\"result_limit_per_source\":120,\"minimum_raw_jobs_total\":120,\"minimum_unique_jobs_total\":80,\"minimum_jobs_per_source\":25,\"stop_when_minimum_reached\":true,\"collection_time_cap_seconds\":120,\"max_queries_per_run\":12,\"shortlist_count\":5,\"jobs_notification_cooldown_days\":3,\"resurface_seen_jobs\":true}}",
    "model": null
  }' | jq
```

### List tasks

```bash
curl -s -H "X-API-Key: $API_KEY" http://localhost:8000/tasks | jq
```

### Get task runs

```bash
curl -s -H "X-API-Key: $API_KEY" http://localhost:8000/tasks/<TASK_ID>/runs | jq
```

### Get the latest result artifact

```bash
curl -s -H "X-API-Key: $API_KEY" http://localhost:8000/tasks/<TASK_ID>/result | jq
```

### Get today’s stats

```bash
curl -s -H "X-API-Key: $API_KEY" http://localhost:8000/stats/today | jq
```

## Useful URLs

When running locally on the mini-PC:

- React operator UI: `http://localhost:8000/app/`
- legacy UI: `http://localhost:8000/legacy`
- legacy observability: `http://localhost:8000/legacy/observability`
- Swagger: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`
- Adminer: `http://localhost:8080/`

## Services and Architecture

Docker Compose services:
- `api`: FastAPI server
- `worker`: RQ worker execution runtime
- `scheduler`: recurring and retry-due task creation
- `postgres`: primary relational store
- `redis`: queue backend
- `adminer`: optional database browser when you start with `COMPOSE_PROFILES=ops`

High-level flow:
1. A client or watcher creates a task with `POST /tasks`.
2. The API validates auth, budget, and payload shape.
3. The API stores the task and enqueues it in Redis.
4. The worker executes the handler and writes runs, artifacts, telemetry, and followup tasks.
5. The scheduler handles recurring schedules, retry-due tasks, and planner-related loops.

## Repository Layout

- `api/main.py` - FastAPI app, routes, models, and frontend serving under `/app`
- `api/scheduler.py` - scheduler loop
- `api/router.py` - model routing
- `api/models/catalog.py` - model catalog helpers
- `worker/worker.py` - worker runtime and task execution
- `worker/task_handlers/` - task handlers
- `worker/llm/openai_adapter.py` - OpenAI adapter
- `integrations/` - external collectors and scrapers
- `frontend/` - React + Vite operator UI
- `migrations/` - SQL migration files
- `examples/` - verification scripts
- `docker-compose.yml` - local orchestration

## Jobs v2 Notes

Jobs v2 is now a full pipeline:

1. `jobs_collect_v1`
2. optional `openclaw_jobs_collect_v1` for bounded Handshake/Glassdoor browser collection
3. `jobs_normalize_v1`
4. `jobs_rank_v1`
5. `jobs_shortlist_v1`
6. `jobs_digest_v2`
7. optional `notify_v1`

Application-prep phase 2 layers onto shortlisted jobs without replacing the pipeline:

0. optional `job_apply_manual_seed_v1` for one-off manual application seeds
1. `job_apply_prepare_v1`
2. `resume_tailor_v1`
3. `openclaw_apply_draft_v1`
4. optional `notify_v1`

Important behavior:
- collection is intentionally broad and can run multiple queries per source
- OpenClaw is an optional bounded collect stage for `handshake` and `glassdoor`; it is off by default and requires `OPENCLAW_ENABLED=true` plus `OPENCLAW_COLLECTOR_COMMAND`
- OpenClaw application handling is draft-only in phase 2; it requires `OPENCLAW_APPLY_DRAFT_ENABLED=true` plus `OPENCLAW_APPLY_DRAFT_COMMAND`
- `job_apply_manual_seed_v1` creates a first-class manual entry path for one-off application testing and preserves lineage as `manual_api/manual_seed`
- `openclaw_apply_draft_v1` stops before final submission, records review artifacts, and queues review notification only when a draft is ready
- the planner and watcher presets do not auto-submit applications
- dedupe happens within a run, not as permanent cross-run suppression
- previously seen but non-winning jobs can resurface in later runs
- recently notified jobs can be cooled down to reduce spam
- Runs and Alerts now expose jobs-specific observability and next actions

### OpenClaw Draft Runner

Mission Control invokes the browser draft stage through `worker/task_handlers/openclaw_apply_draft_v1.py`, which calls `integrations/openclaw_apply_draft.py`, which shells out to `scripts/openclaw_apply_draft.py` using `OPENCLAW_APPLY_DRAFT_COMMAND`.

Required env vars:
- `OPENCLAW_APPLY_DRAFT_ENABLED=true`
- `OPENCLAW_APPLY_DRAFT_COMMAND="python3 scripts/openclaw_apply_draft.py"`
- One adapter path:
- `OPENCLAW_APPLY_TOOL_COMMAND="python3 /app/scripts/openclaw_apply_tool_bridge.py"`
- and `OPENCLAW_APPLY_BROWSER_COMMAND="python /app/scripts/openclaw_apply_browser_backend.py"`
- or `OPENCLAW_APPLY_PYTHON_ENTRYPOINT="openclaw:run_apply_draft"`

Optional runner env vars:
- `OPENCLAW_APPLY_ADAPTER=auto|command|python`
- `OPENCLAW_APPLY_HEADLESS=true|false`
- `OPENCLAW_APPLY_INSPECT_ONLY=true|false`
- `OPENCLAW_APPLY_BROWSER_ATTACH_MODE=true|false`
- `OPENCLAW_APPLY_SKIP_BROWSER_START=true|false`
- `OPENCLAW_APPLY_ALLOW_BROWSER_START=true|false`
- `OPENCLAW_APPLY_RUN_ON_HOST=true|false`
- `OPENCLAW_APPLY_GATEWAY_URL=ws://host.docker.internal:18789`
- `OPENCLAW_APPLY_GATEWAY_TOKEN=<gateway-token>`
- `OPENCLAW_APPLY_CDP_URL=http://host.docker.internal:9222`
- `OPENCLAW_APPLY_HOST_GATEWAY_URL=ws://127.0.0.1:18789`
- `OPENCLAW_APPLY_HOST_CDP_URL=http://127.0.0.1:18800`
- `OPENCLAW_APPLY_HOST_GATEWAY_ALIAS=host.docker.internal`
- `OPENCLAW_APPLY_SCREENSHOT_DIR=./data/openclaw_apply_drafts/screenshots`
- `OPENCLAW_APPLY_RECEIPT_DIR=./data/openclaw_apply_drafts/receipts`
- `OPENCLAW_APPLY_RESUME_DIR=./data/openclaw_apply_drafts/resume_uploads`
- `OPENCLAW_APPLY_ALLOWED_RESUME_EXTENSIONS=.pdf,.doc,.docx,.txt,.rtf`
- `OPENCLAW_APPLY_TIMEOUT_SECONDS=240`
- `OPENCLAW_APPLY_MAX_STEPS=24`
- `OPENCLAW_APPLY_LOG_LEVEL=INFO`
- `OPENCLAW_APPLY_AUTH_STRATEGY=storage_state|existing_session|browser_profile`
- `OPENCLAW_APPLY_STORAGE_STATE_PATH=/absolute/path/to/storage-state.json`
- `OPENCLAW_APPLY_BROWSER_PROFILE_PATH=/absolute/path/to/browser-profile`
- `APPLICATION_DRAFT_STATE_DB_PATH=./data/task_run_history.sqlite3`
- `OPENCLAW_BROWSER_BASE_COMMAND="/opt/openclaw/npm-global/bin/openclaw browser --url ws://host.docker.internal:18789 --token <gateway-token>"`

Docker/local wiring in this repo:
- the `worker` container now includes a Node runtime
- the `worker` container mounts `${HOME}/.npm-global` at `/opt/openclaw/npm-global`
- the `worker` container mounts `${HOME}/.openclaw` at `/root/.openclaw`
- the `worker` container maps `host.docker.internal` to the Docker host so it can reach a host-run OpenClaw gateway

### Manual Application Draft API

Use `POST /applications/manual-drafts` to seed a one-off application without any shortlist upstream artifact. The API creates a `job_apply_manual_seed_v1` task, preserves lineage as `manual_api/manual_seed`, and then chains:

1. `job_apply_prepare_v1`
2. `resume_tailor_v1`
3. `openclaw_apply_draft_v1`

Exact `curl` example for a LinkedIn Easy Apply draft:

```bash
curl -X POST "http://localhost:8000/applications/manual-drafts" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Software Engineer, AI",
    "company": "Example AI",
    "source": "linkedin",
    "source_url": "https://www.linkedin.com/jobs/view/1234567890/",
    "application_url": "https://www.linkedin.com/jobs/view/1234567890/",
    "job_id": "1234567890",
    "normalized_job_id": "linkedin-1234567890",
    "request": {
      "profile_mode": "resume_profile",
      "notify_channels": ["discord"],
      "openclaw_apply_enabled": true
    }
  }'
```

Why the older manual workaround reused the wrong shortlist job:
- `POST /applications/drafts` stores a `selected_job` copy in the payload, but `job_apply_prepare_v1` historically resolved the actual job from the upstream shortlist artifact instead of trusting that payload copy.
- If the injected manual job was not present in the shortlist artifact, the prepare stage could fall back to the shortlisted job selected by `job_id`, `shortlist_index`, or the first shortlist row.
- That wrong `application_target` then flowed into downstream draft dedupe, so duplicate protection could fire for the shortlist job instead of the manual one.
- the bridge command in `OPENCLAW_APPLY_TOOL_COMMAND` gives Mission Control a stable invocation path even if the exact OpenClaw browser command changes
- `OPENCLAW_APPLY_BROWSER_COMMAND` should point at the repo-local backend script, which translates Mission Control's draft payload into conservative `openclaw browser ...` operations
- the repo-local backend is `scripts/openclaw_apply_browser_backend.py`
- `OPENCLAW_APPLY_TOOL_COMMAND` is the apply-draft adapter command, not the raw browser CLI; in normal command mode it should be the repo-local bridge (`scripts/openclaw_apply_tool_bridge.py`)
- if a host-run shell accidentally points `OPENCLAW_APPLY_TOOL_COMMAND` at `openclaw browser ...`, the runner now reinterprets that as `OPENCLAW_BROWSER_BASE_COMMAND`, routes execution through the bridge/backend, and still appends real browser subcommands stage by stage
- `OPENCLAW_BROWSER_BASE_COMMAND` is how the backend reaches the host OpenClaw gateway from inside the worker container
- `OPENCLAW_BROWSER_BASE_COMMAND` is only a base command prefix; the runner/backend appends browser subcommands like `status`, `tabs`, `open`, `snapshot`, and `screenshot`
- `OPENCLAW_BROWSER_BASE_COMMAND` must keep browser-scoped flags after `browser`, for example `openclaw browser --url ... --token ... --browser-profile openclaw`
- when `OPENCLAW_APPLY_BROWSER_ATTACH_MODE=true`, the backend probes the already-running browser with `status` and `tabs` before navigation and skips `browser start` unless `OPENCLAW_APPLY_ALLOW_BROWSER_START=true`
- `OPENCLAW_APPLY_GATEWAY_URL` and `OPENCLAW_APPLY_CDP_URL` are normalized to `host.docker.internal` when the worker is running in Docker and the configured host is `127.0.0.1` or `localhost`
- `OPENCLAW_APPLY_RUN_ON_HOST=true` switches the runner payload to host-local OpenClaw URLs (`127.0.0.1`) and is intended for direct host execution of `scripts/openclaw_apply_draft.py`
- if `OPENCLAW_APPLY_RUN_ON_HOST=true` is requested from the Docker worker, Mission Control now writes a host handoff payload plus the exact host command to run into `debug_json.host_handoff`
- in successful host mode runs, `debug_json.openclaw_commands` lists each executed browser step with `stage`, full argv, exit code, stdout, and stderr

Safety rules enforced by the runner:
- it always sends `submit=false` and `stop_before_submit=true`
- it blocks any adapter submit signal as `unsafe_submit_attempted`
- it never returns `submitted=true`
- it only reports `awaiting_review=true` when `draft_status` shows meaningful draft progress and `fields_filled_manifest`, `screenshot_metadata_references`, and `checkpoint_urls` are all non-empty
- it suppresses notifications unless `awaiting_review=true`, `submitted=false`, and screenshots are present
- it prevents repeat drafting of the same application identity unless `request.force_redraft=true` or `draft_policy.force_redraft=true`

Expected success criteria:
- `draft_status` is `draft_ready` or `partial_draft`
- `awaiting_review=true`
- `submitted=false`
- `fields_filled_manifest` is non-empty
- `screenshot_metadata_references` is non-empty
- `checkpoint_urls` is non-empty
- `notify_decision.should_notify=true`

Deterministic failure modes include:
- `login_required`
- `captcha_or_bot_challenge`
- `anti_bot_blocked`
- `session_expired`
- `unsupported_form`
- `upload_failed`
- `redirected_off_target`
- `timed_out`
- `manual_review_required`
- `unsafe_submit_attempted`

Inspect-only mode:
- set `OPENCLAW_APPLY_INSPECT_ONLY=true` for a debugging run that opens the page, captures screenshots, and returns page/form diagnostics without filling fields
- or pass `request.openclaw_apply_inspect_only=true` in the `openclaw_apply_draft_v1` task payload
- inspect-only runs are intentionally not review-ready and should not notify

Example local invocation:

```bash
export OPENCLAW_APPLY_DRAFT_ENABLED=true
export OPENCLAW_APPLY_DRAFT_COMMAND="python3 scripts/openclaw_apply_draft.py"
export OPENCLAW_APPLY_TOOL_COMMAND="python3 scripts/openclaw_apply_tool_bridge.py"
export OPENCLAW_APPLY_BROWSER_COMMAND="python /app/scripts/openclaw_apply_browser_backend.py"
export OPENCLAW_APPLY_BROWSER_ATTACH_MODE=true
export OPENCLAW_APPLY_SKIP_BROWSER_START=true
export OPENCLAW_APPLY_ALLOW_BROWSER_START=false
export OPENCLAW_APPLY_RUN_ON_HOST=false
export OPENCLAW_APPLY_GATEWAY_URL="ws://host.docker.internal:18789"
export OPENCLAW_APPLY_GATEWAY_TOKEN="<gateway-token>"
export OPENCLAW_APPLY_CDP_URL="http://host.docker.internal:9222"
export OPENCLAW_BROWSER_BASE_COMMAND="/opt/openclaw/npm-global/bin/openclaw browser --url ws://host.docker.internal:18789 --token <gateway-token> --browser-profile openclaw"
python3 scripts/openclaw_apply_draft.py --input-json-file /tmp/openclaw-apply-payload.json
```

Inspect-only local invocation:

```bash
export OPENCLAW_APPLY_DRAFT_ENABLED=true
export OPENCLAW_APPLY_DRAFT_COMMAND="python3 scripts/openclaw_apply_draft.py"
export OPENCLAW_APPLY_TOOL_COMMAND="python3 scripts/openclaw_apply_tool_bridge.py"
export OPENCLAW_APPLY_BROWSER_COMMAND="python /app/scripts/openclaw_apply_browser_backend.py"
export OPENCLAW_APPLY_BROWSER_ATTACH_MODE=true
export OPENCLAW_APPLY_SKIP_BROWSER_START=true
export OPENCLAW_APPLY_ALLOW_BROWSER_START=false
export OPENCLAW_APPLY_RUN_ON_HOST=false
export OPENCLAW_APPLY_GATEWAY_URL="ws://host.docker.internal:18789"
export OPENCLAW_APPLY_GATEWAY_TOKEN="<gateway-token>"
export OPENCLAW_APPLY_CDP_URL="http://host.docker.internal:9222"
export OPENCLAW_BROWSER_BASE_COMMAND="/opt/openclaw/npm-global/bin/openclaw browser --url ws://host.docker.internal:18789 --token <gateway-token> --browser-profile openclaw"
export OPENCLAW_APPLY_INSPECT_ONLY=true
python3 scripts/openclaw_apply_draft.py --input-json-file /tmp/openclaw-apply-payload.json
```

Local test commands:

```bash
docker compose up -d --build worker
docker compose exec worker python -c "import shutil; print(shutil.which('openclaw'))"
printf '%s\n' '{"application_target":{"application_url":"https://jobs.example/apply/1"},"resume_variant":{},"artifacts":{"screenshot_dir":"/tmp/openclaw-smoke","run_key":"smoke-run"},"constraints":{"submit":false,"stop_before_submit":true,"timeout_seconds":5},"submit":false,"stop_before_submit":true}' | docker compose exec -T worker python /app/scripts/openclaw_apply_tool_bridge.py
pytest worker/tests/test_openclaw_apply_browser_backend.py
pytest worker/tests/test_openclaw_apply_draft_runner.py
pytest worker/tests/test_job_application_phase2.py -k openclaw_apply_draft
```

Expected worker smoke-check results:
- if the CLI mount/runtime is healthy but the OpenClaw browser gateway is not ready, the bridge should return `failure_category=manual_review_required` with `blocking_reason` explaining that the gateway is not paired or reachable
- once the gateway is ready, the same path should return either an inspect/draft result or a board-specific blocked result, but not `tool_unavailable`
- `debug_json.runner_debug` now includes the exact stdout, stderr, exit code, stage, and failure kind for each `openclaw browser` subprocess invocation

Host-run mode for loopback-only OpenClaw services:
- use this when OpenClaw is bound only to `127.0.0.1` on the mini-PC host and the Docker worker cannot reach it
- set `OPENCLAW_APPLY_RUN_ON_HOST=true`
- set `OPENCLAW_APPLY_HOST_GATEWAY_URL=ws://127.0.0.1:18789`
- set `OPENCLAW_APPLY_HOST_CDP_URL=http://127.0.0.1:18800`
- if you want the Docker worker to emit host-visible handoff files, set `OPENCLAW_APPLY_RECEIPT_DIR=/data/openclaw_apply_drafts/receipts` in the worker env so those files land in the bind-mounted `./data` tree
- set `OPENCLAW_BROWSER_BASE_COMMAND="openclaw browser --url ws://127.0.0.1:18789 --token <gateway-token> --browser-profile openclaw"` in the host shell that will run the draft runner
- when the Docker worker receives a draft request in host mode, it writes a host handoff request under the receipt root and returns the exact host command in `debug_json.host_handoff.runner_command`
- the host handoff payload carries `browser.run_on_host=true`, so the host-side `scripts/openclaw_apply_draft.py` path now normalizes host mode before adapter resolution and records every executed browser step under `debug_json.openclaw_commands`

Exact host-run test commands:

```bash
export OPENCLAW_APPLY_RUN_ON_HOST=true
export OPENCLAW_APPLY_HOST_GATEWAY_URL="ws://127.0.0.1:18789"
export OPENCLAW_APPLY_HOST_CDP_URL="http://127.0.0.1:18800"
export OPENCLAW_BROWSER_BASE_COMMAND="openclaw browser --url ws://127.0.0.1:18789 --token <gateway-token> --browser-profile openclaw"
export OPENCLAW_APPLY_RECEIPT_DIR="$PWD/data/openclaw_apply_drafts/receipts"
export OPENCLAW_APPLY_SCREENSHOT_DIR="$PWD/data/openclaw_apply_drafts/screenshots"
export OPENCLAW_APPLY_RESUME_DIR="$PWD/data/openclaw_apply_drafts/resume_uploads"

REQUEST_FILE="$(ls -t data/openclaw_apply_drafts/receipts/host_handoff/*.input.json | head -n1)"
RESULT_FILE="data/openclaw_apply_drafts/receipts/host_handoff/$(basename "$REQUEST_FILE" .input.json).result.json"
python3 scripts/openclaw_apply_draft.py --input-json-file "$REQUEST_FILE" > "$RESULT_FILE"
python3 -m json.tool "$RESULT_FILE" | sed -n '1,120p'
```

To inspect the Docker-side handoff/debug artifact after triggering a task:

```bash
python3 -m json.tool "$(ls -t data/openclaw_apply_drafts/receipts/*.json | head -n1)" | sed -n '1,200p'
```

Host gateway setup for container access:
1. stop any existing supervised gateway if you need to change its bind/auth mode
2. run the host gateway with a non-loopback bind plus token auth, for example:
   `openclaw gateway run --bind lan --auth token --token <gateway-token>`
3. set `OPENCLAW_BROWSER_BASE_COMMAND` in Mission Control to use `openclaw browser --url ws://host.docker.internal:18789` with the same token and optional `--browser-profile openclaw`
4. rebuild the worker so the updated host alias/env values are present

How to verify a run in Mission Control:
- open the `openclaw_apply_draft_v1` task result in Runs
- confirm `submitted` is `false`
- confirm `review_status` is `awaiting_review` only when screenshots, checkpoints, and filled fields are present
- inspect `failure_category`, `blocking_reason`, `warnings`, and `errors` for blocked runs
- use `page_diagnostics` and `form_diagnostics` when debugging inspect-only or unsupported boards

## Data and Observability Storage

Mission Control uses both Postgres and SQLite-backed operational stores.

Postgres:
- tasks
- runs
- artifacts
- schedules

SQLite-backed operational data in `./data/task_run_history.sqlite3` by default:
- task run history
- AI usage logs
- event log
- system metrics
- agent heartbeats
- candidate resume profile
- daily ops reports
- deal alert dedupe state

Helpful verification scripts:

```bash
python examples/verify_task_run_history.py
python examples/verify_ai_usage_logging.py
python examples/print_recent_events.py
python examples/verify_system_metrics.py
python examples/verify_agent_heartbeats.py
python examples/verify_resume_profile.py
python examples/generate_daily_ops_report.py
python examples/verify_autonomous_planner.py
python examples/verify_planner_controls.py
```

## Database Migrations

For a fresh local database, startup will create the current tables automatically.

For an existing database, apply the SQL files in `migrations/` in order:

```bash
docker compose up -d postgres
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < migrations/20260225_add_artifacts_table.sql
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < migrations/20260225_cost_precision_numeric.sql
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < migrations/20260225_reliability_scheduler_auth.sql
```

## Endpoints

Core:
- `POST /tasks`
- `GET /tasks`
- `GET /tasks/{task_id}`
- `GET /tasks/{task_id}/runs`
- `GET /tasks/{task_id}/result`
- `GET /runs`
- `POST /schedules`
- `GET /schedules`

Health and telemetry:
- `GET /health`
- `GET /ready`
- `GET /metrics`
- `GET /stats/today`
- `GET /telemetry/events`
- `GET /telemetry/ai-usage`
- `GET /telemetry/ai-usage/today`
- `GET /telemetry/ai-usage/summary`
- `GET /telemetry/runtime-status`

Resume profile:
- `GET /profile/resume?include_text=true|false`
- `POST /profile/resume/upload`
- `PUT /profile/resume`
- `DELETE /profile/resume`

## Troubleshooting

### The UI keeps loading forever

Most common causes:
- the backend stack is not running
- you opened `localhost` on your laptop without SSH port forwarding
- you are using Vite dev mode without forwarding port `5173`
- Vite was started without `--host 0.0.0.0`
- Vite silently moved to another port because `5173` was already in use

### The built UI works, but the Vite dev UI does not

That usually means the backend is fine and the problem is only the dev-server access path. Use:

```bash
ssh -L 8000:127.0.0.1:8000 -L 5173:127.0.0.1:5173 your_user@mini-pc
cd frontend
npm run dev -- --host 0.0.0.0 --port 5173
```

If you use VS Code Remote SSH and the numeric URL works better for you, use this instead:

```bash
cd frontend
npm run dev -- --host 0.0.0.0 --port 5173 --strictPort
```

Then open the exact `Network:` URL Vite prints, for example:

```text
http://100.110.193.90:5173/
```

If `5173` is already in use, clear old Vite processes first:

```bash
pkill -f vite
```

### Tasks should continue when my laptop disconnects

Yes. The durable path is the Docker Compose stack on the mini-PC. Your laptop browser, SSH session, and Vite dev server are not required for already-created tasks to continue running.
