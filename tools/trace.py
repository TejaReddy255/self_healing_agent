"""Spec acceptance-criteria ↔ test traceability (S01-AC9).

Usage::

    python tools/trace.py [--strict] [--specs-dir docs/specs] [--tests-dir tests]

An AC counts as covered when a test is marked ``@pytest.mark.spec("Sxx-ACn")`` or the spec's
"Verification evidence" section contains ``runtime-verified: Sxx-ACn``. With ``--strict`` the
exit code is 1 if any spec whose status is DONE has an uncovered AC.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

AC_ID = re.compile(r"\bS\d{2}-AC\d+\b")
AC_DEFINITION = re.compile(r"\*\*(S\d{2}-AC\d+)\*\*")
STATUS_LINE = re.compile(r"^status:\s*([A-Z_]+)", re.MULTILINE)
RUNTIME_VERIFIED = re.compile(r"runtime-verified:\s*((?:S\d{2}-AC\d+[\s,]*)+)")
EVIDENCE_HEADING = "## Verification evidence"


@dataclass
class Spec:
    spec_id: str
    path: Path
    status: str
    acs: list[str]
    runtime_verified: set[str] = field(default_factory=set)


def _front_matter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[3:end]
    return ""


def parse_spec(path: Path) -> Spec:
    text = path.read_text(encoding="utf-8")
    status_match = STATUS_LINE.search(_front_matter(text))
    evidence = text.split(EVIDENCE_HEADING, 1)[1] if EVIDENCE_HEADING in text else ""
    runtime: set[str] = set()
    for match in RUNTIME_VERIFIED.finditer(evidence):
        runtime.update(AC_ID.findall(match.group(1)))
    return Spec(
        spec_id=path.name.split("-", 1)[0],
        path=path,
        status=status_match.group(1) if status_match else "UNKNOWN",
        acs=list(dict.fromkeys(AC_DEFINITION.findall(text))),
        runtime_verified=runtime,
    )


def load_specs(specs_dir: Path) -> list[Spec]:
    return [parse_spec(p) for p in sorted(specs_dir.glob("S[0-9][0-9]-*.md"))]


def _is_spec_marker(call: ast.Call) -> bool:
    """True for ``pytest.mark.spec(...)`` / ``mark.spec(...)`` calls."""
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "spec"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "mark"
    )


def scan_tests(tests_dir: Path) -> dict[str, list[str]]:
    """Map AC id → ["path:line", ...] for every real ``mark.spec(...)`` call under ``tests_dir``.

    Files are parsed with ``ast`` so markers inside comments or string literals do not count.
    """
    coverage: dict[str, list[str]] = defaultdict(list)
    for path in sorted(tests_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _is_spec_marker(node)):
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    for ac in AC_ID.findall(arg.value):
                        coverage[ac].append(f"{path.as_posix()}:{node.lineno}")
    return coverage


def uncovered(spec: Spec, coverage: dict[str, list[str]]) -> list[str]:
    return [ac for ac in spec.acs if ac not in coverage and ac not in spec.runtime_verified]


def render(specs: list[Spec], coverage: dict[str, list[str]]) -> str:
    lines = [f"{'AC':<12} {'spec status':<12} coverage"]
    for spec in specs:
        for ac in spec.acs:
            refs = coverage.get(ac, [])
            if refs:
                where = f"{len(refs)} test(s): {refs[0]}" + (" ..." if len(refs) > 1 else "")
            elif ac in spec.runtime_verified:
                where = "runtime-verified"
            else:
                where = "UNCOVERED"
            lines.append(f"{ac:<12} {spec.status:<12} {where}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--strict", action="store_true", help="fail on uncovered ACs of DONE specs")
    parser.add_argument("--specs-dir", type=Path, default=Path("docs/specs"))
    parser.add_argument("--tests-dir", type=Path, default=Path("tests"))
    args = parser.parse_args(argv)

    specs = load_specs(args.specs_dir)
    coverage = scan_tests(args.tests_dir)
    print(render(specs, coverage))

    failures = {s.spec_id: uncovered(s, coverage) for s in specs if s.status == "DONE"}
    failures = {k: v for k, v in failures.items() if v}
    if failures:
        for spec_id, acs in failures.items():
            print(f"{spec_id} is DONE but has uncovered ACs: {', '.join(acs)}", file=sys.stderr)
        if args.strict:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
