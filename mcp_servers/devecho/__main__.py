"""Run with ``python -m mcp_servers.devecho`` (port from MCP_DEVECHO_PORT, default 8099)."""

from __future__ import annotations

import os

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

server = MCPServer("devecho")
_notes: dict[str, str] = {}


READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)


@server.tool(annotations=READ_ONLY)
def echo(text: str) -> str:
    """Return the text unchanged."""
    return text


@server.tool(annotations=READ_ONLY)
def get_row_count(table: str) -> str:
    """Pretend row count, for exercising read paths."""
    return f"{table}: 42"


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True))
def set_note(key: str, value: str) -> str:
    """Store a note in process memory, for exercising write paths."""
    _notes[key] = value
    return f"stored {key}"


@server.tool(annotations=READ_ONLY)
def fail(reason: str) -> str:
    """Always fails, for exercising error paths."""
    raise ValueError(reason)


@server.custom_route("/health", methods=["GET"])  # type: ignore[untyped-decorator]
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "server": "devecho"})


def main() -> None:
    port = int(os.environ.get("MCP_DEVECHO_PORT", "8099"))
    server.run("streamable-http", host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
