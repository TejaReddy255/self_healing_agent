---
id: S04
title: Static Metadata Layer (META-01..04)
status: READY
phase: 1
owner_agent: backend-engineer
reviewers: [code-reviewer, solution-architect]
depends_on: [S03]
blocked_by: []
traces: [FR-001, FR-002, FR-003, FR-004, FR-005, UC-AC8, NFR-002, NFR-006]
source_sections: ["src §33.1–§33.7", "src §33.13", "src §33.14", "src §35.2"]
tags: [contract]
approved_by:
approved_on:
---

# S04 — Static Metadata Layer

## Objective
Load, validate, index and serve the version-controlled knowledge in `metadata/` through a typed
`MetadataService`. Reload is atomic and keeps the last-known-good snapshot. A connector seam allows
Jira/Confluence later without consumer changes. Static metadata describes *intent*. It never holds
live Airflow state (src §33, invariant 8).

## Read first
`docs/architecture.md` §3, §4, §6, §10, §18 (C2, C3, C9), ADR-007, ADR-008, ADR-012; S03 contracts
(`backend/domain/{common,dq,sop}.py`, `hashing.py`); S01 auth (`require_role`) and readiness registry; S02
`AuditService`.

## In scope

### Seed content (`metadata/`, one document per file; the directory decides the type)
| Directory | Document type | Seed files (IDs) |
|---|---|---|
| `pipelines/` | PIPELINE | `customer_daily_load.yaml` (`customer_daily_load`) |
| `dq_rules/` | BUSINESS_RULE | `customer_rules.yaml` (`CUSTOMER-DQ`: BR-CUST-001 NOT_NULL, BR-CUST-002 UNIQUE) |
| `sops/` | SOP | `duplicate_data.yaml` (SOP-PG-002 duplicate removal), `missing_partition.yaml` (SOP-PG-001, src §33.5) |
| `runbooks/` | RUNBOOK | `airflow_task_failure.yaml` (RB-AIRFLOW-TASK-FAILURE), `postgres_load_failure.yaml` (RB-PG-LOAD-FAILURE) |
| `known_issues/` | KNOWN_ISSUE | `duplicate_incremental_replay.yaml` (KI-DQ-001, src §33.5) |
| `ownership/` | OWNERSHIP | `customer_domain.yaml` (OWN-CUSTOMER) |
| `historical_incidents/` | HISTORICAL_INCIDENT | `HIST-001.yaml` (HIST-001) |

`.yaml`, `.yml` and `.json` files are loaded. `README.md` files are ignored. Any other file, or a file at the
`metadata/` root, gives a WARNING `UNKNOWN_FILE` and is not loaded.

### Content models (`backend/metadata/models.py`)
All of them extend the S03 `Contract` base (frozen, `extra="forbid"`, tuples, NFC text) and reuse S03 types:
`SystemName`, `Severity`, `RiskLevel`, `DQProfile`, `PipelineId`, `DagId`, `RuleId`, `QualifiedName`, `ColumnName`.
Document IDs use the S03 identifier pattern (`^[A-Za-z0-9_][A-Za-z0-9_.\-]*$`, ≤ 128).

- **`PipelineMetadata`**:
  - `pipeline_id`, `name`, `description?`, `status: ACTIVE|INACTIVE = ACTIVE`.
  - `airflow{dag_id}`.
  - `source` and `target` are each a `DataEndpoint{system, database?, schema?, table}`. Their qualified name is `schema.table`, or `database.table` when there is no schema.
  - `datasets: tuple[DatasetSpec{name: QualifiedName, system, pii_columns: tuple[ColumnName,...]}]`. Source and target are declared datasets automatically. A `datasets` entry with the same name adds `pii_columns` (NFR-006).
  - `sla{enabled, expected_completion_time? "HH:MM", timezone = "UTC" (IANA), max_runtime_minutes? > 0, severity = CRITICAL}`.
  - `monitoring{enabled, airflow{enabled}, dq{enabled}, sla{enabled}, database{enabled}, mcp{enabled}, critical_runtime_factor = 1.5 (> 1)}`. Each flag defaults to true when `monitoring.enabled`.
  - `self_healing{enabled, auto_rca = false, human_approval_required: Literal[True] = True, permitted_systems?, on_recovery_failure: REPLAN_REQUIRED|FAILED = REPLAN_REQUIRED}`. When unset, `permitted_systems` defaults to the systems of the source and target (agent-design §1 N5).
  - `dq_profile: DQProfile = {}`, in the S03 canonical shape (architecture C9). Keys are checkpoint names.
  - Helpers: `declared_datasets()`, `effective_permitted_systems()`, `pii_columns(dataset)`.
