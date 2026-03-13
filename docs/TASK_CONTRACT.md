# Task Contract

This contract applies to every Mission Control task handler.

## Required Behavior

1. Validate payload
- Validate input payload against a schema before task logic runs.
- On validation failure, return a deterministic non-retryable error.

2. Write at least one artifact
- Every successful handler run must persist at least one artifact.
- `content_json` is the preferred artifact format.
- Artifacts should be traceable to `task_type`, `run_id`, and input identity.

3. Never print secrets
- Never log raw API keys, tokens, credentials, or full authorization headers.
- Redact sensitive fields in logs and errors.

4. Return deterministic error types for retry policy
- Errors must map to a stable error type/cause so retry behavior is predictable.
- Recommended baseline taxonomy:
  - `VALIDATION_ERROR` (non-retryable)
  - `AUTH_ERROR` (non-retryable until config is fixed)
  - `RATE_LIMITED` (retryable with backoff)
  - `UPSTREAM_TRANSIENT` (retryable)
  - `INTERNAL_ERROR` (retryable, bounded attempts)

5. Report internal LLM usage when handler calls models directly
- If a handler invokes `run_chat_completion()` directly (instead of returning `llm.messages` for the worker runtime path), include a `usage` object in handler output.
- `usage` shape:
  - `tokens_in` (int)
  - `tokens_out` (int)
  - `cost_usd` (numeric/string decimal)
  - optional `openai_request_ids` (string array)
- This keeps `runs.cost_usd`, `tasks.cost_usd`, and budget telemetry accurate.

## Minimal Handler Checklist

- Payload schema exists in `schemas/`.
- Payload is validated before business logic.
- At least one `content_json` artifact is written.
- Logs are secret-safe.
- Error mapping is deterministic and covered by tests.
