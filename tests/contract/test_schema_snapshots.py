from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.domain import export_schemas
from backend.domain.export_schemas import DEFAULT_OUT_DIR, MODELS, build_schemas, render

UPDATE_HINT = (
    "Contract v1 changed. If this is deliberate, run "
    "`uv run python -m backend.domain.export_schemas`, review the diff in docs/contracts/v1/ "
    "and get contract sign-off (S03)."
)


@pytest.mark.spec("S03-AC5")
def test_committed_schemas_match_the_models() -> None:
    expected = {name: render(doc) for name, doc in build_schemas().items()}
    committed = {
        p.name: p.read_text(encoding="utf-8") for p in DEFAULT_OUT_DIR.glob("*.schema.json")
    }
    assert sorted(committed) == sorted(expected), UPDATE_HINT
    for name, text in expected.items():
        assert committed[name] == text, f"{name}: {UPDATE_HINT}"


@pytest.mark.spec("S03-AC5")
def test_every_contract_model_is_exported() -> None:
    names = set(build_schemas())
    assert {f"{m.__name__}.schema.json" for m in MODELS} | {"StateMachines.schema.json"} == names


@pytest.mark.spec("S03-AC5")
def test_check_mode_detects_drift_without_writing(tmp_path: Path) -> None:
    assert export_schemas.main(["--out", str(tmp_path)]) == 0
    assert export_schemas.main(["--out", str(tmp_path), "--check"]) == 0

    target = tmp_path / "ApprovalPayload.schema.json"
    target.write_text("{}\n", encoding="utf-8")
    (tmp_path / "Removed.schema.json").write_text("{}\n", encoding="utf-8")
    assert export_schemas.main(["--out", str(tmp_path), "--check"]) == 1
    assert target.read_text(encoding="utf-8") == "{}\n"

    assert export_schemas.main(["--out", str(tmp_path)]) == 0
    assert export_schemas.main(["--out", str(tmp_path), "--check"]) == 0
    assert not (tmp_path / "Removed.schema.json").exists()


@pytest.mark.spec("S03-AC5")
def test_schemas_are_deterministic_and_lf_only() -> None:
    first = {name: render(doc) for name, doc in build_schemas().items()}
    second = {name: render(doc) for name, doc in build_schemas().items()}
    assert first == second
    for path in DEFAULT_OUT_DIR.glob("*.schema.json"):
        assert b"\r\n" not in path.read_bytes(), path.name


def test_approval_payload_schema_closes_the_assertion_union() -> None:
    schema = json.loads((DEFAULT_OUT_DIR / "ApprovalPayload.schema.json").read_text("utf-8"))
    assert schema["additionalProperties"] is False
    kinds = schema["$defs"]["VerificationPlan"]["properties"]["assertions"]["items"]
    assert set(kinds["discriminator"]["mapping"]) == {
        "duplicate_count", "row_count", "sql_scalar", "object_exists", "object_count",
        "document_count",
    }  # fmt: skip