- **`BusinessRuleSet`**:
  - Top-level fields: `rule_set_id`, `pipeline_id`, `dataset: QualifiedName`, `status: ACTIVE|INACTIVE`.
  - `rules: tuple[BusinessRule]` (≥ 1).
  - Each `BusinessRule` has `rule_id`, `rule_type` (upper snake, e.g. `NOT_NULL`, `UNIQUE`), `column?`, `columns?`, `severity`, `blocking`, `expected: JsonObject`, `params: JsonObject = {}`, `description?`.
  - Rule-type catalogue validation against the DQ service is S17.
- **`SOPMetadata`** extends the S03 `SOPProposal` fields with:
  - `sop_id`, `status: APPROVED|DRAFT|DEPRECATED`, `version?`, `pipeline_ids: tuple = ()`.
  - A computed `content_sha256`: SHA-256 of `canonical_json` of the document. It fills `SOPReference.content_sha256` (S10).
- **`Runbook`**: `runbook_id`, `title`, `status`, `pipeline_ids`, `systems`, `categories`, `steps` (read-only diagnostics, ≥ 1), `related_sops`.
- **`KnownIssue`** (src §33.5): `issue_id`, `title`, `category`, `status: ACTIVE|RETIRED`, `pipeline_ids`, `error_signature: JsonObject`, `symptoms`, `likely_causes`, `recommended_diagnostics`, `related_sops`.
- **`Ownership`**: `ownership_id`, `pipeline_ids` (≥ 1), `team`, `support_group`, `reviewers: tuple[ShortText]`, `escalation: tuple[ShortText]`.
- **`HistoricalIncident`**: `historical_incident_id`, `title`, `pipeline_ids`, `category`, `occurred_on: date`, `root_cause`, `resolution`, `related_sops`, `related_known_issues`. It is reference only, never current proof (src §33.3).
- **`MetadataDocument`** (src §33.6), the normalised envelope every connector returns:
  - `document_id`, `document_type`, `title`, `source_type: STATIC|JIRA|CONFLUENCE = STATIC`.
  - `pipeline_ids`, `systems`, `categories`, `tags`, `status`, `version?`.
  - `content: JsonObject`: the typed model's JSON dump.
  - `source_reference`: the path relative to `METADATA_DIR`, e.g. `sops/duplicate_data.yaml`.
- **`MetadataQuery`**: `pipeline_id?`, `system?`, `category?`, `document_types: tuple = ()`, `tags: tuple = ()`. Fields combine with AND; a list field matches any of its values.
- **`ValidationIssue`**: `severity: ERROR|WARNING`, `code`, `message`, `file?`, `document_id?`, `location?` (field path).
- **`MetadataHealth`**:
  - `state: LOADED|DEGRADED|EMPTY`, `version`, `loaded_at`, `counts_by_type`.
  - `errors`, `warnings`: those of the active snapshot. They are non-empty only in EMPTY, when startup had no valid tree.
  - `rejected_candidate?{version, checked_at, errors, warnings}`: the last tree that failed validation and was not swapped in. It is cleared by the next successful swap.
- **`ReloadResult`**: `outcome: RELOADED|UNCHANGED|REJECTED`, `previous_version`, `version`, `health`.

### Components (`backend/metadata/`)
- **`settings.py`**: `MetadataSettings` (pydantic-settings, `.env`), covering `METADATA_DIR` and `METADATA_MAX_FILE_BYTES`.
- **`loader.py`**: discovers files, enforces the size cap, and resolves paths so nothing escapes `METADATA_DIR` (symlinks out are rejected). Each file is decoded as UTF-8 (BOM tolerated) and parsed with a strict safe YAML loader, a `yaml.SafeLoader` subclass that rejects:
  - non-safe tags;
  - duplicate mapping keys;
  - anchors and aliases.

  JSON is parsed with `json.loads`, rejecting duplicate keys and NaN.
