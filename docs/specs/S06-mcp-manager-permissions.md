---
id: S06
title: MCP manager and explicit tool-permission registry
status: IN_PROGRESS
phase: 1
owner_agent: backend-engineer
reviewers: [code-reviewer, security-reviewer]
depends_on: [S02, S03]
blocked_by: []
traces: [FR-020, FR-021, FR-022, FR-024, NFR-004, NFR-006, NFR-011]
source_sections: ["src §5.1 steps 34–36, 39", "src §8", "src §8.1", "src §16 (heuristic classification)"]
tags: [security-sensitive]
approved_by: product owner
approved_on: 2026-10-01
---

# S06 — MCP manager and tool-permission registry

## Objective
- Discover the tools on each MCP server and classify them through an explicit, default-deny registry (ADR-005), replacing the name heuristics that src §16 lists as a known gap.
- Compose the diagnostic toolset (read-only) and the execution toolset (one target system) deterministically, from configuration, never from model output.
- Audit every tool invocation.

**This slice is the primary privilege boundary for agents** (NFR-004, invariants 3 and 4).

## Read first
- `docs/architecture.md` §11 (toolsets, write guard), §13 (MCP timeouts and retries), ADR-002, ADR-005, ADR-012.
- `docs/agent-design.md` §6 (tool matrix).
- S02 `AuditService`, S01 readiness and redaction, S03 `SystemName`.

## In scope

### Configuration (`backend/mcp/settings.py`, pydantic-settings, `.env`)
| Key | Default | Notes |
|---|---|---|
| `MCP_<SYSTEM>_URL` (`POSTGRESQL`, `MYSQL`, `S3`, `MONGODB`) | `http://127.0.0.1:800{1..4}/mcp` (src §8) | http(s), with no credentials, query or fragment. Health is `GET <origin>/health`. |
| `MCP_<SYSTEM>_ENABLED` | `true` | A disabled server is not probed and contributes no tools. |
| `MCP_<SYSTEM>_REQUIRED` | `false` | A required server being down makes readiness `down`. |
| `MCP_<SYSTEM>_MODULE` | `mcp_servers.<system>` | The autostart target, launched as `python -m <module>`. It must match `^mcp_servers\.[a-z_]+$`: no arbitrary commands and no shell. |
| `MCP_AUTOSTART` | `false` | Starts unreachable local servers. **Refused when `ENV=prod`** (`ConfigurationError`). |
| `MCP_DEFAULT_TIMEOUT_S` | `60` | Per call. A registry entry may override it (≤ 600). |
| `MCP_MAX_OUTPUT_BYTES` | `8192` | Cap on what is returned to the caller (NFR-006); a registry entry may lower it. |
| `MCP_PROBE_TIMEOUT_S` | `3` | Health probe and connection timeout |
| `MCP_REFRESH_INTERVAL_S` | `60` | Background health and tool refresh; `0` disables it. |
| `MCP_READ_RETRIES` | `2` | Transport-error retries for READ tools only. **WRITE tools are never retried.** |
| `TOOL_PERMISSIONS_FILE` | `<repo>/config/tool_permissions.yaml` | Loaded once at startup. **Missing or invalid → startup fails** *(decision 2026-10-01)*. There is no hot reload: changes are a reviewed commit plus a restart. |

### Permission registry (`backend/mcp/permissions.py`, `config/tool_permissions.yaml`)
- **File shape:** `version: 1`, `tools: [{server, tool, access: read|write, timeout_s?, max_output_bytes?, input_schema_sha256?, notes}]`.
- **Validation:** the file is safe-YAML loaded and Pydantic-validated, rejecting unknown keys, duplicate `(server, tool)` pairs, unknown servers and timeouts over 600 s.
- **Classification** is per `(server, tool)`, using `ToolClass`:
  - **`READ` / `WRITE`:** the registry entry's access, when everything below holds.
  - **`UNCLASSIFIED`:** the tool is not in the registry.
  - **`MISMATCH`:** a registry `read` entry whose name matches the write heuristic `(?i)(execute|insert|update|delete|drop|create|alter|put|upload|copy|remove|truncate|merge|grant|write|set)`. The tool is excluded and a warning is logged. The heuristic is only a tripwire, never the classifier.
  - **`SCHEMA_CHANGED`:** the entry pins `input_schema_sha256` and the discovered input schema hashes differently (SHA-256 of canonical JSON). The tool is excluded, which guards against a server silently changing what a vetted tool accepts.
