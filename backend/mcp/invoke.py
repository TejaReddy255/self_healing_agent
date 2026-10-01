"""Tool handles and audited invocation (S06).

Every call writes a start row *before* contacting the server (so a crash during a write still
leaves evidence) and an end row after it. READ tools retry transport failures; WRITE tools are
attempted exactly once (NFR-011).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from backend.domain.audit import AuditEvent
from backend.mcp.connector import MCPConnector
from backend.mcp.errors import ToolError, ToolUnavailable
from backend.mcp.models import ClassifiedTool, InvocationContext, ToolClass, ToolOutput
from backend.mcp.settings import ServerConfig

logger = logging.getLogger(__name__)

TRUNCATION_MARKER = "\n[… truncated {count} bytes …]"


class AuditSink(Protocol):
    async def record(self, event: AuditEvent) -> int: ...


def cap_output(text: str, max_bytes: int) -> tuple[str, bool, int]:
    """Cut ``text`` to ``max_bytes`` of UTF-8 at a character boundary, with a marker."""
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text, False, len(data)
    kept = data[:max_bytes].decode("utf-8", errors="ignore")
    return kept + TRUNCATION_MARKER.format(count=len(data) - len(kept.encode())), True, len(data)


@dataclass(frozen=True)
class ToolHandle:
    """One usable tool, bound to its server. Immutable; built only by the toolset functions."""

    tool: ClassifiedTool
    server: ServerConfig
    connector: MCPConnector
    audit: AuditSink | None
    read_retries: int

    @property
    def name(self) -> str:
        return self.tool.qualified_name

    @property
    def access(self) -> ToolClass:
        return self.tool.tool_class

    @property
    def description(self) -> str:
        return self.tool.spec.description

    @property
    def input_schema(self) -> Mapping[str, Any]:
        return self.tool.spec.input_schema

    async def invoke(self, arguments: Mapping[str, Any], ctx: InvocationContext) -> ToolOutput:
        started = time.monotonic()
        await self._audit(ctx, "STARTED", arguments, None, None)
        attempts = 1 + (self.read_retries if self.access is ToolClass.READ else 0)
        try:
            for attempt in range(attempts):
                try:
                    result = await self.connector.call_tool(
                        self.server, self.tool.spec.name, arguments, self.tool.timeout_s
                    )
                    break
                except ToolUnavailable:
                    if attempt + 1 >= attempts:
                        raise
                    logger.warning("mcp read tool unavailable; retrying", extra={"tool": self.name})
        except ToolError as exc:
            await self._audit(ctx, exc.status, arguments, None, started)
            raise
        except Exception:
            await self._audit(ctx, "OUTCOME_UNKNOWN", arguments, None, started)
            raise
        content, truncated, size = cap_output(result.content_text, self.tool.max_output_bytes)
        duration_ms = int((time.monotonic() - started) * 1000)
        await self._audit(
            ctx, "OUTPUT_TRUNCATED" if truncated else "OK", arguments, content, started
        )
        return ToolOutput(
            tool=self.name,
            content=content,
            truncated=truncated,
            original_bytes=size,
            duration_ms=duration_ms,
        )

    async def _audit(
        self,
        ctx: InvocationContext,
        status: str,
        arguments: Mapping[str, Any],
        output: str | None,
        started: float | None,
    ) -> None:
        if self.audit is None:
            return
        event = AuditEvent(
            phase=ctx.phase,
            event_type="TOOL_CALL",
            status=status,
            operation=self.access.value.lower(),
            tool_name=self.name,
            server=self.server.name.value,
            duration_ms=None if started is None else int((time.monotonic() - started) * 1000),
            actor_subject=ctx.actor_subject,
            actor_role=ctx.actor_role,
            request_id=ctx.request_id,
            pipeline_id=ctx.pipeline_id,
            dag_id=ctx.dag_id,
            run_id=ctx.run_id,
            analysis_id=ctx.analysis_id,
            review_id=ctx.review_id,
            job_id=ctx.job_id,
            incident_id=ctx.incident_id,
            input=dict(arguments),
            output=None if output is None else {"text": output},
        )
        try:
            await self.audit.record(event)
        except Exception:
            # An audit outage must not hide what happened, nor turn a completed write into an
            # error the caller might retry. Log loudly instead.
            logger.exception("mcp tool audit failed", extra={"tool": self.name, "status": status})
