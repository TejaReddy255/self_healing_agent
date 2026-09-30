---
id: S26
title: DQ service foundation — dq_agents core library, DQ service, jobs, connectors
status: DRAFT
phase: 4
owner_agent: data-engineer
reviewers: [code-reviewer, security-reviewer, devops-sre, solution-architect]
depends_on: [S01, S02, S03]
blocked_by: []
traces: [FR-120, FR-121, FR-122, FR-123, NFR-070, NFR-071, NFR-074]
source_sections: ["src §17", "src §23.1", "src §35.4"]
tags: [runtime, security-sensitive, schema, contract]
approved_by:
approved_on:
---

# S26 — DQ service foundation

## Objective
Stand up the independent DQ service (ADR-013) and the `dq_agents` core library with everything the
seven capabilities share. That means the dataset references, read-only connectors, DuckDB engine,
safe-expression DSL, fingerprinting, masking, sampling, result contract, durable jobs and API skeleton.
Each agent slice then only adds algorithms.

## Read first
`docs/dq-agents-design.md` (all), `docs/architecture.md` ADR-004, ADR-013, ADR-015; S02 and S03 evidence.

## In scope
- `dq_agents/core/`: `DatasetRef` union, `DatasetFingerprint`, `AgentRunRequest`, `AgentRunResult`,
  `DQFinding`, `Agent` protocol (`name`, `version`, `operations`, `params_schema(op)`, `run(ctx) -> AgentRunResult`),
  `params_hash` (S03 canonicalization re-implemented locally — **no import from backend**; golden vectors shared as a test fixture).
- `dq_agents/core/expressions.py`: safe-expression DSL (dq-agents-design §9), `parse(expr) -> ValidatedExpr`,
  `compile(expr, dialect)` for postgres/mysql/duckdb.
- `dq_agents/core/masking.py`, `sampling.py` (reservoir/random with seed; DB-side sampling strategies per dialect),
  `fingerprint.py` (file: SHA-256; table: row count + `max(updated_at)` when declared, else a `snapshot_marker` timestamp).
- `dq_service/storage/`: `FileStorage` protocol (`put`, `open`, `exists`, `delete`, `path_for`) with `LocalVolumeStorage`
  rooted at `DQ_STORAGE_ROOT` (ADR-014, amended). Keys are validated and resolved strictly inside the root. Writes are
  atomic (temp file + rename).
- `dq_agents/io/`: `FileVersionReader` (`FileStorage` → DuckDB), `PostgresReader`, `MySQLReader`, `S3ObjectReader`.
  All are read-only (connections set `default_transaction_read_only` / `SET SESSION TRANSACTION READ ONLY`), with statement timeouts.
- `dq_agents/engine/duckdb.py`: per-job in-memory DuckDB with memory limit, `enable_external_access=false` for
  user expressions, and temp directory per job.
- `dq_service/`: FastAPI app (`/health`, `/health/ready`, `/v1/agents`, `/v1/jobs` submit/status/result),
  service-token auth, JSON logging with redaction (same rules as S01, re-implemented or vendored — no backend import),
  job queue (claim/lease/heartbeat/reaper; ADR-004 pattern), worker `python -m dq_service.worker`,
  migrations in `dq_service/migrations/` run with the S02 runner CLI using `--dir` and `--schema dq_service` (extend the S02 runner if needed).
- A registered no-op `echo` agent used to prove the pipeline end to end.
- `tests/dq/synthetic.py`: seeded synthetic dataset generator with injected-issue ground truth (nulls, duplicates,
  out-of-range, invalid formats, outliers, distribution shift, anomalies), used by S27–S33 and the E-DQ evals.
- Compose: `dq-service`, `dq-worker`, a named volume `dq-storage` mounted by both at `DQ_STORAGE_ROOT`, and the `dq_service` schema.
- Extend CI (ruff, mypy, pytest) and `CLAUDE.md` Commands to cover `dq_agents/` and `dq_service/`. Add DQ dependencies
  (duckdb, sqlglot, pyarrow, numpy, scipy, scikit-learn, jinja2, openpyxl) to a separate dependency group so the
  backend image does not ship them.
