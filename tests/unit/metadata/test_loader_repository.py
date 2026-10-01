from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from backend.domain.common import SystemName
from backend.domain.hashing import canonical_json
from backend.domain.sop import SOPReference
from backend.metadata.context_builder import MetadataContextBuilder
from backend.metadata.models import DocumentType, MetadataQuery, PipelineMetadata
from backend.metadata.repository import MetadataSnapshot
from backend.metadata.service import MetadataService
from backend.metadata.settings import DEFAULT_METADATA_DIR
from backend.metadata.validators import build_candidate, current_version
from tests.fakes.metadata_trees import (
    PIPELINE,
    SOP_DUPLICATES,
    SOP_PARTITION,
    edit_doc,
    read_doc,
    seed_tree,
    settings_for,
    write_doc,
)

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def load(tree: Path) -> MetadataSnapshot:
    candidate = build_candidate(tree, 1 << 20, NOW)
    assert candidate.errors == (), candidate.errors
    assert candidate.snapshot is not None
    return candidate.snapshot


# --- AC1: seed content ---------------------------------------------------------------------


@pytest.mark.spec("S04-AC1")
def test_seed_documents_load_without_errors() -> None:
    snap = load(DEFAULT_METADATA_DIR)
    assert snap.counts_by_type() == {
        DocumentType.PIPELINE: 1,
        DocumentType.BUSINESS_RULE: 1,
        DocumentType.SOP: 2,
        DocumentType.RUNBOOK: 2,
        DocumentType.KNOWN_ISSUE: 1,
        DocumentType.OWNERSHIP: 1,
        DocumentType.HISTORICAL_INCIDENT: 1,
    }
    assert snap.version is not None and re.fullmatch(r"[0-9a-f]{64}", snap.version)
    assert snap.warnings == ()


@pytest.mark.spec("S04-AC1")
def test_version_is_stable_and_tracks_file_bytes(tmp_path: Path) -> None:
    tree = seed_tree(tmp_path)
    first, second = load(tree).version, load(tree).version
    assert first == second == current_version(tree, 1 << 20)
    path = tree / SOP_PARTITION
    path.write_bytes(path.read_bytes() + b"\n")  # one byte, same content
    assert load(tree).version != first


@pytest.mark.spec("S04-AC1")
def test_version_ignores_non_document_files(tmp_path: Path) -> None:
    tree = seed_tree(tmp_path)
    before = load(tree).version
    (tree / "README.md").write_text("changed", encoding="utf-8")
    assert load(tree).version == before


# --- AC5: deterministic queries --------------------------------------------------------------


def _runbook(runbook_id: str, category: str) -> dict[str, Any]:
    return {
        "runbook_id": runbook_id,
        "title": f"Runbook {runbook_id}",
        "pipeline_ids": ["customer_daily_load"],
        "categories": [category],
        "steps": ["look"],
    }


@pytest.mark.spec("S04-AC5")
def test_results_are_sorted_by_id_regardless_of_file_order(tmp_path: Path) -> None:
    tree = seed_tree(tmp_path)
    # File names sort in the opposite order to the document IDs.
    write_doc(tree, "runbooks/a_first_file.yaml", _runbook("RB-ZZZ", "OTHER"))
    write_doc(tree, "runbooks/z_last_file.yaml", _runbook("RB-AAA", "OTHER"))
    snap = load(tree)
    ids = [r.runbook_id for r in snap.runbooks("customer_daily_load")]
    assert ids == sorted(ids)
    assert ids[0] == "RB-AAA"
    query = MetadataQuery(pipeline_id="customer_daily_load")
    assert snap.search(query) == snap.search(query) == load(tree).search(query)
    found = [(d.document_type, d.document_id) for d in snap.search(query)]
    assert found == sorted(found)


@pytest.mark.spec("S04-AC5")
def test_query_filters_combine_with_and_and_lists_match_any() -> None:
    snap = load(DEFAULT_METADATA_DIR)
    sops_on_pg = snap.search(
        MetadataQuery(system=SystemName.POSTGRESQL, document_types=(DocumentType.SOP,))
    )
    assert [d.document_id for d in sops_on_pg] == ["SOP-PG-001", "SOP-PG-002"]
    duplicates = snap.search(
        MetadataQuery(category="DUPLICATE_DATA", document_types=(DocumentType.SOP,))
    )
    assert [d.document_id for d in duplicates] == ["SOP-PG-002"]
    any_tag = snap.search(MetadataQuery(tags=("partitions", "duplicates")))
    assert {d.document_id for d in any_tag} == {"SOP-PG-001", "SOP-PG-002", "KI-DQ-001", "HIST-001"}
    assert snap.search(MetadataQuery(system=SystemName.S3)) == ()


