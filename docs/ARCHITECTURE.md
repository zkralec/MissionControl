# Mission Control Architecture

## Purpose
Mission Control is the orchestrator. External systems plug into Mission Control through stable contracts (events in, tasks out, artifacts persisted).

## Core Roles

### Scrapers (producers)
Scrapers pull raw data from external sources and emit normalized events.

Responsibilities:
- Poll/fetch source systems (deals, jobs, school systems, etc.).
- Normalize raw records into schema-defined payloads.
- Emit idempotent events for downstream handlers.
- Avoid business ranking/summarization logic in scraper code.

### Handlers (Mission Control tasks)
Handlers are Mission Control tasks that enrich, rank, summarize, notify, and store artifacts.

Responsibilities:
- Accept normalized payloads.
- Apply task-specific logic (enrichment, ranking, drafting, notification prep).
- Persist artifacts for traceability and UX consumption.
- Return deterministic outcomes for retry policy.

## North-Star Task Types

| Task Type | Role in Pipeline | Primary Output |
|---|---|---|
| `unicorn_deals_poll_v1` | Poll deals sources and normalize events | Raw/normalized deals artifact |
| `unicorn_deals_rank_v1` | Score and rank candidate deals | Ranked deals artifact |
| `jobs_collect_v1` | Collect jobs from multiple boards/manual sources | Jobs collect artifact |
| `jobs_normalize_v1` | Canonicalize and dedupe collected jobs | Jobs normalize artifact |
| `jobs_rank_v1` | Deterministically score + LLM-rank jobs | Jobs rank artifact |
| `jobs_shortlist_v1` | Apply shortlist policy and future-action seeds | Jobs shortlist artifact |
| `jobs_digest_v2` | Build digest output and notify decision | Jobs digest artifact |
| `school_agenda_sync_v1` | Sync school agenda/calendar/events | Agenda sync artifact |
| `essay_draft_v1` | Generate structured draft content | Essay draft artifact |
| `notify_v1` | Shared notification dispatch task | Notification delivery artifact |

## Repository Structure

```text
mission-control/
  tasks/           # Mission Control task handlers
  integrations/    # Source and service adapters (BestBuy/Newegg, job boards, Google)
  notifications/   # Notification channel adapters (Discord/SMS)
  schemas/         # Payload/artifact JSON schema definitions
  docs/            # Architecture and contracts
```

Notes:
- Existing implementation paths can be migrated incrementally to this structure.
- New task/integration work should default to this structure.
