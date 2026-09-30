from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


CODE_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".go", ".rs",
    ".cs", ".cpp", ".c", ".kt", ".scala", ".sql"
}


def changed_code_files(project_dir: Path) -> list[str]:
    try:
        cp = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_dir,
            text=True,
            capture_output=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return []

    changed = []
    for line in cp.stdout.splitlines():
        if len(line) < 4:
            continue
        raw = line[3:].strip()
        if " -> " in raw:
            raw = raw.split(" -> ", 1)[1].strip()
        if Path(raw).suffix.lower() in CODE_SUFFIXES:
            changed.append(raw)
    return changed


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    # Avoid continuation loops if this hook has already caused a stop continuation.
    if payload.get("stop_hook_active"):
        return 0

    project_dir = Path(os.environ.get("CLAUDE_PROJECT_DIR", payload.get("cwd", ".")))
    changed = changed_code_files(project_dir)
    if not changed:
        return 0

    sample = ", ".join(changed[:6])
    if len(changed) > 6:
        sample += f", +{len(changed) - 6} more"

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "additionalContext": (
                "Code changes are present. Before finishing, ensure the response includes "
                "verification evidence (tests/lint/type-check/runtime verification as applicable), "
                "or explicitly state which checks were not run and why. "
                f"Changed code includes: {sample}"
            )
        }
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