- **`validators.py`**: per-document schema validation, which never echoes input values (`include_input=False`). Then cross-document checks producing the issue codes below.
- **`repository.py`**: `MetadataSnapshot`, an immutable indexed snapshot holding:
  - documents by type and by ID, and pipelines by `dag_id`;
  - `version`, the SHA-256 over sorted `(relative path, file SHA-256)` pairs;
  - `loaded_at`.
- **`service.py`**: `MetadataService` (contracts below). It holds the active snapshot reference. `reload()` builds and validates a candidate in `asyncio.to_thread` (no blocking I/O on the event loop), then swaps with one reference assignment under an `asyncio.Lock`, so reloads run one at a time.
- **`refresher.py`** *(decision 2026-10-01)*: `MetadataRefresher(service, interval_s)`, an asyncio background task.
  - **Interval check:** every `METADATA_REFRESH_INTERVAL_S` it computes the tree version (file hashes only, in `asyncio.to_thread`). If the version differs from the active one and from the last rejected candidate, it runs the same validated `reload()`.
  - **Failure handling:** it never raises out of the loop, and logs a WARNING once per rejected version.
  - **Lifecycle:** the API starts and stops it in the app lifespan. Worker processes (S10/S14 onward) reuse the same class.
  - **Disabling:** `0` disables it.
  - **Audit:** automatic swaps are audited like reloads, with actor subject `system:metadata-refresher` and role `service`. Unchanged checks are not audited.
- **Startup** *(decision 2026-10-01)*: the service loads the tree at startup. If it is invalid, the process still starts with an empty snapshot (`state=EMPTY`, errors listed). The readiness component `metadata` is then `down`, and the next valid reload or refresh fills it with no restart.
- **`connectors.py`**: the `MetadataConnector` Protocol `{search(query) -> list[MetadataDocument]; get(external_id) -> MetadataDocument | None}` (src §35.2). `StaticMetadataConnector` reads from the active snapshot.
- **`context_builder.py`**: the `MetadataContextBuilder` interface plus a minimal implementation. `build(pipeline_id, category=None) -> MetadataContext` returns the reference bundle (pipeline, business rules, approved SOPs, runbooks, known issues, ownership, historical incidents). Each item carries `source="metadata"`, `ref_id`, `version` and `fetched_at = snapshot.loaded_at`. S09/S16 add the runtime sections.
- **`api/metadata.py`**: routes (below). The readiness component is `metadata`.

### Validation rules → `ValidationIssue.code`
| Code | Severity | Rule |
|---|---|---|
| `YAML_INVALID` | ERROR | Syntax error, duplicate key, or top level is not a mapping |
| `UNSAFE_YAML` | ERROR | Non-safe tag (e.g. `!!python/object`), anchor or alias |
| `FILE_TOO_LARGE` | ERROR | File > `METADATA_MAX_FILE_BYTES` |
| `PATH_ESCAPE` | ERROR | The resolved path is outside `METADATA_DIR` |
| `SCHEMA_INVALID` | ERROR | The document fails its content model. This includes unknown DQ agent names and short-form `dq_profile` keys (with a C9 hint). |
| `FORBIDDEN_RUNTIME_KEY` | ERROR | Any of `latest_run_id, last_run_id, last_run_status, latest_dag_state, current_task_status, failed_task, retry_count, current_runtime, last_error, recovery_status` as a key at any depth |
| `DUPLICATE_ID` | ERROR | A duplicate `document_id` (global across types), `rule_id` (across rule sets) or `airflow.dag_id` (across pipelines) |
| `BROKEN_REFERENCE` | ERROR | An unknown `pipeline_ids` / `pipeline_id`, `related_sops`, `related_known_issues`, or `dq_profile` `rules` entry not in the pipeline's rule sets. Also a rule set `dataset` that is not one of the pipeline's declared datasets. |
| `SCOPE_INVALID` | ERROR | `permitted_systems` not a subset of the pipeline's dataset systems. Also SLA enabled without `expected_completion_time` or `max_runtime_minutes`, or an unknown IANA timezone. |
| `OWNERSHIP_CONFLICT` | ERROR | More than one ownership document lists the same pipeline |
| `MISSING_OWNERSHIP` | WARNING | A pipeline with no ownership document |
| `DEPRECATED_REFERENCE` | WARNING | `related_sops` points at a non-APPROVED SOP |
| `UNKNOWN_FILE` | WARNING | An unsupported file, or a file at the root |

