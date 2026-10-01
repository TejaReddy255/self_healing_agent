from __future__ import annotations

import pytest

from backend.airflow.logs import tail_and_redact
from backend.airflow.models import RawLog
from backend.airflow.tools import AirflowReadTools
from tests.fakes.airflow import canonical_scenario, fake_airflow, make_client

DAG = "customer_daily_load"
RUN = "scheduled__2026-10-01T02:00:00+00:00"


def raw(text: str, capped: bool = False) -> RawLog:
    return RawLog(text=text, downloaded_bytes=len(text.encode()), download_capped=capped)


@pytest.mark.spec("S05-AC3")
def test_long_logs_keep_the_tail_with_a_marker() -> None:
    text = "".join(f"line {i:05d}\n" for i in range(5000)) + "ERROR final failure\n"
    content, truncated, original = tail_and_redact(raw(text), 1000)
    assert truncated and original == len(text)
    assert content.startswith("[… truncated ")
    assert content.endswith("ERROR final failure\n")
    assert len(content) < 1000 + 60


@pytest.mark.spec("S05-AC3")
def test_short_logs_are_kept_whole() -> None:
    content, truncated, _ = tail_and_redact(raw("all good\n"), 1000)
    assert (content, truncated) == ("all good\n", False)


@pytest.mark.spec("S05-AC3")
@pytest.mark.parametrize(
    "secret_line",
    [
        "connecting to postgres://etl:Sup3rS3cret@db:5432/dw",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
        "using password=Sup3rS3cret for retry",
        "config api_key: Sup3rS3cret",
    ],
)
def test_credentials_in_logs_are_redacted(secret_line: str) -> None:
    content, _, _ = tail_and_redact(raw(f"INFO start\n{secret_line}\nINFO end\n"), 1000)
    assert "Sup3rS3cret" not in content and "eyJhbGciOiJIUzI1NiJ9" not in content
    assert "INFO end" in content


@pytest.mark.spec("S05-AC3")
def test_redaction_happens_before_truncation() -> None:
    # The secret straddles the truncation boundary: truncating first would leave part of it.
    text = "x" * 100 + "password=Sup3rS3cretValueThatIsLong\n" + "y" * 10
    content, _, _ = tail_and_redact(raw(text), 30)
    assert "S3cretValue" not in content


@pytest.mark.spec("S05-AC3")
def test_capped_download_is_marked_truncated() -> None:
    content, truncated, _ = tail_and_redact(raw("tail only\n", capped=True), 1000)
    assert truncated and content.startswith("[… truncated earlier chars …]")


@pytest.mark.spec("S05-AC3")
async def test_download_keeps_a_rolling_tail_and_stops_at_the_ceiling() -> None:
    fake = canonical_scenario()
    big = "".join(f"noise {i:07d}\n" for i in range(4000)) + "ERROR the real cause\n"
    fake.set_log(DAG, RUN, "load_customers", 2, big)
    with fake_airflow(fake):
        client, _ = make_client(airflow_log_max_download_bytes=4096)
        log = await client.get_task_log(DAG, RUN, "load_customers", 2)
        assert len(log.text.encode()) <= 4096
        # 4000 lines x 14 bytes is under 16 x 4 KiB, so the whole stream is read: the tail holds
        # the real end of the log.
        assert log.text.endswith("ERROR the real cause\n")
        assert not log.download_capped

        huge = "z" * (4096 * 16 + 10_000) + "\nERROR beyond the ceiling\n"
        fake.set_log(DAG, RUN, "load_customers", 2, huge)
        capped = await client.get_task_log(DAG, RUN, "load_customers", 2)
        assert capped.download_capped and capped.downloaded_bytes >= 4096 * 16
        await client.aclose()


@pytest.mark.spec("S05-AC3")
async def test_get_task_logs_tool_defaults_to_latest_try() -> None:
    with fake_airflow(canonical_scenario()):
        client, _ = make_client()
        tools = AirflowReadTools(client, [DAG], log_max_chars=1000)
        log = await tools.get_task_logs(DAG, RUN, "load_customers")
        assert log.try_number == 2
        assert "duplicate key" in log.content and not log.truncated
        await client.aclose()
