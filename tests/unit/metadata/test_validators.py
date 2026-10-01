from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from backend.metadata.models import IssueSeverity, ValidationIssue
from backend.metadata.validators import Candidate, build_candidate
from tests.fakes.metadata_trees import (
    HISTORY,
    KNOWN_ISSUE,
    OWNERSHIP,
    PIPELINE,
    RULES,
    RUNBOOK_PG,
    SOP_DUPLICATES,
    edit_doc,
    read_doc,
    seed_tree,
    write_doc,
    write_text,
)

NOW = datetime(2026, 10, 1, tzinfo=UTC)
MAX_BYTES = 1 << 20

Mutation = Callable[[Path], None]


def build(tree: Path, max_bytes: int = MAX_BYTES) -> Candidate:
    return build_candidate(tree, max_bytes, NOW)


def codes(issues: tuple[ValidationIssue, ...]) -> set[str]:
    return {i.code for i in issues}


def _set(relative: str, path: tuple[Any, ...], value: Any) -> Mutation:
    def mutate(tree: Path) -> None:
        def change(data: dict[str, Any]) -> None:
            target: Any = data
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value

        edit_doc(tree, relative, change)

    return mutate


def _copy_as(source: str, target: str, **updates: Any) -> Mutation:
    def mutate(tree: Path) -> None:
        write_doc(tree, target, read_doc(tree, source) | updates)

    return mutate


def _second_rule_set(tree: Path) -> None:
    rules = read_doc(tree, RULES)
    write_doc(tree, "dq_rules/again.yaml", rules | {"rule_set_id": "CUSTOMER-DQ-2"})


def _second_pipeline_same_dag(tree: Path) -> None:
    pipeline = read_doc(tree, PIPELINE)
    write_doc(tree, "pipelines/clone.yaml", pipeline | {"pipeline_id": "customer_clone"})


def _second_ownership(tree: Path) -> None:
    write_doc(tree, "ownership/other.yaml", read_doc(tree, OWNERSHIP) | {"ownership_id": "OWN-2"})


def _profile_rule(tree: Path) -> None:
    edit_doc(
        tree,
        PIPELINE,
        lambda d: d["dq_profile"]["TARGET"][0].update(rules=["BR-CUST-001", "BR-MISSING"]),
    )


# --- AC2: every rule yields its code and severity ---------------------------------------------

ERROR_CASES: list[Any] = [
    pytest.param(_copy_as(SOP_DUPLICATES, "sops/copy.yaml"), "DUPLICATE_ID", id="duplicate-doc-id"),
    pytest.param(_second_rule_set, "DUPLICATE_ID", id="duplicate-rule-id"),
    pytest.param(_second_pipeline_same_dag, "DUPLICATE_ID", id="duplicate-dag-id"),
    pytest.param(_set(KNOWN_ISSUE, ("pipeline_ids",), ["nope"]), "BROKEN_REFERENCE",
                 id="ki-pipeline"),
    pytest.param(_set(RULES, ("pipeline_id",), "nope"), "BROKEN_REFERENCE", id="ruleset-pipeline"),
    pytest.param(_set(RUNBOOK_PG, ("related_sops",), ["SOP-X"]), "BROKEN_REFERENCE",
                 id="related-sop"),
    pytest.param(
        _set(HISTORY, ("related_known_issues",), ["KI-X"]), "BROKEN_REFERENCE", id="related-ki"
    ),
    pytest.param(_profile_rule, "BROKEN_REFERENCE", id="dq-profile-rule"),
    pytest.param(_set(RULES, ("dataset",), "dw.other"), "BROKEN_REFERENCE", id="ruleset-dataset"),
    pytest.param(
        _set(PIPELINE, ("self_healing", "permitted_systems"), ["s3"]),
        "SCOPE_INVALID",
        id="permitted-systems",
    ),
    pytest.param(
        _set(PIPELINE, ("sla",), {"enabled": True}), "SCOPE_INVALID", id="sla-without-targets"
    ),
    pytest.param(
        _set(PIPELINE, ("sla", "timezone"), "Mars/Olympus"), "SCOPE_INVALID", id="sla-timezone"
    ),
    pytest.param(_second_ownership, "OWNERSHIP_CONFLICT", id="ownership-conflict"),
    pytest.param(
        _set(PIPELINE, ("dq_profile", "TARGET", 0, "agent"), "spellcheck"),
        "SCHEMA_INVALID",
        id="unknown-dq-agent",
    ),
    pytest.param(
        _set(PIPELINE, ("dq_profile",), {"target": [{"agent": "custom_dq"}]}),
        "SCHEMA_INVALID",
        id="short-form-dq-profile",
    ),
    pytest.param(
        _set(PIPELINE, ("dq_profile", "TARGET", 0, "trigger"), "on_warning"),
        "SCHEMA_INVALID",
        id="trigger-before-s18",
    ),
    pytest.param(_set(PIPELINE, ("sla", "expected_completion_time"), 180), "SCHEMA_INVALID",
                 id="unquoted-time"),
]  # fmt: skip


