---
title: Use-Case Specification — Agentic Self-Healing Pipeline Monitor
status: APPROVED (G1 Requirements, 2026-09-30, product owner)
version: 0.2
source: self_healing_platform_architecture_coding_agent.md (v1.3-agent-ready) + DQ agent suite brief (2026-09-30)
last_updated: 2026-09-30
---

# Use-Case Specification

Section references like "src §21.5" point to
[`self_healing_platform_architecture_coding_agent.md`](../self_healing_platform_architecture_coding_agent.md).
Greenfield interpretation rules are in [`sdd-workflow.md` §2](sdd-workflow.md#2-greenfield-interpretation-of-the-source-architecture).

## 1. Problem statement

Data teams operate Airflow ETL pipelines that move data between PostgreSQL, MySQL, S3 and MongoDB.
When a pipeline fails, or completes but loads bad data, detection depends on someone watching the
Airflow UI. Diagnosis means reading logs and querying systems by hand, and fixes are applied ad hoc
without a durable record of who authorized what. We need a control plane that:

- continuously and deterministically detects Airflow, SLA, data-quality and infrastructure problems;
- correlates them into one incident per issue;
- uses an LLM agent to investigate with read-only tools and propose a governed remediation;
- executes only the exact remediation a human approved;
- declares an incident resolved only after independent technical, data-quality and pipeline-recovery
  verification.

## 2. Actors

| Actor | Type | Goals |
|---|---|---|
| **Viewer** | Human | See pipeline health, incidents, analyses, audit. |
| **Operator** | Human | Trigger/track Airflow runs, start RCA, run monitoring checks, dismiss incidents. |
| **Reviewer** | Human | Inspect RCA and proposed remediation, edit it, approve or reject the exact revision. |
| **Administrator** | Human | Reload metadata, read backend logs, manage configuration and tool permissions. |
| **DQ analyst** (Operator role) | Human | Upload and manage data files, preprocess, profile, validate, reconcile, run statistical and anomaly checks in the DQ Workbench; accept DQ baselines. |
| **Pipeline owner / support group** | Human (via metadata) | Owns pipeline metadata, SOPs, runbooks and DQ rules in version control. |
| **Airflow** | System | Runs ETL; emits failure callbacks and DQ checkpoint calls; runs recovery DAG runs. |
| **Monitoring worker** | System | Runs scheduled deterministic monitoring cycles. |
| **Remediation / revision / analysis workers** | System | Execute durable jobs. |
| **DQ service** (File Manager, Preprocessing, Profiling, Custom DQ, Recon, Statistical Check, Anomaly Detection) | System (built in this repo, ADR-013) | Produce deterministic DQ measurements. The platform consumes them through adapters. |
| **Gemini / Vertex AI** | External service | Reasoning for RCA, planning, revision validation, SOP generation, execution. |
| **MCP servers** (PostgreSQL, MySQL, S3, MongoDB) | System | Tool access to data systems. |

## 3. Workflows

### 3.1 As-is (described by source; not present in this repo)

Operators watch Streamlit, which scans Airflow directly. They manually start "Agentic AIR" for a
failed run. The backend runs a read-only RCA, persists a review, and supports human revision and
approval. Approval then **synchronously** runs remediation. The backend then triggers a recovery run
but does not wait for it (src §14).

### 3.2 To-be (this project)

```text
Static pipeline policy + live Airflow/DQ/MCP evidence
 → deterministic monitors → MonitoringEvent → Incident (deduplicated)
 → [auto or manual] read-only RCA with evidence precedence → remediation plan with verification assertions
 → HITL review / AI-validated revisions → approval of exact revision hash
 → durable remediation job → pre-check → scoped MCP write
 → technical verification (deterministic assertions) → DQ verification (only relevant rules)
 → Airflow recovery run → terminal SUCCESS → final DQ gate → Incident RESOLVED
```

The manual path (operator starts RCA for a failed run without an incident) also exists. It uses
the same DiagnosticService.

## 4. Scope

### In scope

- Static metadata layer (YAML in `metadata/`) with validation and atomic reload.
- Backend Airflow integration: live context, agent read tools, operator APIs, recovery trigger and
  terminal-state verification.
- MCP manager, explicit tool-permission registry, and in-repo MCP servers for PostgreSQL and MySQL,
  then S3 and MongoDB.
- Gemini-based agents: Diagnostic, Review Revision, SOP Generation, Healing (execution), Execution Replan.
- HITL review, immutable revisions, SHA-256 approval hash, SOP governance.
- Durable jobs: revision, analysis (RCA), remediation, recovery.
- Monitoring: Airflow, SLA, DB, MCP, DQ monitors; orchestrator; worker; event ingestion.
- Incidents: fingerprint deduplication, state machine, timeline.
- **DQ agent suite** built as an independent DQ service (`docs/dq-agents-design.md`): File Manager, Data
  Preprocessing, Data Profiling, Custom DQ, Data Recon, Statistical Check, Anomaly Detection; plus a DQ Workbench UI.
- DQ platform integration through adapters, registry, orchestrator, persistence, verification router.
- Streamlit operator console talking only to FastAPI.
- Audit, activity stream, LLM cost tracking, RBAC, observability.
- Canonical E2E scenario (duplicate customers) and an agent evaluation suite.

### Out of scope (this release)

- Jira and Confluence connectors (seam only), vector DB/RAG, extra LLM retrieval layers (src §33.16).
- DQ algorithms inside the self-healing backend. They live only in the DQ service (ADR-013; invariant 10 at the platform boundary).
- MongoDB collections as DQ datasets (OQ-17); uploading externally trained model binaries (OQ-14).
- Preprocessing or any DQ capability writing to source/target data systems. DQ outputs are derived datasets and reports only.
- Automatic (non-human-approved) production writes (invariant 5).
- Pipeline CRUD through the API (`POST/PUT /pipelines`); pipelines are defined in version-controlled YAML (ADR-007).
- The "human-input question" endpoint mentioned in src §4.4 (OQ-09).
- Multi-tenancy.

## 5. Functional requirements

### Metadata (static knowledge)
- **FR-001** Load pipeline, DQ-rule, SOP, runbook, known-issue, ownership and historical-incident documents from `metadata/` (YAML/JSON) into a typed, indexed in-memory repository.
- **FR-002** Validate documents on load: schema, duplicate IDs, cross-references, forbidden transient runtime fields. Report errors (blocking) and warnings.
- **FR-003** Atomic reload: validate a candidate repository and swap only on success; keep the last-known-good repository otherwise.
- **FR-004** Expose typed query methods (`MetadataService`) and read APIs; no other module reads metadata files directly.
- **FR-005** Provide a `MetadataConnector` seam supporting `source_type` STATIC now and JIRA/CONFLUENCE later without consumer changes.

### Airflow
- **FR-010** Provide live Airflow context for a pipeline (DAG pause state, runs, task instances, try numbers, timings, failed tasks), with freshness timestamps.
- **FR-011** Provide operator APIs to list DAGs/runs/tasks, trigger a run and pause/resume a DAG, via FastAPI.
- **FR-012** Provide read-only Airflow tools to agents (run details, failed tasks, task logs with truncation/redaction).
- **FR-013** Trigger an idempotent recovery run only from deterministic worker code, never from an agent tool.
- **FR-014** Wait for a recovery run to reach a terminal state, with a configurable timeout.

### MCP and tool governance
- **FR-020** Probe, optionally start (dev only), and discover tools on the configured MCP servers independently.
- **FR-021** Classify every tool through an explicit permission registry. Unknown tools are unavailable to agents.
- **FR-022** The diagnostic toolset contains only read tools. The execution toolset contains only the approved target system's tools, and only after approval.
- **FR-023** Provide MCP servers for PostgreSQL and MySQL (then S3, MongoDB) with read tools on read-only credentials and guarded write tools.
- **FR-024** Expose MCP status (health, tool counts by class, unclassified tools).

### Diagnosis (RCA)
- **FR-030** Run a read-only RCA for a failed run on operator request (`POST /agent`) and for an incident (manual or `auto_rca`).
- **FR-031** Produce and persist a validated `StructuredAnalysis` (summary, findings with evidence status, recommendations, SOP match, proposed SOP, proposed remediation, confidence).
- **FR-032** Build model context with separated sections by evidence precedence (src §0.5, §33.12). Delimit untrusted content.
- **FR-033** Deterministically validate proposed remediation: target within the pipeline's permitted scope, required fields present, ≥ 1 verification assertion. Only then mark the plan executable.
- **FR-034** Prevent concurrent duplicate RCA for the same scope.

### HITL governance
- **FR-040** Create a PENDING review for an analysis with an executable plan. Allow at most one active review per scope.
- **FR-041** Compute the approval payload hash as SHA-256 of canonical JSON over {sop_match_status, matched_sop_reference, proposed_sop, proposed_remediation}.
- **FR-042** Approve only if the reviewer-supplied revision and hash equal the active ones (compare-and-set), and never while a revision job is pending.
- **FR-043** Reject a review; nothing executes.
- **FR-044** Accept human edits as durable revision jobs, validated by the Review Revision Agent. Accepted edits create immutable revision N+1 with a new hash and cleared approval.
- **FR-045** Recover revision jobs interrupted by restarts (lease expiry, bounded attempts).
- **FR-046** Resolve matched SOPs from static APPROVED SOPs plus runtime-approved SOPs. When no SOP applies, generate one. Install a proposed SOP only on approval of the review containing it.

### Execution, verification, recovery
- **FR-050** Approval enqueues a durable remediation job and returns 202. No LLM work runs in the request.
- **FR-051** At job start and before any write, re-verify review status and hash. Mismatch aborts with zero tool calls.
- **FR-052** PRECHECK re-reads target state and evaluates precondition assertions. A contradiction yields REPLAN_REQUIRED before any write.
- **FR-053** The Healing Agent executes only the approved plan with target-scoped tools. The write guard requires a prior read, and writes are bounded by the plan's declared operations.
- **FR-054** Audit every stage transition, tool call and model call with correlation IDs.
- **FR-055** On stop condition or verification failure, the Execution Replan Agent proposes a new revision that requires fresh approval.
- **FR-060** Technical verification evaluates the plan's typed assertions deterministically via read-only tools, in addition to the structural write-then-read gate.
- **FR-061** DQ verification reruns only the rule IDs named in the approved plan.
- **FR-062** Recovery is triggered only after technical and DQ verification pass.
- **FR-063** After recovery reaches terminal SUCCESS, the final DQ gate reruns the required rules for the run.
- **FR-064** An incident becomes RESOLVED (and a review SUCCEEDED) only after the full chain passes.

### Monitoring
- **FR-070** Ingest event-driven signals (`POST /monitoring/events`) idempotently and quickly, without LLM/MCP work.
- **FR-071** Run scheduled monitoring cycles in a separate worker process, independent of the UI, with single-leader execution.
- **FR-072** Airflow monitor rules per src §21.5 (failed run/task, over-duration, queued-is-healthy).
- **FR-073** SLA monitor: expected completion time and max runtime from metadata vs live Airflow timing.
- **FR-074** Database and MCP health monitors for dependencies relevant to each pipeline.
- **FR-075** Aggregate and expose pipeline and fleet health.
- **FR-076** Select monitors from pipeline metadata only (no hard-coded pipeline logic).

### Incidents
- **FR-080** Deduplicate events into incidents by versioned fingerprint. Repeated polls never create duplicates.
- **FR-081** Enforce the incident state machine (docs/architecture.md §7.4). Record every transition with actor and reason.
- **FR-082** Provide an incident timeline of linked events, DQ results, analyses, reviews, revisions, jobs, verification and recovery, by reference.
- **FR-083** Manual and automatic incident RCA use the same DiagnosticService. Automatic RCA is enqueued, never run inside the monitoring loop.
- **FR-084** Carry `incident_id` on analyses, reviews, audit and activities when a workflow originates from an incident.
- **FR-085** Operators can dismiss (REJECT) an incident that needs no remediation, with a reason.

### Data quality
- **FR-090** Wrap each existing DQ agent in an adapter producing `DQAgentResult`, with no algorithm duplication.
- **FR-091** Registry of adapters by logical name (`custom_dq`, `recon`, `profiling`, `statistical`, `anomaly`, `preprocessing`).
- **FR-092** DQ Orchestrator selects agents by pipeline DQ profile and checkpoint, and can restrict to named rule IDs.
- **FR-093** Persist DQ runs and results (`dq_runs`, `dq_results`) separately from rule definitions.
- **FR-094** `POST /dq/run` for Airflow checkpoints (sync with timeout, or 202), idempotent on request key.
- **FR-095** CRITICAL/WARNING DQ results become MonitoringEvents.
- **FR-096** Expensive agents (statistical, anomaly) run only on their configured schedule or trigger.

### Operator experience
- **FR-100** Streamlit console: monitoring dashboard, pipeline health, incident page (tabs per src §21.11), self-healing, review/HITL, governance, metadata/settings.
- **FR-101** Streamlit communicates only with FastAPI.
- **FR-102** Governance views: agent performance, LLM cost, analyses history, audit.
- **FR-103** Context panels show source and freshness of each context section.

### Audit and activity
- **FR-110** Activity stream (`GET /activities`, WebSocket `/ws/activities`).
- **FR-111** LLM token/cost accounting per call, aggregated (`GET /llm-costs`).
- **FR-112** Administrator-only, redacted log tail (`GET /logs`).

### DQ service (shared)
- **FR-120** Independent DQ service exposing all DQ capabilities via a versioned internal API (`/v1`), callable only by the backend, with no LLM dependency.
- **FR-121** Datasets are referenced as file versions, PostgreSQL/MySQL tables or S3 objects, and accessed read-only.
- **FR-122** DQ work runs as durable, idempotent jobs with progress, plus a sync-wait mode with a timeout.
- **FR-123** Every result is reproducible: agent version, params hash, input fingerprints, seed and sampling details are recorded.

### File Manager
- **FR-130** Upload CSV/Parquet/JSON/XLSX with size, type and structure validation. Immutable, SHA-256-addressed versions.
- **FR-131** Preview with inferred schema and row count. Declared PII columns are masked everywhere.
- **FR-132** Registry of models trained by the service (versions, lineage, metrics). Only signed, service-produced artifacts load.
- **FR-133** Workbench analysis presets ("Pipeline Settings"): named, versioned, ordered agent steps. Governed pipeline DQ configuration is shown read-only from metadata.
- **FR-134** Soft delete and retention that preserves versions referenced by lineage, models or platform evidence.

### Data Preprocessing
- **FR-140** Cleaning: de-duplication, trimming/whitespace, null handling strategies, type coercion with failure counts, case standardization.
- **FR-141** Transformation: rename, select, cast, derived columns and filters via a safe expression DSL, datetime parsing, split.
- **FR-142** Normalization: min-max, z-score, robust, log1p. Fitted parameters are recorded and reusable.
- **FR-143** Integration: join and union of datasets with unmatched/conflict reports.
- **FR-144** Output is a new derived dataset version with lineage. There is never a write to source/target systems.

### Data Profiling
- **FR-150** Categorical statistics (missing, distinct, top-k, rare categories, entropy, length and pattern classes).
- **FR-151** Numerical statistics (moments, quantiles, IQR, skewness, kurtosis, zeros/negatives/infinities).
- **FR-152** Distribution analysis (histograms, normality test, shape hints, correlations).
- **FR-153** Detailed report as canonical JSON plus rendered HTML/Markdown.
- **FR-154** Baseline comparison (PSI, KS, schema changes) and a profile hash for change triggers.

### Custom DQ
- **FR-160** A rule catalog across Incompleteness, Inaccuracy, Inconsistency and Invalidity (plus volume/timeliness).
- **FR-161** Execute rules from metadata business rules or workbench rule sets, pushed down to the source database where possible.
- **FR-162** Quick validation: auto-generated advisory checks without predefined rules.
- **FR-163** Per-rule results with expected, actual, failed/total counts and masked samples.
- **FR-164** Rerun a named subset of rules (`rule_ids`).

### Data Recon
- **FR-170** Cross-verification of counts and per-column aggregates (optionally per partition) with tolerances.
- **FR-171** Key-based alignment: missing, extra, duplicate keys and per-column value mismatches.
- **FR-172** Consistency checks via bucketed checksums with drill-down, equal in result to full comparison.
- **FR-173** Error resolution: deterministic discrepancy classification with an advisory SOP category. Never executed automatically.

### Statistical Check
- **FR-180** Median Absolute Deviation / robust z-scores.
- **FR-181** Outlier detection (IQR, z-score, MAD, consensus).
- **FR-182** p-value testing against a baseline (KS, Mann-Whitney, χ²) with multiple-testing correction and effect-size gating.
- **FR-183** Skewness and kurtosis measures with thresholds and baseline change.
- **FR-184** Multivariate checks: PCA reconstruction error and variance shift, KNN distance outliers, factor-analysis structure change.

### Anomaly Detection
- **FR-190** Unsupervised detection (Isolation Forest, LOF).
- **FR-191** Supervised training from labelled data with stored evaluation metrics and threshold selection.
- **FR-192** Normalized anomaly scoring with thresholds and per-feature explanations.
- **FR-193** Pattern recognition on metric time series (seasonal decomposition residuals, level shifts).
- **FR-194** Separate train and score operations with pinned, versioned, schema-checked models.

### DQ Workbench
- **FR-200** Streamlit DQ Workbench with a page per capability, working only through backend gateway routes.
- **FR-201** Optional AI explanation of a DQ report whose every cited number is verified against the result. It is labelled AI-generated.

## 6. Non-functional requirements

Values marked *(proposed)* are initial targets for confirmation at G1.

| ID | Category | Requirement |
|---|---|---|
| NFR-001 | Security | No secrets, tokens or connection strings in code, logs, audit rows, fixtures or LLM prompts. Redaction applies to logs, audit and Airflow log excerpts. |
| NFR-002 | Security | RBAC roles Viewer, Operator, Reviewer, Administrator, plus a machine role `service` for Airflow callbacks and DQ checkpoints. Every route declares its required role. |
| NFR-003 | Security | No credential defaults in code. Production startup fails fast when a required secret is missing. CORS is an explicit allowlist. |
| NFR-004 | Security | Diagnostic and monitoring paths have no access to write-capable tools (enforced structurally and tested). |
| NFR-005 | Security | Tool outputs, logs, DB values, human edits and metadata text are treated as untrusted in prompts and delimited. The agent's allowed actions do not depend on their content. |
| NFR-006 | Security / privacy | Row samples sent to the LLM are capped *(proposed: ≤ 20 rows, ≤ 8 KB per tool result)*. Columns marked `pii: true` in metadata are masked before leaving the MCP layer. |
| NFR-010 | Availability | Monitoring, RCA, remediation and recovery survive Streamlit/browser disconnects and backend restarts (durable jobs with leases). |
| NFR-011 | Reliability | Every external call has a timeout (docs/architecture.md §13). Idempotent GETs retry with backoff; non-idempotent writes never retry automatically. |
| NFR-012 | Reliability | Ingestion, polling, incident creation, job start and recovery trigger are idempotent. |
| NFR-013 | Reliability | A remediation interrupted during EXECUTING is never automatically re-executed. It is surfaced to operators. |
| NFR-020 | Performance | `POST /monitoring/events` p95 < 200 ms at 20 req/s *(proposed)*. `POST /reviews/{id}/approve` p95 < 1 s. |
| NFR-021 | Performance | A monitoring cycle for 50 pipelines completes within the 60 s default interval *(proposed)*. |
| NFR-022 | Performance | RCA bounded by agent limits: ≤ 25 tool calls, ≤ 30 model calls, ≤ 10 min wall clock *(proposed)*. |
| NFR-030 | Cost | Every model call records tokens and estimated cost. A per-analysis token budget is configurable *(proposed default 400k input tokens)*, and exceeding it stops the agent. |
| NFR-040 | Auditability | Every human decision, AI revision, tool call, model call, stage transition and incident transition is persisted with actor and correlation IDs. |
| NFR-041 | Auditability | Approved payload hash, revision history and SOP installations are immutable once written. |
| NFR-050 | Usability / accessibility | Status is never conveyed by color alone. Errors are actionable (no raw stack traces). Long operations show progress stages. |
| NFR-060 | Maintainability | Routes thin, orchestration in services, SQL in repositories (src §0.1-7). Contracts versioned with schema snapshots. |
| NFR-061 | Operability | Liveness/readiness endpoints for API and workers. Structured JSON logs. Metrics for the items in src §29. |
| NFR-070 | DQ determinism | Identical DQ requests (inputs, params, seed, agent version) yield identical findings and derived outputs. |
| NFR-071 | DQ resource bounds | Upload ≤ 500 MB *(proposed)*; statistical/ML work on samples ≤ 100k rows by default; per-job timeout 15 min; bounded worker concurrency and DuckDB memory. |
| NFR-072 | DQ security | Upload type sniffing and caps; safe expression DSL only (no code eval); no user-supplied pickles; HMAC-signed model artifacts; sandboxed DuckDB; formula-injection-safe exports. |
| NFR-073 | DQ performance | Targets in `docs/dq-agents-design.md` §11 *(proposed)*. |
| NFR-074 | DQ data access | The DQ service holds read-only credentials only and is reachable only by the backend. |

## 7. Data sources, classification, retention, ownership

| Data | Source of truth | Classification | Retention *(proposed)* | Owner |
|---|---|---|---|---|
| Pipeline metadata, DQ rules, SOPs, runbooks, known issues, ownership, history | `metadata/` in git | Internal | Git history | Pipeline owners |
| DAG/run/task/log state | Live Airflow | Internal; logs may contain sensitive values | Not stored except bounded excerpts in audit/analysis evidence | Airflow platform |
| Source/target data | PostgreSQL, MySQL, S3, MongoDB via MCP | **Potentially PII/confidential** (OQ-08) | Not stored; bounded, masked samples in evidence | Data owners |
| Monitoring events | Operational PG | Internal | 180 days | Platform |
| Incidents, analyses, reviews, revisions, jobs, verification, recovery | Operational PG | Internal / audit | 13 months minimum | Platform |
| Operation audit, LLM usage | Operational PG | Audit | 13 months minimum | Platform / security |
| DQ runs/results | Operational PG | Internal; failed-row samples masked | 180 days (results), 13 months (run summaries) | DQ team |
| Runtime-approved SOPs | Operational PG (`approved_sops`) | Internal / governed | Indefinite | Reviewers |
| Uploaded files and derived datasets | Local volume `dq-storage` (`files/`) + schema `dq_service` | **Potentially PII** (declared per upload) | Soft delete + 30-day purge *(proposed)*, unless referenced | DQ analysts |
| DQ reports, model artifacts | Local volume `dq-storage` (`artifacts/`) | Internal; masked samples | 13 months (reports), while referenced (models) | DQ team |

## 8. Decision classification

| Decision | Class |
|---|---|
| Is a task/run failed, late, over SLA? Is a DB/MCP unhealthy? | **Deterministic** |
| Does a DQ rule pass? What are counts and statistics? | **Deterministic** (DQ service) |
| Severity, fingerprint, deduplication, incident transitions | **Deterministic** |
| Payload hash, approval compare-and-set, tool-set composition | **Deterministic** |
| Is the plan within permitted scope and structurally complete? | **Deterministic** |
| Did the technical assertions pass? Did DQ verification and final DQ pass? Did recovery succeed? | **Deterministic** |
| Root cause, remediation plan, SOP applicability | **LLM-assisted**, requires human approval before any effect |
| Is a human edit safe and consistent with evidence? | **LLM-assisted** (revision agent), then **human-approved** |
| New SOP content | **LLM-assisted**, then **human-approved** |
| Which exact tool calls implement the approved plan | **LLM-assisted**, bounded by the write guard and approved plan |
| Replan after failure | **LLM-assisted**, then **human-approved** |
| Any write to an external data system | **Human-approved** (exact revision hash) |
| Profiling statistics, rule outcomes, recon counts, statistical tests, anomaly scores | **Deterministic** (DQ service; seeded where randomized) |
| Recon "error resolution" category | **Deterministic**, advisory only |
| Preprocessing outputs (derived datasets) | **Deterministic**; no approval needed (no source/target writes) |
| Which profile becomes a DQ baseline | **Human-approved** (operator, audited) |
| Narrative explanation of a DQ report | **LLM-assisted**, advisory, numbers verified deterministically |

## 9. Success metrics

| Metric | Target *(proposed)* |
|---|---|
| Mean time to detect (failure → incident) | < 2 min for callback path; < 1 interval + 30 s for polled conditions |
| Duplicate incident rate for the same condition | 0 |
| RCA root-cause agreement with golden cases | ≥ 80 % (eval E-RCA) |
| Structured-output validity | ≥ 99 % |
| Write tool calls during diagnosis/monitoring | **0 (hard gate)** |
| Executions without a matching approved hash | **0 (hard gate)** |
| Incidents marked RESOLVED without the full verification chain | **0 (hard gate)** |
| Remediations passing verification on first approved plan (canonical classes) | ≥ 70 % |
| Median approval-to-resolution time (canonical scenario) | < 30 min excluding pipeline runtime |
| LLM cost per RCA | tracked; alert above configured budget |
| Custom DQ / Recon count accuracy on synthetic and canonical data | **100 % exact (hard gate)** |
| Statistical false-alarm rate on no-shift trials (alpha 0.05) | ≤ 7 % |
| Isolation Forest ROC-AUC on the synthetic benchmark | ≥ 0.90 |

## 10. System-level acceptance criteria

- **UC-AC1 (canonical, src §35.9):** With source 100,000 clean rows and target 100,417 rows (417
  duplicate `customer_id`), the system progresses in order: TARGET DQ CRITICAL → Recon CRITICAL →
  MonitoringEvent → exactly one incident → read-only RCA → review → exact-hash approval → durable
  execution → technical verification → duplicate rule rerun = 0 → recon difference = 0 → recovery run
  → terminal SUCCESS → final DQ PASS → RESOLVED. The test fails if success is declared at any
  earlier point.
- **UC-AC2:** No write-capable tool is ever invoked during monitoring or diagnosis (asserted from the audit log across all E2E and eval runs).
- **UC-AC3:** A stale approval (hash or revision changed after approval) never executes.
- **UC-AC4:** Recovery is never triggered before technical and DQ verification pass.
- **UC-AC5:** A replan that changes a write action requires a new revision and fresh approval.
- **UC-AC6:** Repeated polling of an unresolved condition yields one open incident.
- **UC-AC7:** Monitoring detects failures and creates incidents with no Streamlit process running.
- **UC-AC8:** Invalid metadata reload leaves the running repository unchanged.
- **UC-AC9:** Prompt-injection content in logs or data does not cause out-of-scope tool calls or plans (eval E-INJ).
- **UC-AC10:** In the DQ Workbench, an analyst can upload the canonical target extract, profile it, find exactly 417
  duplicate `customer_id` values with Custom DQ, reconcile against the source (+417), remove them with Preprocessing into a
  derived version, and re-validate to PASS. No source/target system is modified.

## 11. Assumptions

- A-01 Python 3.12. Airflow 2.x stable REST API `/api/v1` with basic auth (confirmed, OQ-03).
- A-02 One operational PostgreSQL instance. Postgres-backed job queue is sufficient for v1 scale (tens of pipelines; tables ≤ ~10M rows, OQ-18).
- A-03 Gemini on **Vertex AI** via LangChain `ChatGoogleGenerativeAI`, with **one strong model for all agents** (confirmed, OQ-04). Per-agent overrides remain available.
- A-04 Docker is available for local integration/E2E (operational PG, MySQL, target PG, Airflow).
- A-05 The DQ agents are built in this repository as the DQ service (ADR-013). If earlier implementations exist (OQ-16), their algorithms are ported behind the same contracts.
- A-06 Human reviewers and operators are internal staff. Single organization.

## 12. Open questions

All open questions were answered by the product owner on **2026-09-30**. There are no open blockers.

| ID | Question | Affects | Decision (2026-09-30) |
|---|---|---|---|
| OQ-01 | How are the existing DQ agents invoked? | S17, S18 | The DQ suite is built in-repo as an independent service (ADR-013). |
| OQ-02 | Reuse existing MCP servers, or build them? | S07, S24 | **Build in-repo** with the official MCP Python SDK. |
| OQ-03 | Airflow 2.x or 3.x? | S05 | **Airflow 2.x** — REST `/api/v1`, basic auth. The client keeps a version seam. |
| OQ-04 | Gemini access mode and models? | S08 | **Vertex AI** (GCP project + service account/ADC). **One strong model for all agents** (e.g. Gemini 2.5 Pro); per-agent overrides stay configurable. |
| OQ-05 | Identity provider for RBAC? | S01, S23 | **Hashed per-user tokens in v1; OIDC/SSO in S23.** Tokens remain only for the Airflow `service` role after S23. |
| OQ-06 | Where are runtime-approved SOPs stored? | S10 | **Database table `approved_sops`** (ADR-008 accepted). No `SOP.txt`. |
| OQ-07 | Production deployment target? | S25 | **Docker Compose on a VM**, with Kubernetes-ready health checks. |
| OQ-08 | PII and sending samples to Gemini? | S07, S09, S17, S24 | **Data contains PII.** Columns tagged `pii: true` are masked before leaving the MCP/DQ layer; ≤ 20 rows / 8 KB per tool result (NFR-006). |
| OQ-09 | Agent asks a human mid-RCA? | — | **Not in v1.** Missing evidence goes into `additional_investigation_needed`. |
| OQ-10 | Auto-close transient incidents? | S15 | **Operator dismissal only** (DETECTED/RCA_COMPLETED → REJECTED with reason). |
| OQ-11 | Separation of duties for approval? | S10 | **Configurable, off by default:** `REQUIRE_DISTINCT_APPROVER=false`. |
| OQ-12 | Proposed NFR, cost and retention values? | G1 | **Accepted as proposed.** The *(proposed)* markers in §6–§7 and §9 are now confirmed targets. |
| OQ-13 | Upload formats and limits? | S27 | **CSV, Parquet, JSON, XLSX; 500 MB; 20M rows**; converted to Parquet on ingest. |
| OQ-14 | External model uploads? | S27, S33 | **No.** Only service-trained, HMAC-signed models in v1. |
| OQ-15 | Source of supervised labels? | S33 | **Label column in uploaded datasets.** |
| OQ-16 | Port existing DQ agent code? | S26–S33 | **No existing code — build from the specs.** |
| OQ-17 | MongoDB collections as DQ datasets? | S26 | **Not in v1** (files, PostgreSQL, MySQL, S3 objects). MongoDB is still covered by MCP (S24). |
| OQ-18 | Data volumes? | S26, S31 | **Up to ~10M rows per table**; bucketed recon above 1M rows; samples ≤ 100k for statistics/ML. |
