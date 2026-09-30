"""Remediation plan and typed verification assertions (src §25.1, ADR-010).

A plan is part of the approval payload, so every field here is covered by the approval hash.
``RemediationPlan`` rejects only plans that are structurally meaningless or unsafe to hash.
``check_approvable`` adds the rules every approval payload enforces, and the plan validator (S09)
decides whether a plan is *executable* (scope, stop conditions, ``max_affected_rows`` for DML, ...).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

from backend.domain.common import (
    MAX_SAFE_INTEGER,
    BucketName,
    ColumnName,
    Contract,
    JsonObject,
    NonEmptyStr,
    QualifiedName,
    RiskLevel,
    ShortText,
    SystemName,
)
from backend.domain.dq import DQRuleRef

ComparisonOp = Literal["eq", "ne", "lt", "lte", "gt", "gte"]
FiniteFloat = Annotated[StrictFloat, Field(allow_inf_nan=False)]
ScalarValue = StrictBool | StrictInt | FiniteFloat | StrictStr
Count = Annotated[StrictInt, Field(ge=0, le=MAX_SAFE_INTEGER)]

# Server-side JavaScript operators: never allowed in an approved filter (S19/S24 also deny them).
_MONGO_CODE_OPERATORS = frozenset({"$where", "$function", "$accumulator"})


class CountExpectation(Contract):
    op: ComparisonOp
    value: Count


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
    resource: QualifiedName
    key_columns: tuple[ColumnName, ...] = Field(min_length=1, max_length=32)
    expected: CountExpectation


class RowCountAssertion(_Assertion):
    kind: Literal["row_count"]
    resource: QualifiedName
    expected: CountExpectation


class SqlScalarAssertion(_Assertion):
    """A single-value read-only query (validated read-only before execution, S19)."""

    kind: Literal["sql_scalar"]
    sql: NonEmptyStr
    expected: ScalarExpectation


class ObjectExistsAssertion(_Assertion):
    kind: Literal["object_exists"]
    bucket: BucketName
    key: Annotated[NonEmptyStr, Field(max_length=1024)]
    expected: ExistsExpectation


class ObjectCountAssertion(_Assertion):
    kind: Literal["object_count"]
    bucket: BucketName
    prefix: Annotated[str, Field(max_length=1024)] = ""
    expected: CountExpectation


def _walk_keys(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def _no_code_operators(value: JsonObject) -> JsonObject:
    if found := sorted(_MONGO_CODE_OPERATORS.intersection(_walk_keys(value))):
        raise ValueError(f"filter must not use server-side code operators: {', '.join(found)}")
    return value


class DocumentCountAssertion(_Assertion):
    kind: Literal["document_count"]
    resource: QualifiedName
    filter: Annotated[JsonObject, AfterValidator(_no_code_operators)] = Field(default_factory=dict)
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
    assertions: tuple[VerificationAssertion, ...] = ()


class RemediationPlan(Contract):
    target_system: SystemName
    target_resources: tuple[ShortText, ...] = ()
    risk_level: RiskLevel
    preconditions: tuple[NonEmptyStr, ...] = ()
    stop_conditions: tuple[NonEmptyStr, ...] = ()
    steps: tuple[NonEmptyStr, ...] = ()
    expected_write_operations: tuple[NonEmptyStr, ...] = ()
    # None: any approved-target write tool, still bounded by len(expected_write_operations).
    # When declared, the write guard also refuses tools outside this list (agent-design §4).
    write_tools: Annotated[tuple[ShortText, ...], Field(min_length=1)] | None = None
    max_affected_rows: Annotated[StrictInt, Field(gt=0, le=MAX_SAFE_INTEGER)] | None = None
    retry_airflow_after_success: StrictBool = True
    precondition_assertions: tuple[VerificationAssertion, ...] = ()
    verification_plan: VerificationPlan
    verification_rules: tuple[DQRuleRef, ...] = ()

    @model_validator(mode="after")
    def _unique_assertion_ids(self) -> Self:
        ids = [
            a.assertion_id
            for a in (*self.precondition_assertions, *self.verification_plan.assertions)
        ]
        if duplicates := sorted({i for i in ids if ids.count(i) > 1}):
            raise ValueError(f"assertion_id values must be unique: {', '.join(duplicates)}")
        return self


def check_approvable(plan: RemediationPlan) -> None:
    """Rules an approvable plan must meet beyond its own structure (S03-AC3).

    Checked on ``ApprovalPayload``, not on ``RemediationPlan``: an RCA whose proposed plan breaks
    them keeps its findings and the plan is marked non-executable (S09 N5) instead of failing the
    whole analysis.
    """
    # Invariant 6 / ADR-010: a write is never verified by tool success alone.
    if plan.expected_write_operations and not plan.verification_plan.assertions:
        raise ValueError(
            "verification_plan.assertions must not be empty when "
            "expected_write_operations is non-empty"
        )
    if plan.write_tools and not plan.expected_write_operations:
        raise ValueError("write_tools requires at least one expected_write_operations entry")