- `config/tool_permissions.yaml` starts with `version: 1` and an empty `tools: []`. S07 and S24 add entries for their tools.

### MCP connection seam (`backend/mcp/connector.py`, ADR-012)
- **`MCPConnector` Protocol:**
  - `health(server) -> ServerHealth`;
  - `list_tools(server) -> list[ToolSpec{name, description, input_schema: JsonObject}]`;
  - `call_tool(server, name, arguments, timeout_s) -> ToolCallResult{content_text, is_error, raw_bytes}`.
- **`SdkMCPConnector`** uses the official `mcp` Python SDK client over streamable HTTP (ADR-002), with the configured timeouts and no redirects. It is a new runtime dependency, pinned in `uv.lock`.
- **`tests/fakes/mcp.py`:**
  - `FakeMCPConnector`, an in-memory connector with configurable servers, tools, health, results, delays and errors, which records calls;
  - `tests/fakes/mcp_server.py`, a tiny FastMCP server used by the integration tests (real SDK path and autostart).

### Manager (`backend/mcp/manager.py`, `MCPManager`)
- **`probe(server)`:** a health check with `MCP_PROBE_TIMEOUT_S`.
- **`start_local(server)`:** only with `MCP_AUTOSTART=true` and `ENV != prod`, and only for an enabled, unreachable server whose URL host is loopback. It runs `[sys.executable, "-m", module]` with no shell and its own process group, then waits up to 15 s for health.
- **`discover()`:** probes and lists tools per server, isolated so one failure never blocks the others, then classifies through the registry. It produces an immutable `MCPSnapshot{servers: {name: ServerState{healthy, started_by_us, checked_at, error?, tools: {name: ClassifiedTool}}}}`, swapped atomically.
- **`refresh()`:** `discover()` on an interval while the app runs.
- **`shutdown()`:** terminates the subprocesses it started (SIGTERM, then kill after 5 s) and never touches anything else.
- Startup runs `discover()` once, so a server that is down does not block startup.

### Toolsets (`backend/mcp/toolsets.py`)
- **`diagnostic_toolset(snapshot) -> tuple[ToolHandle, ...]`:** only `READ` tools on healthy, enabled servers.
- **`execution_toolset(snapshot, target: SystemName) -> tuple[ToolHandle, ...]`:** the `READ` and `WRITE` tools of the `target` server only. It raises:
  - `InvalidTarget` when the target is not a configured, enabled system;
  - `TargetUnavailable` when the server is unhealthy;
  - `NoWriteTools` when the server has zero `WRITE` tools (src §8.1).
- `ToolHandle` names are `"{server}.{tool}"` (e.g. `postgresql.run_select`), which keeps tools with the same name on different servers apart (agent-design §6). Handles are immutable. Toolsets are fixed when built and depend only on configuration, the registry and discovery, never on model output.
- **Import boundary:** only `backend/services/remediation*` and `backend/jobs/remediation*` may import `execution_toolset` (AC9).

### Invocation (`backend/mcp/invoke.py`)
`ToolHandle.invoke(arguments: JsonObject, ctx: InvocationContext) -> ToolOutput`:
- **Context:** `InvocationContext` = `{phase, actor?, request_id?, analysis_id?, review_id?, job_id?, incident_id?, pipeline_id?, dag_id?, run_id?}`.
- **Audit rows:**
  - a `TOOL_CALL` **start** row (status `STARTED`) is written *before* the call, so a crash mid-write still leaves evidence;
  - an **end** row records `OK`, `TOOL_ERROR`, `TIMEOUT`, `UNAVAILABLE` or `OUTPUT_TRUNCATED`, with `duration_ms`.
  - Both carry `tool_name`, `server`, the correlation IDs, redacted `input` (S02 `AuditService`) and the truncated output.
