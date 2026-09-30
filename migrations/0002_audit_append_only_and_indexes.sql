-- S02 review follow-up (data-engineer M3, M5, L2).
--
-- 1. recorded_at: server-assigned insert time. occurred_at stays the caller's event time and may
--    be earlier; retention decisions use recorded_at so back-dated events cannot be purged early.
-- 2. operation_audit is append-only (NFR-041): UPDATE and TRUNCATE are rejected, and DELETE is
--    allowed only for rows recorded more than 13 months ago (retention, use-case-spec §7).
--    A trigger does not stop a superuser; separating the owner role from a DML-only runtime role
--    is S23/S25 scope.
-- 3. Correlation indexes become partial composite indexes matching timeline queries
--    ("rows for review X ordered by time") and skipping the many NULLs.

ALTER TABLE operation_audit
    ADD COLUMN recorded_at TIMESTAMPTZ NOT NULL DEFAULT now();

DROP INDEX operation_audit_review_id_idx;
DROP INDEX operation_audit_incident_id_idx;
DROP INDEX operation_audit_analysis_id_idx;
DROP INDEX operation_audit_job_id_idx;

CREATE INDEX operation_audit_review_timeline_idx
    ON operation_audit (review_id, occurred_at) WHERE review_id IS NOT NULL;
CREATE INDEX operation_audit_incident_timeline_idx
    ON operation_audit (incident_id, occurred_at) WHERE incident_id IS NOT NULL;
CREATE INDEX operation_audit_analysis_timeline_idx
    ON operation_audit (analysis_id, occurred_at) WHERE analysis_id IS NOT NULL;
CREATE INDEX operation_audit_job_timeline_idx
    ON operation_audit (job_id, occurred_at) WHERE job_id IS NOT NULL;
CREATE INDEX operation_audit_recorded_at_idx ON operation_audit (recorded_at);

CREATE FUNCTION operation_audit_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'operation_audit is append-only: UPDATE is not allowed'
            USING ERRCODE = 'insufficient_privilege';
    ELSIF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'operation_audit is append-only: TRUNCATE is not allowed'
            USING ERRCODE = 'insufficient_privilege';
    ELSIF TG_OP = 'DELETE' THEN
        IF OLD.recorded_at >= now() - INTERVAL '13 months' THEN
            RAISE EXCEPTION 'operation_audit rows can only be deleted after the 13-month retention period'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN OLD;
    END IF;
    RETURN NULL;
END;
$$;

-- recorded_at is always the server's insert time, even if an INSERT supplies a value, so rows
-- cannot be back-dated to fall out of retention early.
CREATE FUNCTION operation_audit_stamp() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.recorded_at := now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER operation_audit_stamp_recorded_at
    BEFORE INSERT ON operation_audit
    FOR EACH ROW EXECUTE FUNCTION operation_audit_stamp();

CREATE TRIGGER operation_audit_no_update
    BEFORE UPDATE ON operation_audit
    FOR EACH ROW EXECUTE FUNCTION operation_audit_guard();

CREATE TRIGGER operation_audit_retention_delete
    BEFORE DELETE ON operation_audit
    FOR EACH ROW EXECUTE FUNCTION operation_audit_guard();

CREATE TRIGGER operation_audit_no_truncate
    BEFORE TRUNCATE ON operation_audit
    FOR EACH STATEMENT EXECUTE FUNCTION operation_audit_guard();
