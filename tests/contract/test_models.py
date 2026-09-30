from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from backend.domain.analysis import PipelineFailureRequest, StructuredAnalysis
from backend.domain.common import SystemName
from backend.domain.dq import (
    Checkpoint,
    DQAgentName,
    DQAgentResult,
    DQProfile,
    DQProfileEntry,
    DQRuleRef,
    DQRunResult,
    DQStatus,
)
from backend.domain.incident import IncidentStatus
from backend.domain.monitoring import MonitoringEvent, MonitorRunStatus, PipelineHealthResult
from backend.domain.remediation import RemediationPlan
from backend.domain.review import ApprovalPayload, ApproveRequest, ReviewRevisionResult
from backend.domain.sop import SOPMatchStatus, SOPProposal, SOPReference

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _payload(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "sop_match_status": "NO_MATCH",
        "matched_sop_reference": None,
        "proposed_sop": None,
        "proposed_remediation": plan,
    }


def canonical_plan() -> dict[str, Any]:
    plan: dict[str, Any] = load("canonical_structured_analysis.json")["proposed_remediation"]
    return plan


# --- AC1: source examples validate ---------------------------------------------------------


@pytest.mark.spec("S03-AC1")
def test_src_21_3_monitoring_event_validates() -> None:
    event = MonitoringEvent.model_validate(load("src_21_3_monitoring_event.json"))
    assert event.severity == "CRITICAL"
    assert event.source == "AIRFLOW"
    assert event.detected_at == datetime(2026, 9, 30, 2, 5, tzinfo=UTC)
    assert event.event_id is None  # assigned on persistence (S13)


@pytest.mark.spec("S03-AC1")
def test_src_21_3_pipeline_health_uses_c6_union_fields() -> None:
    health = PipelineHealthResult.model_validate(load("src_21_3_pipeline_health.json"))
    assert health.overall_status == "CRITICAL"
    assert health.monitor_status["mcp"] is MonitorRunStatus.SKIPPED
    event = health.events[0]
    assert (event.resource, event.rule_id) == ("dw.customer", "BR-CUST-002")
    assert event.idempotency_key is not None


@pytest.mark.spec("S03-AC1")
def test_src_21_3_pipeline_health_rejects_legacy_checked_at() -> None:
    data = load("src_21_3_pipeline_health.json")
    data["checked_at"] = data.pop("evaluated_at")  # architecture §18 C6: evaluated_at is canonical
    with pytest.raises(ValidationError):
        PipelineHealthResult.model_validate(data)


@pytest.mark.spec("S03-AC1")
def test_src_21_3_incident_statuses_are_all_present() -> None:
    source_statuses = {
        "DETECTED", "INVESTIGATING", "RCA_COMPLETED", "AWAITING_APPROVAL", "APPROVED", "EXECUTING",
        "VERIFYING", "RECOVERY_RUNNING", "REPLAN_REQUIRED", "RESOLVED", "FAILED", "REJECTED",
    }  # fmt: skip
    assert {s.value for s in IncidentStatus} == source_statuses


@pytest.mark.spec("S03-AC1")
def test_src_23_3_dq_profile_validates() -> None:
    profile = DQProfile.model_validate(load("src_23_3_dq_profile.json"))
    assert [e.agent for e in profile.root[Checkpoint.TARGET]] == [
        DQAgentName.CUSTOM_DQ,
        DQAgentName.STATISTICAL,
    ]
    assert profile.root[Checkpoint.RECONCILIATION][0].blocking is True


@pytest.mark.spec("S03-AC1")
def test_src_23_3_legacy_reconciliation_name_is_rejected_with_guidance() -> None:
    # src §23.3 spells the agent "reconciliation"; architecture §18 C9 fixes the name to "recon".
    with pytest.raises(ValidationError, match="'recon'"):
        DQProfileEntry.model_validate({"agent": "reconciliation", "blocking": True})


@pytest.mark.spec("S03-AC1")
def test_src_33_5_sop_shape_validates_as_proposal_and_reference() -> None:
    sop = load("src_33_5_sop.json")
    # sop_id and status are catalogue fields (S04 / approved_sops), not part of a proposal.
    reference = SOPReference(sop_id=sop.pop("sop_id"))
    assert sop.pop("status") == "APPROVED"
    proposal = SOPProposal.model_validate(sop)
    assert reference.sop_id == "SOP-PG-001"
    assert proposal.target_system is SystemName.POSTGRESQL
    assert proposal.stop_conditions == (
        "partition strategy cannot be verified",
        "overlapping partition exists",
    )