### APIs (all responses use the S01 error envelope)
| Method | Path | Role | Response |
|---|---|---|---|
| GET | `/metadata/health` | viewer | 200 `MetadataHealth` |
| POST | `/metadata/reload` | admin | 200 `ReloadResult` (RELOADED or UNCHANGED). 422 `METADATA_INVALID` with `details{errors, warnings}`, and the active snapshot and version are unchanged. 403 for non-admin. |
| GET | `/metadata/pipelines` | viewer | 200 `{version, items: [PipelineMetadata]}` sorted by ID |
| GET | `/metadata/pipelines/{pipeline_id}` | viewer | 200 `PipelineMetadata`. 404 `METADATA_NOT_FOUND` |
| GET | `/metadata/sops?pipeline_id&category&status=APPROVED` | viewer | 200 `{version, items: [SOPMetadata]}` |
| GET | `/metadata/known-issues?pipeline_id&category` | viewer | 200 `{version, items}` |
| GET | `/metadata/business-rules?pipeline_id` | viewer | 200 `{version, items: [BusinessRuleSet]}` |
| GET | `/metadata/runbooks?pipeline_id&category` | viewer | 200 `{version, items}` |
| GET | `/metadata/ownership?pipeline_id` | viewer | 200 `{version, items}` |

An unknown `pipeline_id` filter → 404 `METADATA_NOT_FOUND`. An empty match → 200 with `items: []`.
**Envelope extension (additive to S01):** `ApiError` and `error_response` gain an optional `details` object, which is
emitted only when set.

### Audit
Every reload attempt writes an `operation_audit` row via S02 `AuditService`:
- **Fields:** phase `metadata`, event_type `METADATA_RELOAD`, status `RELOADED|UNCHANGED|REJECTED`, and the actor's subject and role.
- **Details:** `previous_version`, `version`, `error_count`, `warning_count`.
- **No database:** when no DB is configured (dev only), the reload proceeds and logs a WARNING `audit unavailable`.

## Out of scope
- Jira/Confluence connector implementations, and pipeline write APIs (ADR-007).
- `/pipelines/{id}/context` (S05) and runtime context sections (S09/S16).
- Rule-type catalogue validation (S17).
- Wiring `MetadataRefresher` into worker processes, which do not exist yet (S10 job worker, S14 monitoring worker).

## Public contracts
- **`MetadataService`** (async), every result deterministic and sorted by document ID:
  - `get_pipeline(pipeline_id) -> PipelineMetadata | None`, `get_pipeline_by_dag_id(dag_id) -> PipelineMetadata | None`, `list_pipelines() -> tuple[PipelineMetadata, ...]`.
  - `get_business_rules(pipeline_id) -> tuple[BusinessRuleSet, ...]`.
  - `get_sops(pipeline_id, category=None) -> tuple[SOPMetadata, ...]`. Returns APPROVED SOPs only. A SOP applies when its `pipeline_ids` contain the pipeline, or its `pipeline_ids` are empty and its `target_system` is in `effective_permitted_systems()`.
  - `get_sop(sop_id) -> SOPMetadata | None`.
  - `get_runbooks(pipeline_id, category=None)`, `get_known_issues(pipeline_id, category=None)`, `get_historical_incidents(pipeline_id, category=None)`.
  - `get_ownership(pipeline_id) -> Ownership | None`.
  - `search(MetadataQuery) -> tuple[MetadataDocument, ...]`, sorted by `(document_type, document_id)`.
  - `health() -> MetadataHealth`.
  - `reload() -> ReloadResult`.
- Service results are typed models. Callers never see dicts from YAML, and no module outside `backend/metadata` reads `METADATA_DIR`.

## Configuration
`METADATA_DIR` (default `metadata`, resolved to an absolute path at startup and logged), `METADATA_MAX_FILE_BYTES` (default 1048576)
and `METADATA_REFRESH_INTERVAL_S` (default 30; `0` disables auto-refresh).
Both go in `.env.example`. The test isolation fixture clears `METADATA_*`. `tzdata` becomes an explicit dependency: Windows has
no system IANA database, and it is currently only present transitively.