- **Timeout:** the registry `timeout_s`, or `MCP_DEFAULT_TIMEOUT_S`. A timeout gives `ToolTimeout`, audited as `TIMEOUT`.
- **Output:** text over `max_output_bytes` is cut at a UTF-8 boundary with a `[… truncated N bytes …]` marker and `truncated=true`.
- **Retries:** READ tools retry transport errors up to `MCP_READ_RETRIES` times. WRITE tools are attempted exactly once (NFR-011).
- **Errors:** `ToolTimeout`, `ToolUnavailable`, `ToolCallError` (the server returned `isError`), all subclasses of `ToolError`. Messages never echo arguments or server stack traces.

### API and readiness
- **`GET /mcp-status`** (viewer) returns:
  - `{servers: [{name, url_alias (host:port only), enabled, required, healthy, started_by_us, checked_at, error?, counts: {read, write, unclassified, mismatch, schema_changed}, unclassified: [tool], mismatch: [tool], schema_changed: [tool]}], diagnostic_write_tools_blocked}`;
  - `diagnostic_write_tools_blocked` is the number of discovered tools kept out of the diagnostic toolset because they are WRITE, UNCLASSIFIED, MISMATCH or SCHEMA_CHANGED.
- **Readiness component `mcp`:**
  - `ok` when every enabled server is healthy;
  - `degraded` when a non-required server is down, or when none are enabled;
  - `down` when a required server is down.

  It reads the cached snapshot and never calls MCP inline.

## Out of scope
- The MCP servers themselves (S07, S24) and their registry entries.
- Agent tool wrapping and `TOOL_NOT_ALLOWED` (S08).
- The write guard (S11) and verification's use of read tools (S19).

## Public contracts
- Exports for S07, S08, S09, S11, S14, S19 and S24: `MCPConnector`, `SdkMCPConnector`, `MCPManager`, `MCPSnapshot`, `ToolClass`, `ClassifiedTool`, `ToolPermissionRegistry`, `ToolHandle`, `InvocationContext`, `ToolOutput`, `diagnostic_toolset`, `execution_toolset`, and the error types above.
- `FakeMCPConnector` for tests.

## Persistence and migrations
None. Audit rows use `operation_audit` (S02).

