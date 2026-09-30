"""Remediation plan and typed verification assertions (src §25.1, ADR-010).

A plan is part of the approval payload, so every field here is covered by the approval hash.
Completeness rules that decide whether a plan is *executable* (scope, stop conditions,
``max_affected_rows`` for DML, ...) live in the plan validator (S09). This module rejects only plans
that are structurally meaningless.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import (
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

from backend.domain.common import Contract, NonEmptyStr, RiskLevel, ShortText, SystemName
from backend.domain.dq import DQRuleRef

ComparisonOp = Literal["eq", "ne", "lt", "lte", "gt", "gte"]
FiniteFloat = Annotated[StrictFloat, Field(allow_inf_nan=False)]
ScalarValue = StrictBool | StrictInt | FiniteFloat | StrictStr


class CountExpectation(Contract):
    op: ComparisonOp
    value: Annotated[StrictInt, Field(ge=0)]


class ExistsExpectation(Contract):
    op: Literal["eq"] = "eq"
    value: StrictBool


class ScalarExpectation(Contract):
    op: ComparisonOp
    value: ScalarValue


class _Assertion(Contract):
    assertion_id: ShortText
    description: NonEmptyStr | None = None


class DuplicateCountAssertion(_Assertion):
    kind: Literal["duplicate_count"]
    resource: ShortText
    key_columns: list[ShortText] = Field(min_length=1)
    expected: CountExpectation


class RowCountAssertion(_Assertion):
    kind: Literal["row_count"]
    resource: ShortText
    expected: CountExpectation


class SqlScalarAssertion(_Assertion):
    """A single-value read-only query (validated read-only before execution, S19)."""

    kind: Literal["sql_scalar"]
    sql: NonEmptyStr
    expected: ScalarExpectation


class ObjectExistsAssertion(_Assertion):
    kind: Literal["object_exists"]
    bucket: ShortText
    key: NonEmptyStr
    expected: ExistsExpectation


class ObjectCountAssertion(_Assertion):
    kind: Literal["object_count"]
    bucket: ShortText
    prefix: str = ""
    expected: CountExpectation


class DocumentCountAssertion(_Assertion):
    kind: Literal["document_count"]
    resource: ShortText
    filter: dict[str, Any] = Field(default_factory=dict)
    expected: CountExpectation


VerificationAssertion = Annotated[
    DuplicateCountAssertion
    | RowCountAssertion
    | SqlScalarAssertion
    | ObjectExistsAssertion
    | ObjectCountAssertion
    | DocumentCountAssertion,
    Field(discriminator="kind"),
]


class VerificationPlan(Contract):
    assertions: list[VerificationAssertion] = Field(default_factory=list)


class RemediationPlan(Contract):
    target_system: SystemName
    target_resources: list[ShortText] = Field(default_factory=list)
    risk_level: RiskLevel
    preconditions: list[NonEmptyStr] = Field(default_factory=list)
    stop_conditions: list[NonEmptyStr] = Field(default_factory=list)
    steps: list[NonEmptyStr] = Field(default_factory=list)
    expected_write_operations: list[NonEmptyStr] = Field(default_factory=list)
    write_tools: list[ShortText] | None = None
    max_affected_rows: Annotated[StrictInt, Field(gt=0)] | None = None
    retry_airflow_after_success: bool = True
    precondition_assertions: list[VerificationAssertion] = Field(default_factory=list)
    verification_plan: VerificationPlan
    verification_rules: list[DQRuleRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _writes_need_assertions(self) -> Self:
        # Invariant 6 / ADR-010: a write is never verified by tool success alone.
        if self.expected_write_operations and not self.verification_plan.assertions:
            raise ValueError(
                "verification_plan.assertions must not be empty when "
                "expected_write_operations is non-empty"
            )
        return self
