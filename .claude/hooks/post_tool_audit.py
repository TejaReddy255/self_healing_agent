from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    project_dir = Path(os.environ.get("CLAUDE_PROJECT_DIR", payload.get("cwd", ".")))
    audit_dir = project_dir / ".claude" / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)

    tool_input = payload.get("tool_input") or {}
    # Store metadata only. Do not persist full commands/file contents/tool responses.
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": payload.get("session_id"),
        "tool_use_id": payload.get("tool_use_id"),
        "tool": payload.get("tool_name"),
        "duration_ms": payload.get("duration_ms"),
        "cwd": payload.get("cwd"),
        "file_path": tool_input.get("file_path"),
    }

    with (audit_dir / "events.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
