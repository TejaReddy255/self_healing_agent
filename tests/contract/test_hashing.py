from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from backend.domain.hashing import CanonicalizationError, canonical_json, payload_hash
from backend.domain.review import ApprovalPayload

VECTORS_FILE = Path(__file__).parent / "hash_vectors.json"
VECTORS: list[dict[str, Any]] = json.loads(VECTORS_FILE.read_text(encoding="utf-8"))["vectors"]
REPO_ROOT = Path(__file__).resolve().parents[2]

# Leaves whose value cannot change on its own without changing the payload's structure.
STRUCTURAL_LEAVES = {"kind", "human_approval_required"}
ALTERNATIVE_STRINGS = (
    "PARTIAL", "MATCHED", "mysql", "postgresql", "s3", "LOW", "CRITICAL", "ne", "gt",
)  # fmt: skip


def vector(name: str) -> dict[str, Any]:
    return next(v for v in VECTORS if v["name"] == name)


def reorder(value: Any) -> Any:
    """Same data, reversed dict insertion order at every level."""
    if isinstance(value, dict):
        return {k: reorder(value[k]) for k in reversed(list(value))}
    if isinstance(value, list):
        return [reorder(v) for v in value]
    return value


def leaf_paths(value: Any, path: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in leaf_paths(v, (*path, k))]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in leaf_paths(v, (*path, i))]
    return [path]


def mutations(value: Any) -> list[Any]:
    if isinstance(value, bool):
        return [not value]
    if isinstance(value, int):
        return [value + 1]
    if isinstance(value, float):
        return [value + 0.25]
    if isinstance(value, str):
        return [value + "x", *(s for s in ALTERNATIVE_STRINGS if s != value)]
    return []


def get_path(data: Any, path: tuple[Any, ...]) -> Any:
    for key in path:
        data = data[key]
    return data


def is_coupled(payload: dict[str, Any], path: tuple[Any, ...]) -> bool:
    """A leaf that has no valid alternative value unless another field changes with it."""
    if path[-1] in STRUCTURAL_LEAVES:
        return True
    if path == ("sop_match_status",) and payload["matched_sop_reference"] is None:
        return True  # MATCHED/PARTIAL also need a reference
    return (
        path[-2:] == ("expected", "op") and get_path(payload, path[:-2])["kind"] == "object_exists"
    )


def set_path(data: Any, path: tuple[Any, ...], value: Any) -> None:
    for key in path[:-1]:
        data = data[key]
    data[path[-1]] = value


@pytest.mark.spec("S03-AC2")
@pytest.mark.parametrize("case", VECTORS, ids=[v["name"] for v in VECTORS])
def test_golden_vectors_match(case: dict[str, Any]) -> None:
    payload = ApprovalPayload.model_validate(case["payload"])
    canonical = canonical_json(payload)
    assert canonical == case["canonical"].encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == case["sha256"]
    assert payload_hash(payload) == case["sha256"]


@pytest.mark.spec("S03-AC2")
@pytest.mark.parametrize("case", VECTORS, ids=[v["name"] for v in VECTORS])
def test_hash_ignores_key_order_whitespace_and_insertion_order(case: dict[str, Any]) -> None:
    reordered = reorder(case["payload"])
    assert list(reordered) != list(case["payload"])
    spaced_json = json.dumps(reordered, indent=4, ensure_ascii=True)
    compact_json = json.dumps(case["payload"], separators=(",", ":"), ensure_ascii=False)
    hashes = {
        payload_hash(ApprovalPayload.model_validate(reordered)),
        payload_hash(ApprovalPayload.model_validate_json(spaced_json)),
        payload_hash(ApprovalPayload.model_validate_json(compact_json)),
    }
    assert hashes == {case["sha256"]}


@pytest.mark.spec("S03-AC2")
def test_hash_is_identical_in_separate_processes() -> None:
    script = (
        "import json, sys\n"
        "from backend.domain.hashing import payload_hash\n"
        "from backend.domain.review import ApprovalPayload\n"
        "vectors = json.load(open(sys.argv[1], encoding='utf-8'))['vectors']\n"
        "print(json.dumps([payload_hash(ApprovalPayload.model_validate(v['payload'])) "
        "for v in vectors]))\n"
    )
    expected = [v["sha256"] for v in VECTORS]
    for seed in ("0", "4242"):
        env = os.environ | {"PYTHONHASHSEED": seed, "PYTHONIOENCODING": "utf-8"}
        completed = subprocess.run(  # noqa: S603 - fixed interpreter and arguments
            [sys.executable, "-c", script, str(VECTORS_FILE)],
            capture_output=True,
            check=True,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            timeout=60,
        )
        assert json.loads(completed.stdout) == expected


