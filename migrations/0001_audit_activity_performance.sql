-- S02: cross-cutting audit trail, activity stream and agent counters.
-- Correlation columns are nullable and have no foreign keys yet; S15 adds FKs once the target
-- tables exist. Entity IDs (analysis, review, job, incident) are UUIDs across the platform.

CREATE TABLE operation_audit (
    id            BIGSERIAL PRIMARY KEY,
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    phase         TEXT        NOT NULL,
    event_type    TEXT        NOT NULL,
    operation     TEXT,
    tool_name     TEXT,
    server        TEXT,
    status        TEXT        NOT NULL,
    duration_ms   INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    actor_subject TEXT,
    actor_role    TEXT,
    request_id    TEXT,
    pipeline_id   TEXT,
    dag_id        TEXT,
    run_id        TEXT,
    task_id       TEXT,
    analysis_id   UUID,
    review_id     UUID,
    job_id        UUID,
    incident_id   UUID,
    input         JSONB,
    output        JSONB,
    details       JSONB
);

CREATE INDEX operation_audit_review_id_idx   ON operation_audit (review_id);
CREATE INDEX operation_audit_incident_id_idx ON operation_audit (incident_id);
CREATE INDEX operation_audit_analysis_id_idx ON operation_audit (analysis_id);
CREATE INDEX operation_audit_job_id_idx      ON operation_audit (job_id);
CREATE INDEX operation_audit_occurred_at_idx ON operation_audit (occurred_at);

CREATE TABLE activities (
    id          BIGSERIAL PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    type        TEXT        NOT NULL,
    details     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    incident_id UUID,
    pipeline_id TEXT
);

CREATE INDEX activities_created_at_idx ON activities (created_at);

CREATE TABLE agent_performance (
    id                 SMALLINT PRIMARY KEY CHECK (id = 1),
    completed_analyses BIGINT      NOT NULL DEFAULT 0,
    tool_executions    BIGINT      NOT NULL DEFAULT 0,
    issues_detected    BIGINT      NOT NULL DEFAULT 0,
    total_activities   BIGINT      NOT NULL DEFAULT 0,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO agent_performance (id) VALUES (1);
