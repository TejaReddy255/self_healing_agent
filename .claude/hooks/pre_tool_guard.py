from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def emit_ask(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }))


def emit_deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    tool = str(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input") or {}

    # File operations: protect common secret-bearing files.
    if tool in {"Write", "Edit"}:
        raw_path = str(tool_input.get("file_path", ""))
        name = Path(raw_path).name.lower()
        sensitive_names = {
            ".env", ".env.production", ".env.prod",
            "id_rsa", "id_ed25519", "credentials.json",
            "service-account.json", "service_account.json",
        }
        if name in sensitive_names or name.endswith(".pem") or name.endswith(".key"):
            emit_ask(
                f"Modification of sensitive file '{name}' requires explicit confirmation. "
                "Prefer secret-manager/environment injection and avoid committing credentials."
            )
            return 0

    if tool not in {"Bash", "PowerShell"}:
        return 0

    command = str(tool_input.get("command", ""))
    lowered = command.lower()

    # Clearly catastrophic commands are denied.
    deny_patterns = [
        r"rm\s+-rf\s+/(?:\s|$)",
        r"rm\s+-rf\s+/\*",
        r"mkfs(?:\.|\s)",
        r":\(\)\s*\{\s*:\|:&\s*\};:",
    ]
    if any(re.search(p, command, flags=re.IGNORECASE) for p in deny_patterns):
        emit_deny("Catastrophic destructive command blocked by project hook.")
        return 0

    # High-impact actions require user confirmation.
    ask_patterns = [
        r"\bgit\s+reset\s+--hard\b",
        r"\bgit\s+clean\s+-[a-z]*f",
        r"\bgit\s+push\b.*\s--force(?:-with-lease)?\b",
        r"\bdrop\s+database\b",
        r"\bdrop\s+schema\b",
        r"\btruncate\s+table\b",
        r"\bdelete\s+from\b(?!.*\bwhere\b)",
        r"\bkubectl\s+delete\b",
        r"\bterraform\s+destroy\b",
        r"\baws\b.*\bdelete-",
        r"\bremove-item\b.*-recurse",
    ]
    if any(re.search(p, command, flags=re.IGNORECASE | re.DOTALL) for p in ask_patterns):
        emit_ask(
            "This command appears destructive or high-impact. Confirm scope, environment, "
            "backup/rollback, and that the target is not production before proceeding."
        )
        return 0

    # Warn before commands that may expose environment secrets.
    secret_exposure = [
        r"\bprintenv\b",
        r"\benv\b\s*$",
        r"\bset\b\s*$",
        r"\bcat\b.*(?:\.env|credentials|secret|token|\.pem|\.key)",
        r"\btype\b.*(?:\.env|credentials|secret|token|\.pem|\.key)",
    ]
    if any(re.search(p, lowered, flags=re.IGNORECASE) for p in secret_exposure):
        emit_ask(
            "Command may expose secrets to the model/transcript. Confirm that secret values "
            "are necessary and will be redacted."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