@pytest.mark.spec("S03-AC1")
def test_src_33_5_business_rules_are_referenced_by_plans_and_results() -> None:
    rule_set = load("src_33_5_business_rule.json")
    refs = [
        DQRuleRef(rule_id=rule["rule_id"], rule_set_id=rule_set["rule_set_id"])
        for rule in rule_set["rules"]
    ]
    assert [r.rule_id for r in refs] == ["BR-CUST-001", "BR-CUST-002"]
    rule = rule_set["rules"][1]
    result = DQAgentResult(
        agent=DQAgentName.CUSTOM_DQ,
        checkpoint=Checkpoint.TARGET,
        rule_id=rule["rule_id"],
        status=DQStatus.CRITICAL,
        blocking=rule["blocking"],
        expected=rule["expected"],
        actual={"duplicate_count": 417},
        failed_row_count=417,
        evaluated_at=datetime(2026, 9, 30, 2, 20, tzinfo=UTC),
    )
    assert result.expected == {"duplicate_count": 0}


@pytest.mark.spec("S03-AC1")
def test_canonical_structured_analysis_and_approval_payload_validate() -> None:
    data = load("canonical_structured_analysis.json")
    analysis = StructuredAnalysis.model_validate(data)
    plan = analysis.proposed_remediation
    assert plan is not None
    assert plan.max_affected_rows == 417
    assert plan.verification_rules == (DQRuleRef(rule_id="BR-CUST-002", rule_set_id="CUSTOMER-DQ"),)
    payload = ApprovalPayload(
        sop_match_status=analysis.sop_match_status,
        matched_sop_reference=analysis.matched_sop_reference,
        proposed_sop=analysis.proposed_sop,
        proposed_remediation=plan,
    )
    assert ApprovalPayload.model_validate_json(payload.model_dump_json()) == payload


# --- AC3: plan validation ------------------------------------------------------------------


_DUP = {"kind": "duplicate_count", "resource": "t"}
_ROWS = {"kind": "row_count", "resource": "t"}
_SQL = {"kind": "sql_scalar", "sql": "SELECT 1"}
_OBJ = {"kind": "object_exists", "bucket": "b", "key": "k"}


def _eq(value: Any) -> dict[str, Any]:
    return {"op": "eq", "value": value}


@pytest.mark.spec("S03-AC3")
def test_plan_rejects_unknown_target_system() -> None:
    plan = canonical_plan() | {"target_system": "oracle"}
    with pytest.raises(ValidationError, match="target_system"):
        RemediationPlan.model_validate(plan)


@pytest.mark.spec("S03-AC3")
def test_approval_payload_rejects_writes_without_assertions() -> None:
    plan = canonical_plan() | {"verification_plan": {"assertions": []}}
    with pytest.raises(ValidationError, match="assertions must not be empty"):
        ApprovalPayload.model_validate(_payload(plan))


@pytest.mark.spec("S03-AC3")
def test_rca_keeps_a_plan_without_assertions_for_the_plan_validator() -> None:
    # Decision 2026-09-30: an RCA is not lost over an incomplete plan. The plan validator (S09 N5)
    # marks it non-executable, and it can never be approved.
    plan = canonical_plan() | {"verification_plan": {"assertions": []}}
    analysis = load("canonical_structured_analysis.json") | {"proposed_remediation": plan}
    parsed = StructuredAnalysis.model_validate(analysis)
    assert parsed.proposed_remediation is not None
    assert parsed.proposed_remediation.verification_plan.assertions == ()


@pytest.mark.spec("S03-AC3")
def test_plan_without_writes_may_have_no_assertions() -> None:
    plan = canonical_plan() | {
        "expected_write_operations": [],
        "write_tools": None,
        "verification_plan": {"assertions": []},
    }
    assert RemediationPlan.model_validate(plan).verification_plan.assertions == ()


@pytest.mark.spec("S03-AC3")
def test_plan_rejects_unknown_assertion_kind() -> None:
    plan = copy.deepcopy(canonical_plan())
    plan["verification_plan"]["assertions"][0]["kind"] = "eyeball_check"
    with pytest.raises(ValidationError, match="eyeball_check"):
        RemediationPlan.model_validate(plan)


