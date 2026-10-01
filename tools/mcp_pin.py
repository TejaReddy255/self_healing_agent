"""Print tool-permission registry entries (with pins) for a running MCP server (S06).

    uv run python tools/mcp_pin.py http://127.0.0.1:8001/mcp postgresql

The output is a starting point for a reviewed change to ``config/tool_permissions.yaml``. Each
entry's ``access`` is printed as ``REVIEW`` (which the registry rejects): a human must decide read
or write. Pins (``definition_sha256``) cover the tool's name, description, input schema and
annotations, so any later change on the server excludes the tool until it is reviewed again.
"""

from __future__ import annotations

import asyncio
import sys

import yaml

from backend.domain.common import SystemName
from backend.mcp.connector import SdkMCPConnector
from backend.mcp.permissions import definition_sha256, looks_write_capable
from backend.mcp.settings import ServerConfig


async def entries(url: str, server: SystemName) -> list[dict[str, object]]:
    config = ServerConfig(
        name=server, url=url, enabled=True, required=False, module="mcp_servers.x"
    )
    specs = await SdkMCPConnector(probe_timeout_s=5).list_tools(config)
    result: list[dict[str, object]] = []
    for spec in sorted(specs, key=lambda s: s.name):
        hints = []
        if spec.read_only_hint is True:
            hints.append("server says readOnlyHint")
        if spec.destructive_hint is True:
            hints.append("server says destructiveHint")
        if looks_write_capable(spec.name):
            hints.append("name looks write-capable")
        result.append(
            {
                "server": server.value,
                "tool": spec.name,
                "access": "REVIEW",
                "definition_sha256": definition_sha256(spec),
                "notes": "; ".join(hints) or "no hints",
            }
        )
    return result


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[1] not in {s.value for s in SystemName}:
        print(
            "usage: python tools/mcp_pin.py <mcp-url> <postgresql|mysql|s3|mongodb>",
            file=sys.stderr,
        )
        return 2
    found = asyncio.run(entries(args[0], SystemName(args[1])))
    print(yaml.safe_dump({"tools": found}, sort_keys=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
