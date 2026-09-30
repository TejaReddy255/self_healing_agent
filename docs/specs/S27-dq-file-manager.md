---
id: S27
title: File Manager — uploads, versions, preview, model registry, workbench presets
status: DRAFT
phase: 4
owner_agent: data-engineer
reviewers: [code-reviewer, security-reviewer]
depends_on: [S26]
blocked_by: []
traces: [FR-130, FR-131, FR-132, FR-133, FR-134, NFR-072]
source_sections: ["user DQ suite brief: File Manager (File Upload & Management, Pipeline Settings, File Preview)"]
tags: [security-sensitive, schema]
approved_by:
approved_on:
---

# S27 — File Manager

## Objective
Let analysts upload and version data files safely, preview them, keep a registry of service-trained
models, and save workbench analysis presets ("Pipeline Settings"). Governed pipeline configuration
stays in metadata (architecture C12).

## In scope
- `POST /v1/files` (multipart; new file or new version of `file_id`), `GET /v1/files` (filters, pagination),
  `GET /v1/files/{id}` (versions + lineage), `GET /v1/files/{id}/versions/{v}/preview?rows=50`, `DELETE /v1/files/{id}` (soft).
- Upload validation: size ≤ `DQ_MAX_UPLOAD_MB`; magic-byte and extension agreement; allowlist CSV (delimiter/encoding sniffing,
  UTF-8/UTF-16/latin-1), Parquet, JSON Lines/array, XLSX (first or named sheet; `openpyxl` read-only; decompressed-size cap);
  row and column caps; the file is converted to Parquet on ingest (canonical storage) with the original retained.
- Schema inference (numeric, integer, boolean, datetime, categorical, text, id-like) stored per version; row count; SHA-256.
- PII declaration at upload (`pii_columns`), editable in later versions; enforced by S26 masking.
- Model registry read APIs (`/v1/models…`). Writes happen only through the anomaly/statistical train operations (S33).
  Artifact signing/verification utilities (HMAC-SHA256 with `DQ_ARTIFACT_SIGNING_KEY`).
- Presets: `GET/POST/PUT /v1/presets` — `{name, description, steps: [{agent, operation, params}], version}`, validated
  against each agent's params schema. Versioned (PUT creates version N+1).
- Retention: soft-deleted files purged after `DQ_FILE_RETENTION_DAYS`, except versions referenced by a
  platform `dq_result`, a model version or a lineage chain.

## Acceptance criteria
- **S27-AC1** A valid CSV, Parquet, JSON and XLSX each upload to version 1 with schema, row count and SHA-256. Re-upload → version 2. Versions are immutable (no update path).
- **S27-AC2** Rejected: oversized files; extension/magic mismatch; unsupported types; an XLSX exceeding the decompressed-size cap (zip bomb fixture); files exceeding row/column caps. Each is rejected with a specific error code and no stored file.
- **S27-AC3** Preview returns ≤ the requested rows with PII columns masked, and inferred types per column.
- **S27-AC4** Soft delete hides the file from listings. Versions referenced by lineage or models are not purged. The purge job removes only eligible files from the storage volume (dry-run and real-run test).
- **S27-AC5** Presets reject steps with unknown agents/operations or params failing the agent schema. PUT creates a new version.
- **S27-AC6** A model artifact with a tampered signature is refused on load (`ArtifactSignatureError`). No endpoint accepts a user-uploaded model binary.
- **S27-AC7** CSV/XLSX preview and exports neutralize formula-injection prefixes.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1–AC4, AC7 | integration (temporary storage root) | `tests/integration/dq_service/test_files.py` |
| AC5 | unit | `tests/unit/dq_service/test_presets.py` |
| AC6 | unit | `tests/unit/dq_agents/test_artifact_signing.py` |

## Rollback
Revert. Files remain on the `dq-storage` volume. The schema is additive.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-30 — created (DRAFT)
