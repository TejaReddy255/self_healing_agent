# Static metadata

Version-controlled knowledge about pipelines, loaded by `backend/metadata` (S04). One document per
file; the directory decides the document type. Changes go through git review (ADR-007) and are
picked up by every process within `METADATA_REFRESH_INTERVAL_S`, or immediately via
`POST /metadata/reload` (admin).

Never put live Airflow state here (run IDs, task states, retry counts, last errors): Airflow is
the runtime source of truth (src §33, invariant 8). Validation rejects such keys.

Quote times such as `"03:00"`: unquoted, YAML 1.1 reads them as base-60 integers.