@pytest.mark.spec("S03-AC3")
def test_plan_rejects_assertion_without_expected() -> None:
    plan = copy.deepcopy(canonical_plan())
    del plan["verification_plan"]["assertions"][1]["expected"]
    with pytest.raises(ValidationError, match="expected"):
        RemediationPlan.model_validate(plan)


@pytest.mark.spec("S03-AC3")
def test_precondition_assertions_are_typed_too() -> None:
    plan = copy.deepcopy(canonical_plan())
    del plan["precondition_assertions"][0]["expected"]
    with pytest.raises(ValidationError, match="expected"):
        RemediationPlan.model_validate(plan)


@pytest.mark.spec("S03-AC3")
@pytest.mark.parametrize(
    "assertion",
    [
        pytest.param({**_DUP, "key_columns": ["k"], "expected": _eq(-1)}, id="negative-count"),
        pytest.param({**_DUP, "key_columns": [], "expected": _eq(0)}, id="no-key-columns"),
        pytest.param({**_ROWS, "expected": {"op": "approx", "value": 1}}, id="unknown-op"),
        pytest.param({**_ROWS, "expected": _eq(True)}, id="bool-as-count"),
        pytest.param({**_ROWS, "expected": _eq(1.0)}, id="float-as-count"),
        pytest.param({**_SQL, "expected": _eq(float("nan"))}, id="nan-scalar"),
        pytest.param({**_OBJ, "expected": {"op": "gt", "value": True}}, id="exists-ordering-op"),
        pytest.param({**_OBJ, "expected": {"value": 1}}, id="exists-with-int"),
        pytest.param({**_ROWS, "expected": _eq(1), "extra": 1}, id="unknown-field"),
    ],
)
def test_assertion_expectations_are_type_checked(assertion: dict[str, Any]) -> None:
    plan = copy.deepcopy(canonical_plan())
    plan["verification_plan"]["assertions"] = [{"assertion_id": "a1", **assertion}]
    with pytest.raises(ValidationError):
        RemediationPlan.model_validate(plan)


@pytest.mark.spec("S03-AC3")
def test_every_assertion_kind_is_accepted() -> None:
    assertions = [
        {"kind": "duplicate_count", "resource": "dw.customer", "key_columns": ["customer_id"],
         "expected": {"op": "eq", "value": 0}},
        {"kind": "row_count", "resource": "dw.customer", "expected": {"op": "gte", "value": 1}},
        {"kind": "sql_scalar", "sql": "SELECT max(load_ts) > now() - interval '1 day'",
         "expected": {"op": "eq", "value": True}},
        {"kind": "object_exists", "bucket": "landing", "key": "customer/2026-09-30.csv",
         "expected": {"value": True}},
        {"kind": "object_count", "bucket": "landing", "prefix": "customer/",
         "expected": {"op": "gt", "value": 0}},
        {"kind": "document_count", "resource": "crm.customers", "filter": {"status": "dup"},
         "expected": {"op": "eq", "value": 0}},
    ]  # fmt: skip
    plan = copy.deepcopy(canonical_plan())
    plan["verification_plan"]["assertions"] = [
        {"assertion_id": f"a{i}", **a} for i, a in enumerate(assertions)
    ]
    parsed = RemediationPlan.model_validate(plan)
    assert [a.kind for a in parsed.verification_plan.assertions] == [a["kind"] for a in assertions]


# --- Contract invariants shared by later slices --------------------------------------------


def test_approval_payload_requires_sop_reference_consistency() -> None:
    with pytest.raises(ValidationError, match="required when sop_match_status is MATCHED"):
        ApprovalPayload(
            sop_match_status=SOPMatchStatus.MATCHED,
            matched_sop_reference=None,
            proposed_sop=None,
            proposed_remediation=None,
        )
    with pytest.raises(ValidationError, match="must be null"):
        ApprovalPayload(
            sop_match_status=SOPMatchStatus.NO_MATCH,
            matched_sop_reference=SOPReference(sop_id="SOP-PG-001"),
            proposed_sop=None,
            proposed_remediation=None,
        )


def test_approval_payload_fields_are_all_required() -> None:
    # Every field is explicit, so a reviewer never approves an implicit default.
    with pytest.raises(ValidationError) as caught:
        ApprovalPayload.model_validate({"sop_match_status": "NO_MATCH"})
    missing = {e["loc"][0] for e in caught.value.errors() if e["type"] == "missing"}
    assert missing == {"matched_sop_reference", "proposed_sop", "proposed_remediation"}


