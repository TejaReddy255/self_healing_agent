from __future__ import annotations

import inspect

import pytest

from backend.airflow.errors import AirflowToolDenied
from backend.airflow.tools import AirflowReadTools
from tests.fakes.airflow import canonical_scenario, fake_airflow, make_client

DAG = "customer_daily_load"
RUN = "scheduled__2026-10-01T02:00:00+00:00"


@pytest.mark.spec("S05-AC7")
@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("get_workflow_run_details", ("other_dag", RUN)),
        ("get_failed_tasks_in_run", ("other_dag", RUN)),
        ("get_task_logs", ("other_dag", RUN, "load_customers")),
    ],
)
async def test_tools_reject_dags_outside_the_allowlist_without_calling_airflow(
    method: str, args: tuple[str, ...]
) -> None:
    with fake_airflow(canonical_scenario()) as fake:
        client, _ = make_client()
        tools = AirflowReadTools(client, [DAG], log_max_chars=1000)
        with pytest.raises(AirflowToolDenied):
            await getattr(tools, method)(*args)
        assert fake.calls == []
        await client.aclose()


@pytest.mark.spec("S05-AC7")
async def test_tools_answer_for_allowed_dags() -> None:
    with fake_airflow(canonical_scenario()):
        client, _ = make_client()
        tools = AirflowReadTools(client, [DAG], log_max_chars=1000)
        details = await tools.get_workflow_run_details(DAG, RUN)
        assert details.run.state == "failed"
        assert details.task_states == {"failed": 1, "success": 1, "upstream_failed": 1}
        failed = await tools.get_failed_tasks_in_run(DAG, RUN)
        assert [t.task_id for t in failed] == ["dq_target_check", "load_customers"]
        await client.aclose()


@pytest.mark.spec("S05-AC7")
def test_tools_expose_no_write_capability() -> None:
    public = {
        name
        for name, member in inspect.getmembers(AirflowReadTools)
        if not name.startswith("_") and callable(member)
    }
    assert public == {"get_workflow_run_details", "get_failed_tasks_in_run", "get_task_logs"}
    forbidden = ("trigger", "patch", "pause", "clear", "delete", "post")
    assert not any(word in name for name in public for word in forbidden)


@pytest.mark.spec("S05-AC7")
def test_tools_depend_on_the_read_only_protocol() -> None:
    from typing import get_type_hints

    from backend.airflow.client import AirflowClient, AirflowReader

    hints = get_type_hints(AirflowReadTools.__init__)
    assert hints["client"] is AirflowReader
    reader_methods = {n for n in dir(AirflowReader) if not n.startswith("_")}
    assert {"trigger_dag_run", "patch_dag"}.isdisjoint(reader_methods)
    assert {"trigger_dag_run", "patch_dag"} <= {n for n in dir(AirflowClient)}