## Acceptance criteria
- **S06-AC1** Tools from a fake server are discovered and classified per the registry. With the FastMCP test server, the real SDK path lists and calls a tool.
- **S06-AC2** A tool absent from the registry is UNCLASSIFIED, is excluded from both toolsets, and is listed by name in `/mcp-status`.
- **S06-AC3** A registry `read` entry whose name matches the write heuristic is MISMATCH: excluded from both toolsets, logged as a warning, and listed in `/mcp-status`. A pinned `input_schema_sha256` that differs from the discovered schema is SCHEMA_CHANGED and excluded.
- **S06-AC4** `diagnostic_toolset()` never contains a WRITE, UNCLASSIFIED, MISMATCH or SCHEMA_CHANGED tool, or a tool from an unhealthy or disabled server. This is a property-based test (hypothesis) over generated registries, discovered tool lists and server health.
- **S06-AC5** `execution_toolset("mysql")` contains only `mysql.*` tools. An invalid or disabled target raises `InvalidTarget`, an unhealthy one `TargetUnavailable`, and one with zero WRITE tools `NoWriteTools`.
- **S06-AC6** With one server down, the others are still discovered and usable. Readiness `mcp` is `degraded`, or `down` only when the down server is `required`. Readiness reads the cached snapshot and makes no MCP call.
- **S06-AC7** Autostart launches only `python -m mcp_servers.<name>` for loopback URLs. Started subprocesses terminate on shutdown, and processes not started by the manager are untouched. `MCP_AUTOSTART=true` with `ENV=prod` fails at startup.
- **S06-AC8** Every invocation writes a start row before the call and an end row after it, with status, duration, correlation IDs, redacted arguments and truncated output. A timeout gives an audited `TIMEOUT`. A READ tool retries transport errors up to `MCP_READ_RETRIES`, and a WRITE tool is attempted exactly once.
- **S06-AC9** *(new; from the security consideration)* An import-boundary test fails if any module other than `backend/services/remediation*` and `backend/jobs/remediation*` imports `execution_toolset`. `backend/monitoring`, `backend/agents` (diagnostic) and `backend/api` cannot reach it.
- **S06-AC10** *(new, decision 2026-10-01)* A missing or invalid `TOOL_PERMISSIONS_FILE` stops startup with a `ConfigurationError` naming the problem. Cases: missing file, YAML error, unknown key, duplicate `(server, tool)`, unknown server, unsafe YAML tag.
- **S06-AC11** *(new)* Output beyond `max_output_bytes` is truncated at a UTF-8 boundary with a marker and `truncated=true`. Tool error messages, `/mcp-status` and logs never contain call arguments, credentials or server stack traces.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1–AC5, AC10 | unit | `tests/unit/mcp/test_registry.py`, `tests/unit/mcp/test_toolsets.py` (hypothesis for AC4) |
| AC6, AC8, AC11 | unit (fake connector, recording audit) | `tests/unit/mcp/test_manager.py`, `tests/unit/mcp/test_invoke.py`, `tests/unit/api/test_mcp_status.py` |
| AC1 (SDK path), AC7 | integration (FastMCP test server subprocess) | `tests/integration/mcp/test_manager_sdk.py` |
| AC8 (audit rows in DB) | integration | `tests/integration/mcp/test_invoke_audit.py` |
| AC9 | unit | `tests/unit/test_import_boundaries.py` |

## Runtime verification
- Start the API with `MCP_AUTOSTART=true` and the test FastMCP server registered, and check `/mcp-status` and readiness.
- Stop the server and check that readiness goes `degraded`.
- Real PostgreSQL and MySQL servers arrive in S07.

## Security and eval considerations
- **Primary privilege boundary:**
  - default deny, with the registry as the only classifier; heuristics only exclude;
  - toolsets fixed at build time from configuration;
  - `execution_toolset` reachable only from remediation code (AC9);
  - writes never retried, and start rows written before writes.
- **Autostart:** a module allowlist, no shell, loopback only, never in prod.
- **Rug-pull guard:** schema pinning catches a server changing a vetted tool's input shape.
- `security-reviewer` reviews the implementation.

## Rollback
Revert the code. Audit rows remain.

## Open issues
- **Decided 2026-10-01 (product owner):** an invalid or missing registry fails startup (AC10).
- **Cross-slice:**
  - **S07, S24:** add registry entries (with `input_schema_sha256`) for every tool; their servers expose `GET /health` and an `mcp_servers.<name>` module entry point.
  - **S08:** wraps `ToolHandle` and reports out-of-set calls as `TOOL_NOT_ALLOWED`.
  - **S11:** builds the write guard around `execution_toolset`.
  - **S14:** the MCP monitor reads `MCPSnapshot` health.
  - **S19:** uses READ handles from the target server for assertions.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-29 — created (DRAFT)
- 2026-10-01 — refined via `/slice-spec`.
  - Concrete settings, registry schema with schema pinning, the connector seam on the official `mcp` SDK, a manager with snapshot and refresh, and an autostart allowlist.
  - Toolset naming and errors; invocation audit (start before call), retries and output caps; status and readiness shapes.
  - AC1–AC8 sharpened (IDs kept); AC9–AC11 added.
  - Product owner decided that an invalid registry fails startup.
  - Status READY.
- 2026-10-01 — approved by product owner ("approve S06 and implement it"). Status IN_PROGRESS.
