"""Generate an API token and its token-file entry.

Usage::

    python tools/new_token.py <subject> <role>

Prints the plaintext token once (give it to the user; never store it) and the JSON entry to add
to the file referenced by ``API_TOKENS_FILE``. Only the SHA-256 digest goes into configuration.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sys

ROLES = ("viewer", "operator", "reviewer", "admin", "service")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[1] not in ROLES:
        print(f"usage: python tools/new_token.py <subject> <{'|'.join(ROLES)}>", file=sys.stderr)
        return 2
    subject, role = args
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    print(f"token (shown once): {token}")
    print("token-file entry:")
    print(json.dumps({digest: {"subject": subject, "role": role}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