## Persistence and migrations
None. Reload audit rows use the existing `operation_audit` (S02).

## Acceptance criteria
- **S04-AC1** All seed documents load with 0 errors. `/metadata/health` shows counts per type (pipeline 1, business_rule 1, sop 2, runbook 2, known_issue 1, ownership 1, historical_incident 1), 0 errors and a 64-hex repository version. The version is identical across two loads of the same tree and changes when any file's bytes change.
- **S04-AC2** Each validation rule in the table above produces its code and severity on a dedicated fixture tree. This covers duplicate `document_id`/`rule_id`/`dag_id`, each broken-reference kind, `permitted_systems` outside the pipeline's systems, ownership conflict (ERROR), and missing ownership and deprecated reference (WARNING). Warnings alone do not block a load.
- **S04-AC3** Reload with an invalid candidate leaves the active snapshot, version and all query results unchanged and returns 422 `METADATA_INVALID` with the errors. A valid reload swaps atomically: readers running throughout many reloads always see one consistent snapshot (every result's `version` matches its content), never a mix. Concurrent reload calls are serialised. Reloading an unchanged tree returns `UNCHANGED`.
- **S04-AC4** A forbidden runtime key at any depth (top level, nested mapping, inside a list) yields `FORBIDDEN_RUNTIME_KEY` and the document is rejected.
- **S04-AC5** Queries are deterministic: identical queries return identical results ordered by ID, independent of file discovery order. `MetadataQuery` filters combine with AND, and list fields match any value. `get_sops` applies the applicability rule and returns APPROVED SOPs only.
- **S04-AC6** No module outside `backend/metadata` reads files under `METADATA_DIR`. A boundary test fails on `open`/`Path.read_*`/`yaml.*load` calls or `metadata_dir` references outside `backend/metadata` (settings wiring in `backend/app.py` is allowed).
- **S04-AC7** `source_type` accepts STATIC, JIRA and CONFLUENCE. Only `StaticMetadataConnector` exists, it satisfies the `MetadataConnector` Protocol, and `pyproject.toml`/`uv.lock` contain no Jira, Confluence or Atlassian packages.
- **S04-AC8** `POST /metadata/reload` returns 403 for viewer, operator, reviewer and service roles. Every reload attempt (RELOADED, UNCHANGED, REJECTED) writes exactly one `operation_audit` row with actor subject/role, outcome and versions.
- **S04-AC9** Unsafe input is rejected without code execution or unbounded expansion: a `!!python/object` tag, an anchor/alias "billion laughs" document, a duplicate YAML key, a file over `METADATA_MAX_FILE_BYTES`, and a symlink resolving outside `METADATA_DIR`.
- **S04-AC10** *(new)* Metadata APIs: each read route returns the documented shape with the snapshot `version`. An unknown `pipeline_id` gives 404 `METADATA_NOT_FOUND`. Unauthenticated requests get 401. The readiness component `metadata` reports:
  - `ok` when LOADED with no rejected candidate;
  - `degraded` (detail `candidate <version> rejected: N errors`) when a newer tree was rejected and last-known-good is still served;
  - `down` (detail `N errors`) when EMPTY.
- **S04-AC11** *(new)* `SOPMetadata.content_sha256` equals `sha256(canonical_json(document))`. It is stable across loads and changes when any SOP field changes. `SOPReference(sop_id, version, content_sha256)` can be built from it.
- **S04-AC12** *(new)* Pipeline semantics: source and target are declared datasets, and `datasets` entries add PII columns. `effective_permitted_systems()` defaults to the source and target systems. `human_approval_required: false` is rejected. Monitoring flags default as specified, and `get_pipeline_by_dag_id` resolves the seed DAG.
- **S04-AC13** *(new, decision 2026-10-01)* Auto-refresh, tested with a fake clock and a temp tree:
  - a valid change on disk is swapped in within one interval;
  - an invalid change keeps last-known-good (readiness `degraded`, `rejected_candidate` populated) and is not re-validated on every tick while unchanged;
  - fixing the files swaps in and clears `rejected_candidate`;
  - every automatic swap or rejection writes one audit row (actor `system:metadata-refresher`);
  - `METADATA_REFRESH_INTERVAL_S=0` starts no task;
  - an exception inside a check is logged and the loop continues.
- **S04-AC14** *(new, decision 2026-10-01)* Startup with an invalid tree does not crash: the app starts, `/health` is 200, readiness `metadata` is `down`, `/metadata/health` shows `state=EMPTY` with the errors, and pipeline reads return 404. After the files are fixed, `POST /metadata/reload` returns RELOADED and readiness becomes `ok`.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1, AC5, AC11, AC12 | unit | `tests/unit/metadata/test_loader_repository.py`, `test_models.py` (fixture trees under `tests/fixtures/metadata/`) |
| AC2, AC4, AC9 | unit | `tests/unit/metadata/test_validators.py` |
| AC3 | unit | `tests/unit/metadata/test_reload_atomic.py` (reader tasks plus repeated reloads) |
| AC13 | unit | `tests/unit/metadata/test_refresher.py` (fake clock, temp tree) |
| AC14 | unit (API) | `tests/unit/api/test_metadata_api.py` (invalid seed tree at startup) |
| AC6 | unit | `tests/unit/test_import_boundaries.py` (metadata boundary) |
| AC7 | contract | `tests/contract/test_metadata_models.py` |
| AC8, AC10 | unit (API with fake audit) + integration (audit rows) | `tests/unit/api/test_metadata_api.py`, `tests/integration/api/test_metadata_api.py` |

## Runtime verification
Start the API and call `/metadata/health` (counts, 0 errors). Break a seed file locally, `POST /metadata/reload` as admin,
and confirm 422 with the old version retained. Restore the file, reload, and confirm RELOADED or UNCHANGED. Check the audit rows.

## Security and eval considerations
- **YAML hardening:** safe YAML only, with no tags, anchors or duplicate keys; size-capped and path-confined.
- **Untrusted text:** metadata free text reaches prompts later, so S09 wraps it in `untrusted()` (NFR-005).
- **No echo:** validation errors never echo document values.
- **Admin-only reload:** reload is restricted to admin and audited.
- **Scope guard:** `permitted_systems` cannot widen beyond the pipeline's own systems, so metadata cannot authorise writes to unrelated systems.

## Rollback
Revert the code. The `metadata/` directory is additive.

## Open issues
1. ~~Cross-process reload~~ **Decided 2026-10-01 (product owner): auto-refresh.** Every process runs `MetadataRefresher` (default 30 s). Workers adopt it when they are built (S10, S14), recorded in those specs.
2. ~~Startup with invalid metadata~~ **Decided 2026-10-01 (product owner): start degraded** (empty snapshot, readiness `down`, fix and reload without restart).
3. **Cross-slice, recorded for S18:** S18 needs `dq_profile` entries to carry `trigger` (`on_profile_change`, `on_warning`). That is an additive field on the S03 `DQProfileEntry` (not part of the approval hash), to be added in S18 with a schema snapshot update. Until then, a `trigger` key is a `SCHEMA_INVALID` error.
4. **Cross-slice:** fields consumed later are defined here with defaults, so those slices need no model change:
   - `sla.*` and `monitoring.critical_runtime_factor` (S14);
   - `self_healing.auto_rca` (S16);
   - `self_healing.on_recovery_failure` (S20);
   - `permitted_systems` and `datasets` (S09);
   - `get_pipeline_by_dag_id` (S13).

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-29 — created (DRAFT)
- 2026-10-01 — refined via `/slice-spec`. Concrete content models, validation codes, API shapes, service contracts and audit added. AC1–AC9 sharpened (IDs kept). AC10–AC12 added (APIs/readiness, SOP content hash, pipeline semantics). YAML hardening (duplicate keys, anchors, path confinement) added. Status READY. Open issues 1–2 need product-owner answers.
- 2026-10-01 — product owner decided open issues 1 (auto-refresh every 30 s across processes) and 2 (start degraded). Added `refresher.py`, `MetadataHealth.state`/`rejected_candidate`, `METADATA_REFRESH_INTERVAL_S`, readiness `degraded`, and AC13/AC14.