@pytest.mark.spec("S03-AC2")
@pytest.mark.parametrize("name", ["canonical-duplicate-cleanup", "unicode-and-scalars"])
def test_changing_any_single_field_changes_the_hash(name: str) -> None:
    case = vector(name)
    original = case["sha256"]
    checked = 0
    for path in leaf_paths(case["payload"]):
        if is_coupled(case["payload"], path):
            continue
        current = get_path(case["payload"], path)
        for candidate in mutations(current):
            mutated = copy.deepcopy(case["payload"])
            set_path(mutated, path, candidate)
            try:
                model = ApprovalPayload.model_validate(mutated)
            except ValidationError:
                continue
            assert payload_hash(model) != original, f"changing {path} did not change the hash"
            checked += 1
            break
        else:
            if current is not None:
                pytest.fail(f"no valid single-field mutation found for {path}")
    assert checked >= 20


@pytest.mark.spec("S03-AC2")
def test_list_order_and_membership_change_the_hash() -> None:
    case = vector("canonical-duplicate-cleanup")
    swapped = copy.deepcopy(case["payload"])
    assertions = swapped["proposed_remediation"]["verification_plan"]["assertions"]
    assertions.reverse()
    appended = copy.deepcopy(case["payload"])
    appended["proposed_remediation"]["stop_conditions"].append("row count below 100000")
    for variant in (swapped, appended):
        assert payload_hash(ApprovalPayload.model_validate(variant)) != case["sha256"]


@pytest.mark.spec("S03-AC2")
def test_null_versus_absent_optional_field_hashes_the_same() -> None:
    # exclude_none=False: an omitted optional field and an explicit null are the same payload.
    case = vector("matched-sop-without-plan")
    payload = copy.deepcopy(case["payload"])
    del payload["matched_sop_reference"]["version"]
    payload["matched_sop_reference"]["sop_id"] = "SOP-PG-001"
    explicit = copy.deepcopy(payload)
    explicit["matched_sop_reference"]["version"] = None
    assert payload_hash(ApprovalPayload.model_validate(payload)) == payload_hash(
        ApprovalPayload.model_validate(explicit)
    )


# --- canonical_json rules for plain values ----------------------------------------------------


@pytest.mark.spec("S03-AC2")
def test_canonical_json_rules() -> None:
    cest = timezone(timedelta(hours=2))
    value = {
        "b": [1, 2.5, True, None],
        "a": "naïve ✓",
        "at": datetime(2026, 9, 30, 4, 0, tzinfo=cest),
        "amount": Decimal("10.50"),
        "id": UUID("12345678-1234-5678-1234-567812345678"),
    }
    assert (
        canonical_json(value)
        == (
            '{"a":"naïve ✓","amount":"10.50","at":"2026-09-30T02:00:00Z","b":[1,2.5,true,null],'
            '"id":"12345678-1234-5678-1234-567812345678"}'
        ).encode()
    )
    assert (
        canonical_json({"at": datetime(2026, 9, 30, tzinfo=UTC)})
        == b'{"at":"2026-09-30T00:00:00Z"}'
    )


@pytest.mark.spec("S03-AC2")
@pytest.mark.parametrize(
    "bad",
    [
        {"x": float("nan")},
        {"x": float("inf")},
        {"x": Decimal("NaN")},
        {"x": datetime(2026, 9, 30)},
        {1: "non-string key"},
        {"x": {1, 2}},
        {"x": "\ud800"},
        {"x": object()},
    ],
    ids=[
        "nan",
        "infinity",
        "decimal-nan",
        "naive-datetime",
        "int-key",
        "set",
        "surrogate",
        "object",
    ],
)
def test_canonical_json_rejects_values_without_a_canonical_form(bad: dict[Any, Any]) -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json(bad)