def test_models_are_frozen_and_reject_unknown_fields() -> None:
    request = PipelineFailureRequest(workflow_id="customer_daily_load", run_id="manual__1")
    with pytest.raises(ValidationError):
        request.run_id = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError, match="extra"):
        PipelineFailureRequest.model_validate({"workflow_id": "d", "run_id": "r", "sql": "x"})


def test_generated_sop_always_requires_human_approval() -> None:
    sop = load("src_33_5_sop.json")
    del sop["sop_id"], sop["status"]
    with pytest.raises(ValidationError):
        SOPProposal.model_validate(sop | {"human_approval_required": False})


def test_approve_request_requires_a_sha256_hash() -> None:
    ok = ApproveRequest(expected_revision=1, expected_payload_hash="a" * 64)
    assert ok.comment is None
    for bad in ("A" * 64, "a" * 63, "sha256:" + "a" * 64):
        with pytest.raises(ValidationError):
            ApproveRequest(expected_revision=1, expected_payload_hash=bad)
    with pytest.raises(ValidationError):
        ApproveRequest(expected_revision=0, expected_payload_hash="a" * 64)


def test_revision_accept_requires_payload() -> None:
    with pytest.raises(ValidationError, match="validated_payload"):
        ReviewRevisionResult(decision="ACCEPT", summary="looks fine")
    assert ReviewRevisionResult(decision="REJECT", summary="widens scope").violations == ()


def test_datetimes_must_be_aware_and_are_normalised_to_utc() -> None:
    base = load("src_21_3_monitoring_event.json")
    with pytest.raises(ValidationError, match="timezone"):
        MonitoringEvent.model_validate(base | {"detected_at": "2026-09-30T02:05:00"})
    cest = timezone(timedelta(hours=2))
    event = MonitoringEvent.model_validate(
        base | {"detected_at": datetime(2026, 9, 30, 4, 5, tzinfo=cest)}
    )
    assert event.detected_at.tzinfo is UTC
    assert event.model_dump(mode="json")["detected_at"] == "2026-09-30T02:05:00Z"


def test_event_type_is_upper_snake_case() -> None:
    base = load("src_21_3_monitoring_event.json")
    with pytest.raises(ValidationError):
        MonitoringEvent.model_validate(base | {"event_type": "task failed"})


def _dq_result(status: DQStatus, *, blocking: bool) -> DQAgentResult:
    return DQAgentResult(
        agent=DQAgentName.CUSTOM_DQ,
        checkpoint=Checkpoint.TARGET,
        status=status,
        blocking=blocking,
        evaluated_at=datetime(2026, 9, 30, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("run_status", "result_status", "blocking"),
    [
        (DQStatus.PASS, DQStatus.ERROR, False),
        (DQStatus.PASS, DQStatus.CRITICAL, False),
        (DQStatus.WARNING, DQStatus.CRITICAL, True),
    ],
)
def test_dq_run_status_never_hides_failures(
    run_status: DQStatus, result_status: DQStatus, blocking: bool
) -> None:
    with pytest.raises(ValidationError):
        DQRunResult(
            request_key="k",
            pipeline_id="customer_daily_load",
            checkpoint=Checkpoint.TARGET,
            status=run_status,
            results=[_dq_result(result_status, blocking=blocking)],
            started_at=datetime(2026, 9, 30, tzinfo=UTC),
        )


# --- Review follow-ups: text hygiene, identifiers, strictness ----------------------------


@pytest.mark.parametrize(
    "text",
    ["dw.customer​", "delete ‮WHERE 1=1", "line\x00break", "bell\x07"],
    ids=["zero-width-space", "bidi-override", "nul", "control"],
)
def test_text_with_invisible_or_control_characters_is_rejected(text: str) -> None:
    plan = canonical_plan() | {"steps": [text]}
    with pytest.raises(ValidationError, match="control or invisible"):
        RemediationPlan.model_validate(plan)


def test_text_is_nfc_normalised_so_lookalikes_hash_alike() -> None:
    decomposed = "café cleanup"  # "e" + combining acute accent
    plan = RemediationPlan.model_validate(canonical_plan() | {"steps": [decomposed, "a\tb\nc"]})
    assert plan.steps == ("café cleanup", "a\tb\nc")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("resource", "dw.customer; DELETE FROM dw.customer"),
        ("resource", "a.b.c.d"),
        ("key_columns", ["customer_id) OR (1=1"]),
    ],
)
def test_assertion_identifiers_must_be_plain_names(field: str, value: Any) -> None:
    plan = copy.deepcopy(canonical_plan())
    plan["verification_plan"]["assertions"][0][field] = value
    with pytest.raises(ValidationError):
        RemediationPlan.model_validate(plan)