@pytest.mark.spec("S04-AC2")
@pytest.mark.parametrize(("mutate", "code"), ERROR_CASES)
def test_invalid_tree_reports_error(tmp_path: Path, mutate: Mutation, code: str) -> None:
    tree = seed_tree(tmp_path)
    mutate(tree)
    candidate = build(tree)
    assert candidate.snapshot is None
    assert code in codes(candidate.errors), candidate.errors
    assert all(i.severity is IssueSeverity.ERROR for i in candidate.errors)


@pytest.mark.spec("S04-AC2")
@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        pytest.param(lambda t: (t / OWNERSHIP).unlink(), "MISSING_OWNERSHIP", id="no-ownership"),
        pytest.param(
            _set(SOP_DUPLICATES, ("status",), "DEPRECATED"), "DEPRECATED_REFERENCE", id="deprecated"
        ),
        pytest.param(lambda t: write_text(t, "sops/notes.txt", "x"), "UNKNOWN_FILE", id="txt"),
        pytest.param(lambda t: write_text(t, "loose.yaml", "a: 1"), "UNKNOWN_FILE", id="root-file"),
    ],
)
def test_warnings_do_not_block_a_load(tmp_path: Path, mutate: Mutation, code: str) -> None:
    tree = seed_tree(tmp_path)
    mutate(tree)
    candidate = build(tree)
    assert candidate.errors == ()
    assert candidate.snapshot is not None
    assert code in codes(candidate.warnings)


@pytest.mark.spec("S04-AC2")
def test_issues_point_at_file_document_and_field(tmp_path: Path) -> None:
    tree = seed_tree(tmp_path)
    _set(KNOWN_ISSUE, ("pipeline_ids",), ["nope"])(tree)
    (issue,) = build(tree).errors
    assert (issue.file, issue.document_id, issue.location) == (
        KNOWN_ISSUE,
        "KI-DQ-001",
        "pipeline_ids",
    )


@pytest.mark.spec("S04-AC2")
def test_schema_errors_never_echo_document_values(tmp_path: Path) -> None:
    tree = seed_tree(tmp_path)
    _set(PIPELINE, ("sla", "max_runtime_minutes"), "leak-me-please")(tree)
    candidate = build(tree)
    assert "SCHEMA_INVALID" in codes(candidate.errors)
    assert not any("leak-me-please" in i.message for i in candidate.errors)


# --- AC4: live runtime state is rejected at any depth ----------------------------------------


@pytest.mark.spec("S04-AC4")
@pytest.mark.parametrize(
    ("path", "location"),
    [
        (("latest_run_id",), "latest_run_id"),
        (("airflow", "last_run_status"), "airflow.last_run_status"),
        (("datasets", 0, "retry_count"), "datasets[0].retry_count"),
    ],
    ids=["top-level", "nested", "inside-list"],
)
def test_forbidden_runtime_keys_are_rejected(
    tmp_path: Path, path: tuple[Any, ...], location: str
) -> None:
    tree = seed_tree(tmp_path)
    _set(PIPELINE, path, "scheduled__2026-10-01")(tree)
    candidate = build(tree)
    assert candidate.snapshot is None
    forbidden = [i for i in candidate.errors if i.code == "FORBIDDEN_RUNTIME_KEY"]
    assert [i.location for i in forbidden] == [location]


# --- AC9: hostile or careless input --------------------------------------------------------------

SENTINEL = "s04_yaml_rce_sentinel"


