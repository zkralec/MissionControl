-- Reliability + scheduler schema updates:
-- - task idempotency + retry metadata
-- - permanent-failure task status
-- - schedules table for recurring jobs

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'taskstatus') THEN
        ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'failed_permanent';
    END IF;
END
$$;

ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(128),
    ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 3,
    ADD COLUMN IF NOT EXISTS next_run_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS max_cost_usd NUMERIC(12, 8) NULL,
    ADD COLUMN IF NOT EXISTS expected_tokens_in INTEGER NULL,
    ADD COLUMN IF NOT EXISTS expected_tokens_out INTEGER NULL;

CREATE INDEX IF NOT EXISTS ix_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS ix_tasks_next_run_at ON tasks(next_run_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_task_type_idempotency_key
    ON tasks(task_type, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS schedules (
    id VARCHAR(36) PRIMARY KEY,
    task_type VARCHAR(64) NOT NULL,
    payload_json TEXT NOT NULL,
    model VARCHAR(64) NULL,
    cron VARCHAR(128) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    last_run_at TIMESTAMPTZ NULL,
    next_run_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_schedules_enabled_next_run_at
    ON schedules(enabled, next_run_at);