@pytest.mark.parametrize(
    "assertion",
    [
        pytest.param({**_OBJ, "bucket": "Bad_Bucket", "expected": {"value": True}}, id="bucket"),
        pytest.param(
            {"kind": "document_count", "resource": "crm.c", "expected": _eq(0),
             "filter": {"$or": [{"$where": "sleep(1000)"}]}},
            id="mongo-where",
        ),
    ],
)  # fmt: skip
def test_s3_and_mongo_assertions_are_constrained(assertion: dict[str, Any]) -> None:
    plan = copy.deepcopy(canonical_plan())
    plan["verification_plan"]["assertions"] = [{"assertion_id": "a1", **assertion}]
    with pytest.raises(ValidationError):
        RemediationPlan.model_validate(plan)


def test_write_tools_require_declared_writes_and_are_never_empty() -> None:
    no_writes = canonical_plan() | {"expected_write_operations": [], "verification_plan": {}}
    with pytest.raises(ValidationError, match="write_tools requires"):
        ApprovalPayload.model_validate(_payload(no_writes))
    with pytest.raises(ValidationError):
        RemediationPlan.model_validate(canonical_plan() | {"write_tools": []})


def test_assertion_ids_are_unique_across_the_plan() -> None:
    plan = copy.deepcopy(canonical_plan())
    plan["precondition_assertions"][0]["assertion_id"] = "post-rows"
    with pytest.raises(ValidationError, match="unique: post-rows"):
        RemediationPlan.model_validate(plan)


def test_hashed_numbers_and_flags_are_strict() -> None:
    for update in (
        {"retry_airflow_after_success": "off"},
        {"retry_airflow_after_success": 0},
        {"max_affected_rows": True},
        {"max_affected_rows": 2**53},
    ):
        with pytest.raises(ValidationError):
            RemediationPlan.model_validate(canonical_plan() | update)


def test_validated_sequences_cannot_be_edited_in_place() -> None:
    plan = RemediationPlan.model_validate(canonical_plan())
    assert isinstance(plan.steps, tuple)
    with pytest.raises(AttributeError):
        plan.steps.append("DROP TABLE dw.customer")  # type: ignore[attr-defined]


def test_sop_reference_can_pin_content_hash() -> None:
    assert SOPReference(sop_id="SOP-PG-001", content_sha256="f" * 64).content_sha256 == "f" * 64
    with pytest.raises(ValidationError):
        SOPReference(sop_id="SOP-PG-001", content_sha256="not-a-hash")


def test_dq_run_pass_needs_results_and_blocking_error_fails_closed() -> None:
    common: dict[str, Any] = {
        "request_key": "k",
        "pipeline_id": "customer_daily_load",
        "checkpoint": Checkpoint.TARGET,
        "started_at": datetime(2026, 9, 30, tzinfo=UTC),
    }
    with pytest.raises(ValidationError, match="at least one result"):
        DQRunResult(status=DQStatus.PASS, results=(), **common)
    with pytest.raises(ValidationError, match="blocking CRITICAL or ERROR"):
        DQRunResult(
            status=DQStatus.WARNING, results=(_dq_result(DQStatus.ERROR, blocking=True),), **common
        )
    ok = DQRunResult(
        status=DQStatus.PASS, results=(_dq_result(DQStatus.PASS, blocking=True),), **common
    )
    assert ok.status is DQStatus.PASS


def test_free_form_evidence_must_be_finite_json() -> None:
    base = load("src_21_3_monitoring_event.json")
    with pytest.raises(ValidationError):
        MonitoringEvent.model_validate(base | {"evidence": {"score": float("nan")}})


@pytest.mark.spec("S03-AC1")
def test_src_23_3_verbatim_example_differs_only_by_the_c9_agent_name() -> None:
    verbatim = load("src_23_3_dq_profile.json")
    verbatim["RECONCILIATION"][0]["agent"] = "reconciliation"  # exactly as printed in src §23.3
    with pytest.raises(ValidationError, match="'recon'"):
        DQProfile.model_validate(verbatim)
