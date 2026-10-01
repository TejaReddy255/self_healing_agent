---
id: S24
title: S3 and MongoDB MCP servers
status: DRAFT
phase: 1-6 (parallel track)
owner_agent: backend-engineer
reviewers: [code-reviewer, security-reviewer]
depends_on: [S06]
blocked_by: []                      # OQ-02 default applies (build in-repo)
traces: [FR-023, NFR-006]
source_sections: ["src §8"]
tags: [security-sensitive]
approved_by:
approved_on:
---

# S24 — S3 and MongoDB MCP servers

## Objective
Complete the four-system MCP coverage from src §8 with the same safety properties as S07.

## In scope
- `mcp_servers/s3/` (port 8003): read `list_objects(prefix)`, `head_object`, `get_object_sample(max_bytes)`,
  `object_count(prefix)`; write `copy_object`, `put_object`, `delete_object`. Bucket and prefix allowlists.
  Read tools use read-only credentials. Writes require an explicit key (no wildcard delete).
- `mcp_servers/mongodb/` (port 8004): read `find(filter, projection, limit)`, `count(filter)`, `list_indexes`;
  write `update_many(filter, update, max_affected)`, `delete_many(filter, max_affected)`, `create_index`.
  A non-empty filter is required for writes. Exceeding `max_affected` aborts before the write (count first, inside a session/transaction where supported).
- Registry entries. Dev compose adds MinIO and MongoDB with seed data. Verification assertion kinds
  `object_exists`, `object_count` and `document_count` become available to plan validation (S19).

## Acceptance criteria
- **S24-AC1** Read tools cannot mutate (credential-level test against MinIO and Mongo with read-only users).
- **S24-AC2** S3 writes outside the allowlisted bucket/prefix are rejected. `delete_object` requires an exact key.
- **S24-AC3** Mongo writes with an empty filter are rejected. `max_affected` exceeded → no change, and the count is reported.
- **S24-AC4** Output size caps and PII masking are applied to samples and documents.
- **S24-AC5** Every discovered tool has a registry entry with the correct access class. `/mcp-status` shows the servers healthy in dev compose.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1–AC5 | integration (MinIO, Mongo in compose) | `tests/integration/mcp_servers/test_s3.py`, `test_mongodb.py` |

## Rollback
Stop the servers. Remove the registry entries.

## Open issues
- From S06 refinement: as for S07, add registry entries with `input_schema_sha256`, a `GET /health` endpoint and `mcp_servers.<name>` module entry points.
- From S06 review: as for S07, set `readOnlyHint` and `destructiveHint` annotations, generate pinned entries with `tools/mcp_pin.py`, name settings `MCP_*`, and enforce read-only access in the backing store for read tools.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-29 — created (DRAFT)
