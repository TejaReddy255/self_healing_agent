"""Export the v1 contract JSON Schemas: ``python -m backend.domain.export_schemas [--check]``.

The committed files in ``docs/contracts/v1/`` are the frozen contract (S03). ``--check`` exits 1
when they differ from the models, without writing anything.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from backend.domain.analysis import PipelineFailureRequest, StructuredAnalysis
from backend.domain.dq import (
    Checkpoint,
    DQAgentName,
    DQAgentResult,
    DQExecutionContext,
    DQProfile,
    DQRuleRef,
    DQRunResult,
    DQStatus,
)
from backend.domain.incident import ALLOWED_TRANSITIONS, IncidentCategory, IncidentStatus
from backend.domain.jobs import STAGE_TO_INCIDENT_STATUS, JobStatus, RemediationStage
from backend.domain.monitoring import MonitoringEvent, PipelineHealthResult
from backend.domain.remediation import RemediationPlan
from backend.domain.review import (
    ApprovalPayload,
    ApproveRequest,
    ReviewRevisionResult,
    ReviewStatus,
)
from backend.domain.sop import SOPProposal, SOPReference

CONTRACT_VERSION = "v1"
DEFAULT_OUT_DIR = Path(__file__).resolve().parents[2] / "docs" / "contracts" / CONTRACT_VERSION
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

MODELS: tuple[type[BaseModel], ...] = (
    PipelineFailureRequest,
    StructuredAnalysis,
    RemediationPlan,
    SOPReference,
    SOPProposal,
    ApprovalPayload,
    ReviewRevisionResult,
    ApproveRequest,
    MonitoringEvent,
    PipelineHealthResult,
    DQRuleRef,
    DQProfile,
    DQExecutionContext,
    DQAgentResult,
    DQRunResult,
)


def _state_machines() -> dict[str, Any]:
    enums = (
        IncidentStatus,
        IncidentCategory,
        ReviewStatus,
        JobStatus,
        RemediationStage,
        Checkpoint,
        DQStatus,
        DQAgentName,
    )
    return {
        "$schema": JSON_SCHEMA_DIALECT,
        "title": "StateMachines",
        "description": "Status vocabularies and transition tables (architecture §7).",
        "$defs": {e.__name__: {"enum": [m.value for m in e], "type": "string"} for e in enums},
        "x-incident-transitions": {
            status.value: sorted(t.value for t in targets)
            for status, targets in ALLOWED_TRANSITIONS.items()
        },
        "x-stage-to-incident-status": {
            stage.value: status.value for stage, status in STAGE_TO_INCIDENT_STATUS.items()
        },
    }


def build_schemas() -> dict[str, dict[str, Any]]:
    """File name → schema document, in a deterministic form."""
    documents: dict[str, dict[str, Any]] = {}
    for model in MODELS:
        schema = model.model_json_schema(mode="validation")
        documents[f"{model.__name__}.schema.json"] = {"$schema": JSON_SCHEMA_DIALECT, **schema}
    documents["StateMachines.schema.json"] = _state_machines()
    return documents


def render(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.domain.export_schemas")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR, help="output directory")
    parser.add_argument("--check", action="store_true", help="fail if the files are out of date")
    args = parser.parse_args(argv)
    out_dir: Path = args.out

    expected = {name: render(doc) for name, doc in build_schemas().items()}
    existing = {p.name for p in out_dir.glob("*.schema.json")} if out_dir.is_dir() else set()
    stale = sorted(existing - expected.keys())
    changed = sorted(
        name
        for name, text in expected.items()
        if not (out_dir / name).is_file() or (out_dir / name).read_text(encoding="utf-8") != text
    )

    if args.check:
        for name in changed:
            print(f"out of date: {name}")
        for name in stale:
            print(f"not produced by any model: {name}")
        return 1 if changed or stale else 0

    out_dir.mkdir(parents=True, exist_ok=True)
    for name in changed:
        (out_dir / name).write_text(expected[name], encoding="utf-8", newline="\n")
        print(f"wrote {name}")
    for name in stale:
        (out_dir / name).unlink()
        print(f"removed {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