@pytest.mark.spec("S04-AC9")
@pytest.mark.parametrize(
    ("text", "code"),
    [
        pytest.param(
            f"!!python/object/apply:os.system ['echo > {SENTINEL}']\n",
            "UNSAFE_YAML",
            id="python-tag",
        ),
        pytest.param(
            'a: &a ["x","x","x","x","x","x","x","x","x"]\n'
            "b: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a]\n"
            "c: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b]\n"
            "d: [*c,*c,*c,*c,*c,*c,*c,*c,*c]\n",
            "UNSAFE_YAML",
            id="billion-laughs",
        ),
        pytest.param("sop_id: A\nsop_id: B\n", "YAML_INVALID", id="duplicate-key"),
        pytest.param("? [a]\n: 1\n", "YAML_INVALID", id="unhashable-key"),
        pytest.param("? {a: 1}\n: 1\n", "YAML_INVALID", id="mapping-key"),
        pytest.param("a: " + "[" * 5000 + "]" * 5000 + "\n", "YAML_INVALID", id="deep-nesting"),
        pytest.param("- just\n- a list\n", "YAML_INVALID", id="not-a-mapping"),
        pytest.param("sop_id: [unclosed\n", "YAML_INVALID", id="syntax"),
    ],
)
def test_unsafe_yaml_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, code: str
) -> None:
    monkeypatch.chdir(tmp_path)
    tree = seed_tree(tmp_path)
    write_text(tree, "sops/hostile.yaml", text)
    candidate = build(tree)
    assert candidate.snapshot is None
    assert code in codes(candidate.errors)
    assert not (tmp_path / SENTINEL).exists()


@pytest.mark.spec("S04-AC9")
@pytest.mark.parametrize(
    "text",
    [
        '{"sop_id": "A", "sop_id": "B"}',
        '{"value": NaN}',
        '{"a": ' + "[" * 50000 + "]" * 50000 + "}",
    ],
    ids=["duplicate-key", "nan", "deep-nesting"],
)
def test_ambiguous_json_is_rejected(tmp_path: Path, text: str) -> None:
    tree = seed_tree(tmp_path)
    write_text(tree, "sops/odd.json", text)
    assert "YAML_INVALID" in codes(build(tree).errors)


@pytest.mark.spec("S04-AC9")
def test_oversized_file_is_rejected_without_parsing(tmp_path: Path) -> None:
    tree = seed_tree(tmp_path)
    write_text(tree, "sops/huge.yaml", "title: " + "x" * 5000 + "\n")
    candidate = build(tree, max_bytes=4096)
    assert [i.file for i in candidate.errors if i.code == "FILE_TOO_LARGE"] == ["sops/huge.yaml"]


@pytest.mark.spec("S04-AC9")
def test_symlink_outside_the_tree_is_rejected(tmp_path: Path) -> None:
    tree = seed_tree(tmp_path)
    outside = tmp_path / "outside.yaml"
    outside.write_text("sop_id: OUTSIDE\n", encoding="utf-8")
    try:
        os.symlink(outside, tree / "sops" / "escape.yaml")
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks needs Developer Mode or admin rights on Windows")
    assert "PATH_ESCAPE" in codes(build(tree).errors)


@pytest.mark.spec("S04-AC9")
@pytest.mark.skipif(os.name != "nt", reason="directory junctions are Windows-only")
def test_junction_outside_the_tree_is_rejected(tmp_path: Path) -> None:
    import _winapi  # CPython's Windows API module; junctions need no admin rights

    tree = seed_tree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "escape.yaml").write_text("sop_id: OUTSIDE\n", encoding="utf-8")
    _winapi.CreateJunction(str(outside), str(tree / "sops" / "linked"))
    errors = build(tree).errors
    assert [i.file for i in errors if i.code == "PATH_ESCAPE"] == ["sops/linked/escape.yaml"]


def test_hidden_directories_are_ignored(tmp_path: Path) -> None:
    tree = seed_tree(tmp_path)
    write_text(tree, ".git/config", "[core]")
    write_text(tree, "sops/.draft/wip.yaml", "anything: at all")
    candidate = build(tree)
    assert candidate.errors == () and candidate.warnings == ()


@pytest.mark.spec("S04-AC9")
def test_hostile_file_never_crashes_startup_or_reload(tmp_path: Path) -> None:
    from backend.metadata.service import MetadataService
    from tests.fakes.metadata_trees import settings_for

    tree = seed_tree(tmp_path)
    write_text(tree, "sops/bad.yaml", "? [a]\n: 1\n")
    service = MetadataService(settings_for(tree))
    health = service.load_initial()  # must not raise (S04-AC14)
    assert health.state == "EMPTY"
    assert [e.code for e in health.errors] == ["YAML_INVALID"]