- `backend/dq/client.py` `DQServiceClient` (httpx; submit, poll with backoff, fetch result; timeouts; typed errors).

## Out of scope
Agent algorithms (S28–S33), File Manager features beyond the storage primitives (S27), backend gateway and UI (S34).

## Configuration
DQ service: `DQ_SERVICE_TOKEN_SHA256`, `DQ_DB_DSN` (secret), `DQ_CONNECTIONS` (secret map of read-only DSNs),
`DQ_STORAGE_ROOT` (required; e.g. `/data/dq-storage` in containers), `DQ_MAX_SAMPLE_ROWS=100000`, `DQ_JOB_TIMEOUT_S=900`,
`DQ_DUCKDB_MEMORY_LIMIT=2GB`, `DQ_WORKER_CONCURRENCY=2`.
Backend: `DQ_SERVICE_URL`, `DQ_SERVICE_TOKEN` (secret), `DQ_CLIENT_TIMEOUT_S=30`.

## Acceptance criteria
- **S26-AC1** `dq_service` and `dq_agents` import nothing from `backend`, and `backend` imports nothing from them (import-lint test both ways).
- **S26-AC2** `POST /v1/jobs` with the `echo` agent → 202. The worker processes it and `GET /v1/jobs/{id}/result` returns a valid `AgentRunResult` with input fingerprints, `params_hash`, seed and `agent_version`.
- **S26-AC3** The same `request_key` submitted twice → one job. A killed worker → the job is reclaimed after lease expiry. `max_attempts` exceeded → ERROR with a message.
- **S26-AC4** The expression DSL accepts allowed constructs and rejects subqueries, DDL/DML, non-allowlisted functions, `read_csv`-style table functions and multiple statements (table-driven, ≥ 25 cases). It compiles identical semantics to postgres/mysql/duckdb (a result-equivalence test on a fixture table).
- **S26-AC5** Readers are read-only: an attempted write through any connector fails at the database level. Statement timeouts are enforced.
- **S26-AC6** DuckDB user-expression execution cannot read arbitrary local files or URLs (test attempts `read_csv('C:/…')` and an http URL → rejected).
- **S26-AC7** Masking replaces configured PII columns in samples and previews. Sampling with the same seed returns identical rows.
- **S26-AC8** Unauthenticated or wrong-token calls to the DQ service → 401. Logs contain no DSNs or tokens.
- **S26-AC9** The `DQServiceClient` handles 202 → polling → result, timeouts (typed `DQServiceUnavailable`) and ERROR jobs, tested against a respx fake of the DQ API.
- **S26-AC10** The synthetic generator is deterministic per seed and emits a ground-truth manifest.
- **S26-AC11** `LocalVolumeStorage` rejects keys that escape `DQ_STORAGE_ROOT` (`..`, absolute paths, drive letters,
  symlinks pointing outside), and a write interrupted mid-way never leaves a partial file under its final key.
  Readiness reports the storage root as down when it is missing or not writable.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1 | unit | `tests/unit/test_import_boundaries.py` |
| AC2, AC3, AC8 | integration | `tests/integration/dq_service/test_jobs.py` |
| AC4, AC6 | unit | `tests/unit/dq_agents/test_expressions.py`, `test_duckdb_sandbox.py` |
| AC5 | integration (compose DBs) | `tests/integration/dq_agents/test_readers_readonly.py` |
| AC7, AC10 | unit | `tests/unit/dq_agents/test_masking_sampling.py`, `test_synthetic.py` |
| AC9 | unit | `tests/unit/dq/test_dq_service_client.py` |

## Rollback
Stop `dq-service`/`dq-worker`. The backend degrades DQ readiness only.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-30 — created (DRAFT)
- 2026-09-30 — ADR-014 amended: local volume instead of MinIO. Added `FileStorage`/`LocalVolumeStorage`, `DQ_STORAGE_ROOT`, S26-AC11.
