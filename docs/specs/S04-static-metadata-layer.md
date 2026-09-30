---
id: S04
title: Static Metadata Layer (META-01..04)
status: DRAFT
phase: 1
owner_agent: backend-engineer
reviewers: [code-reviewer, solution-architect]
depends_on: [S03]
blocked_by: []
traces: [FR-001, FR-002, FR-003, FR-004, FR-005, UC-AC8]
source_sections: ["src §33.2–§33.7", "src §33.13", "src §33.14", "src §35.2"]
tags: [contract]
approved_by:
approved_on:
---

# S04 — Static Metadata Layer

## Objective
Load, validate, index and serve the version-controlled knowledge in `metadata/` through a typed
`MetadataService`, with atomic last-known-good reload and a connector seam for future Jira/Confluence.

## In scope
- Seed content for the canonical pipeline: `metadata/pipelines/customer_daily_load.yaml`,
  `dq_rules/customer_rules.yaml` (`CUSTOMER-DQ`: BR-CUST-001 NOT_NULL, BR-CUST-002 UNIQUE),
  `sops/duplicate_data.yaml` (SOP-PG-002 duplicate removal), `sops/missing_partition.yaml` (SOP-PG-001),
  `runbooks/airflow_task_failure.yaml`, `runbooks/postgres_load_failure.yaml`,
  `known_issues/duplicate_incremental_replay.yaml` (KI-DQ-001), `ownership/customer_domain.yaml`,
  `historical_incidents/HIST-001.yaml`.
- Pipeline YAML adds, beyond src §33.4: `self_healing.permitted_systems`, `sla.timezone`, `datasets`
  (qualified names with optional `pii_columns`), and the canonical `dq_profile` shape (architecture §18 C9).
- `backend/metadata/models.py`: `MetadataDocument`, `MetadataQuery` (src §33.6), plus typed content
  models `PipelineMetadata`, `BusinessRuleSet`, `SOPMetadata`, `Runbook`, `KnownIssue`, `Ownership`,
  `HistoricalIncident`.
- `loader.py` (`yaml.safe_load`, JSON), `validators.py` (ERROR/WARNING report per src §33.14),
  `repository.py` (immutable indexed snapshot; `version` = SHA-256 over sorted file hashes),
  `service.py` (methods per src §35.2, plus `list_pipelines()`), `connectors.py` (`MetadataConnector`
  Protocol, `StaticMetadataConnector`), and `context_builder.py` stub interface (filled by S09/S16).
- **Forbidden runtime keys** anywhere in a document → ERROR: `latest_run_id, last_run_id, last_run_status,
  latest_dag_state, current_task_status, failed_task, retry_count, current_runtime, last_error, recovery_status`.
- APIs per architecture §6 (`/metadata/*`). Reload is admin-only and returns 422 with the report on failure.
- Readiness component `metadata` (loaded, error count = 0).

## Out of scope
Jira/Confluence implementations; pipeline write APIs (ADR-007); `/pipelines/{id}/context` (S05).

## Public contracts
- `MetadataService` async methods: `get_pipeline(id) -> PipelineMetadata | None`, `list_pipelines()`,
  `get_business_rules(pipeline_id)`, `get_sops(pipeline_id, category=None)`, `get_runbooks(pipeline_id, category=None)`,
  `get_known_issues(pipeline_id, category=None)`, `get_ownership(pipeline_id)`,
  `get_historical_incidents(pipeline_id, category=None)`, `search(MetadataQuery)`, `health() -> MetadataHealth`,
  `reload() -> ReloadResult`.
- `MetadataHealth{version, loaded_at, counts_by_type, errors, warnings}`.

## Configuration
`METADATA_DIR` (default `metadata`), `METADATA_MAX_FILE_BYTES` (default 1 MiB).

## Acceptance criteria
- **S04-AC1** All seed documents load. `/metadata/health` shows counts per type, 0 errors and the repository version.
- **S04-AC2** Duplicate `document_id`, `pipeline_id`, `sop_id` or `rule_id` → ERROR. Broken references (pipeline IDs, related SOPs, DQ-profile agent names, rule refs, unknown `permitted_systems`) → ERROR. Missing ownership → WARNING.
- **S04-AC3** Reload with an invalid candidate leaves the active repository (and version) unchanged and returns 422 with errors. A valid reload swaps atomically: concurrent readers during reload observe either the old or the new snapshot, never a mix (concurrency test).
- **S04-AC4** Documents containing any forbidden runtime key (at any depth) are rejected.
- **S04-AC5** Queries are deterministic: identical queries return identical ordering (sorted by ID).
- **S04-AC6** No module outside `backend/metadata` opens files under `METADATA_DIR` (import/grep boundary test).
- **S04-AC7** `source_type` accepts STATIC/JIRA/CONFLUENCE. Only a STATIC connector exists, and no Jira/Confluence dependencies are in `pyproject.toml`.
- **S04-AC8** `/metadata/reload` returns 403 for a non-admin role. Every reload attempt is audited with the actor and outcome.
- **S04-AC9** YAML with a non-safe tag (e.g. `!!python/object`) is rejected. Files larger than `METADATA_MAX_FILE_BYTES` are rejected.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1, AC5 | unit | `tests/unit/metadata/test_loader_repository.py` |
| AC2, AC4, AC9 | unit | `tests/unit/metadata/test_validators.py` (fixture trees under `tests/fixtures/metadata/`) |
| AC3 | unit | `tests/unit/metadata/test_reload_atomic.py` |
| AC6 | unit | `tests/unit/test_import_boundaries.py` |
| AC7 | contract | `tests/contract/test_metadata_models.py` |
| AC8 | integration | `tests/integration/api/test_metadata_api.py` |

## Runtime verification
Start the API; call `/metadata/health`; break a seed file locally; call reload and see 422 with the old version retained.

## Security and eval considerations
Metadata free text reaches prompts later, so it is treated as untrusted in S09. Safe YAML loading only.

## Rollback
Revert code; the metadata directory is additive.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-29 — created (DRAFT)
