-- Create artifacts table for persisted task/run outputs.
-- Safe to run multiple times.

CREATE TABLE IF NOT EXISTS artifacts (
    id VARCHAR(36) PRIMARY KEY,
    task_id VARCHAR(36) NOT NULL REFERENCES tasks(id),
    run_id VARCHAR(36) NOT NULL REFERENCES runs(id),
    artifact_type VARCHAR(64) NOT NULL,
    content_text TEXT NULL,
    content_json JSONB NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_artifacts_task_id ON artifacts(task_id);
CREATE INDEX IF NOT EXISTS ix_artifacts_run_id ON artifacts(run_id);
CREATE INDEX IF NOT EXISTS ix_artifacts_artifact_type ON artifacts(artifact_type);