@pytest.mark.spec("S04-AC5")
def test_get_sops_returns_applicable_approved_sops_only(tmp_path: Path) -> None:
    tree = seed_tree(tmp_path)
    sop = read_doc(tree, SOP_PARTITION)
    write_doc(tree, "sops/mysql.yaml", sop | {"sop_id": "SOP-MY-001", "target_system": "mysql"})
    write_doc(tree, "sops/draft.yaml", sop | {"sop_id": "SOP-PG-DRAFT", "status": "DRAFT"})
    write_doc(
        tree,
        "sops/explicit.yaml",
        sop | {"sop_id": "SOP-MY-LISTED", "target_system": "mysql",
               "pipeline_ids": ["customer_daily_load"]},
    )  # fmt: skip
    snap = load(tree)
    # mysql is not permitted for this pipeline, unless the SOP names the pipeline explicitly.
    applicable = [s.sop_id for s in snap.sops("customer_daily_load")]
    assert applicable == ["SOP-MY-LISTED", "SOP-PG-001", "SOP-PG-002"]
    assert [s.sop_id for s in snap.sops("customer_daily_load", "DUPLICATE_DATA")] == ["SOP-PG-002"]
    assert snap.sops("unknown_pipeline") == ()


# --- AC11: SOP content hash ------------------------------------------------------------------


@pytest.mark.spec("S04-AC11")
def test_sop_content_hash_pins_exact_content(tmp_path: Path) -> None:
    tree = seed_tree(tmp_path)
    sop = load(tree).get_sop("SOP-PG-002")
    assert sop is not None
    expected = hashlib.sha256(
        canonical_json(sop.model_dump(mode="json", exclude={"content_sha256"}))
    ).hexdigest()
    assert sop.content_sha256 == expected
    again = load(tree).get_sop("SOP-PG-002")
    assert again is not None and again.content_sha256 == expected

    edit_doc(tree, SOP_DUPLICATES, lambda d: d["stop_conditions"].append("row count drops"))
    changed = load(tree).get_sop("SOP-PG-002")
    assert changed is not None and changed.content_sha256 != expected

    reference = SOPReference(sop_id=sop.sop_id, version=sop.version, content_sha256=expected)
    assert reference.content_sha256 == expected


# --- AC12: pipeline semantics ----------------------------------------------------------------


@pytest.mark.spec("S04-AC12")
def test_pipeline_datasets_scope_and_defaults() -> None:
    snap = load(DEFAULT_METADATA_DIR)
    pipeline = snap.get_pipeline("customer_daily_load")
    assert pipeline is not None
    assert [d.name for d in pipeline.declared_datasets()] == ["dw.customer", "sales.customer"]
    assert pipeline.pii_columns("dw.customer") == ("email", "phone")
    assert pipeline.effective_permitted_systems() == (SystemName.POSTGRESQL,)
    assert snap.get_pipeline_by_dag_id("customer_daily_load") == pipeline
    assert snap.get_pipeline_by_dag_id("other_dag") is None


@pytest.mark.spec("S04-AC12")
def test_pipeline_defaults_and_approval_is_mandatory() -> None:
    data = read_doc(DEFAULT_METADATA_DIR, PIPELINE)
    minimal = {k: data[k] for k in ("pipeline_id", "name", "airflow", "source", "target")}
    pipeline = PipelineMetadata.model_validate(minimal)
    assert pipeline.effective_permitted_systems() == (SystemName.MYSQL, SystemName.POSTGRESQL)
    assert pipeline.monitoring.monitor_enabled("sla") is True
    assert pipeline.monitoring.critical_runtime_factor == 1.5
    assert pipeline.self_healing.on_recovery_failure == "REPLAN_REQUIRED"
    assert pipeline.self_healing.auto_rca is False
    disabled = PipelineMetadata.model_validate(minimal | {"monitoring": {"enabled": False}})
    assert disabled.monitoring.monitor_enabled("airflow") is False
    with pytest.raises(ValidationError):
        PipelineMetadata.model_validate(
            minimal | {"self_healing": {"human_approval_required": False}}
        )


@pytest.mark.spec("S04-AC12")
def test_dataset_system_must_match_endpoint() -> None:
    data = read_doc(DEFAULT_METADATA_DIR, PIPELINE)
    data["datasets"][1]["system"] = "mysql"  # dw.customer is the postgresql target
    with pytest.raises(ValidationError, match="endpoint uses postgresql"):
        PipelineMetadata.model_validate(data)


# --- Context builder ------------------------------------------------------------------------


async def test_context_builder_bundles_reference_metadata() -> None:
    service = MetadataService(settings_for(DEFAULT_METADATA_DIR))
    service.load_initial()
    context = await MetadataContextBuilder(service).build("customer_daily_load")
    assert context is not None
    kinds = [item.document_type for item in context.items]
    assert kinds[0] is DocumentType.SOP  # approved SOPs rank above pipeline knowledge
    assert {i.ref_id for i in context.items} >= {"customer_daily_load", "CUSTOMER-DQ", "KI-DQ-001"}
    assert all(i.source == "metadata" and i.version == context.version for i in context.items)
    assert await MetadataContextBuilder(service).build("unknown") is None
