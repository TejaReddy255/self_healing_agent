"""Identifier checks and quoting (S07-AC9, pattern half)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mcp_servers.common.errors import ToolRejected
from mcp_servers.common.identifiers import IDENT, check_identifier, qualified, quote, split_table

INJECTIONS = [
    "customer; DROP TABLE x",
    '"a"."b"--',
    "`customer`",
    "customer`; DROP TABLE x; --",
    "cust" + chr(0x2019) + "omer",  # unicode right single quote
    "cust" + chr(0x201C) + "omer",  # unicode left double quote
    "customer--",
    "customer/*",
    "customer name",
    "1customer",
    "",
    "a" * 64,
    "customer\x00",
    "customer\n",
    "dw.customer",
]


@pytest.mark.spec("S07-AC9")
@pytest.mark.parametrize("value", INJECTIONS)
def test_injection_attempts_are_rejected(value: str) -> None:
    with pytest.raises(ToolRejected):
        check_identifier(value, "column")


@pytest.mark.spec("S07-AC9")
@pytest.mark.parametrize(
    "value",
    [
        "dw",
        "customer; DROP TABLE x",
        "dw.customer.extra",
        "other.customer",
        'dw."customer"',
        "dw.`customer`",
        "dw.customer--",
        ".customer",
    ],
)
def test_split_table_rejects_bad_or_foreign_names(value: str) -> None:
    with pytest.raises(ToolRejected):
        split_table(value, ["dw"])


@pytest.mark.spec("S07-AC9")
def test_split_table_accepts_allowed_schema_case_insensitively() -> None:
    assert split_table("DW.customer", ["dw"]) == ("DW", "customer")


@pytest.mark.spec("S07-AC9")
def test_quoting_per_dialect() -> None:
    assert qualified("dw", "customer", "postgres") == '"dw"."customer"'
    assert qualified("sales", "customer", "mysql") == "`sales`.`customer`"
    with pytest.raises(ToolRejected):
        quote('a"b', "postgres")


@pytest.mark.spec("S07-AC9")
@given(st.text(max_size=80))
def test_anything_accepted_is_safe_to_quote(value: str) -> None:
    try:
        check_identifier(value)
    except ToolRejected:
        return
    assert IDENT.fullmatch(value)
    for ch in "\"`'; -/\\\x00\n":
        assert ch not in value
