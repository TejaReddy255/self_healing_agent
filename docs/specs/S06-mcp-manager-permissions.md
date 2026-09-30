---
id: S06
title: MCP manager and explicit tool-permission registry
status: DRAFT
phase: 1
owner_agent: backend-engineer
reviewers: [code-reviewer, security-reviewer]
depends_on: [S02, S03]
blocked_by: []
traces: [FR-020, FR-021, FR-022, FR-024, NFR-004, NFR-011]
source_sections: ["src §5.1 steps 34–36", "src §8", "src §8.1", "src §16 (heuristic classification)"]
tags: [security-sensitive]
approved_by:
approved_on:
---

# S06 — MCP manager and tool-permission registry

## Objective
Discover MCP tools per server and classify them through an explicit, default-deny registry
(ADR-005). Compose the diagnostic (read-only) and execution (target-scoped) toolsets deterministically.
Audit every invocation.

## In scope
- `backend/mcp/config.py`: servers `postgresql, mysql, s3, mongodb` with `url` (defaults
  `http://127.0.0.1:800{1..4}/mcp` per src §8), optional `script`, `required: bool`.
- `backend/mcp/manager.py` `MCPManager`: `probe(server)`, `start_local(server)` (only when
  `MCP_AUTOSTART=true` and `ENV != prod`), `discover()` (per-server isolation; one failure doesn't block others),
  `shutdown()` terminating started subprocesses.
- `backend/mcp/permissions.py` `ToolPermissionRegistry` loaded from `config/tool_permissions.yaml`:
  entries `{server, tool, access: read|write, timeout_s?, max_output_bytes?, notes}`. Classification:
  registry → `READ|WRITE`; not in registry → `UNCLASSIFIED`. If a registry `read` tool's name matches
  the write heuristic (`execute|insert|update|delete|drop|create|alter|put|upload|copy|remove|truncate|merge|grant`)
  → `MISMATCH` (excluded, warning).
- `backend/mcp/toolsets.py`: `diagnostic_toolset() -> list[ToolHandle]` (READ tools on healthy servers),
  `execution_toolset(target: SystemName) -> list[ToolHandle]` (READ + WRITE tools of that server only;
  error if the server is unhealthy or has 0 WRITE tools, per src §8.1).
- `backend/mcp/invoke.py`: `ToolHandle.invoke(args, ctx)` with timeout, output-size cap and audit
  start/end (S02 `AuditService`, `phase` from ctx), plus correlation IDs.
- `GET /mcp-status`: per server `{url_alias, healthy, started_by_us, tools: {read, write, unclassified, mismatch}}`
  plus `diagnostic_write_tools_blocked` count. Readiness component `mcp` (degraded if a non-required server is down).
- `tests/fakes/mcp.py`: in-process fake MCP server(s) with configurable tools.

## Out of scope
Actual MCP servers (S07, S24), agent wiring (S08), write guard (S11).

## Configuration
`MCP_<SYSTEM>_URL`, `MCP_<SYSTEM>_SCRIPT`, `MCP_<SYSTEM>_REQUIRED`, `MCP_AUTOSTART=false`,
`MCP_DEFAULT_TIMEOUT_S=60`, `MCP_MAX_OUTPUT_BYTES=8192`, `TOOL_PERMISSIONS_FILE=config/tool_permissions.yaml`.

## Acceptance criteria
- **S06-AC1** Tools from a fake server are discovered and classified per the registry.
- **S06-AC2** A tool absent from the registry is UNCLASSIFIED, is excluded from both toolsets, and is listed in `/mcp-status`.
- **S06-AC3** A registry `read` tool whose name matches the write heuristic is MISMATCH, excluded, and logged as a warning.
- **S06-AC4** `diagnostic_toolset()` never contains a WRITE, UNCLASSIFIED or MISMATCH tool (property-based test over generated registries and tool lists).
- **S06-AC5** `execution_toolset("mysql")` contains only mysql tools. An invalid target, an unhealthy target, or a target with zero WRITE tools raises a typed error.
- **S06-AC6** With one server down, the others are still discovered. Readiness is degraded, not failed, unless the down server is `required`.
- **S06-AC7** Autostarted subprocesses terminate on shutdown. Autostart is refused when `ENV=prod`.
- **S06-AC8** Every invocation writes start and end audit rows with status, duration, redacted args and a truncated output. A timeout produces an audited `TIMEOUT` status.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1–AC5 | unit | `tests/unit/mcp/test_registry_toolsets.py` (hypothesis for AC4) |
| AC6, AC7 | integration | `tests/integration/mcp/test_manager.py` |
| AC8 | integration | `tests/integration/mcp/test_invoke_audit.py` |

## Security and eval considerations
This is the primary privilege boundary. `security-reviewer` must confirm there is no path from diagnostic code to `execution_toolset`
(static check: only `backend/services/remediation*` and `backend/jobs/remediation*` may import it).

## Rollback
Revert code.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-29 — created (DRAFT)
