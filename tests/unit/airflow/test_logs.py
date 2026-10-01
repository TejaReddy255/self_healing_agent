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
def test_capped_download_says_it_is_not_the_end() -> None:
    content, truncated, _ = tail_and_redact(raw("middle of the log\n", capped=True), 1000)
    assert truncated and content.startswith("[… truncated earlier chars …]")
    assert content.endswith("this is NOT the end of the log …]")


def test_window_start_partial_line_is_dropped() -> None:
    # The byte window began inside "password=hunter2"; the visible remainder would escape
    # redaction, so the partial first line is dropped.
    trimmed = RawLog(
        text="rd=hunter2 retry\nINFO ok\n", downloaded_bytes=99, download_capped=False,
        tail_trimmed=True,
    )  # fmt: skip
    content, truncated, _ = tail_and_redact(trimmed, 1000)
    assert "hunter2" not in content and truncated and content.endswith("INFO ok\n")


def test_ansi_and_control_characters_are_stripped() -> None:
    content, _, _ = tail_and_redact(raw("\x1b[31mERROR\x1b[0m bell\x07 tab\tok\n"), 1000)
    assert content == "ERROR bell tab\tok\n"


@pytest.mark.spec("S05-AC3")
def test_redaction_work_is_bounded_on_one_huge_line() -> None:
    import time

    line = "a" * 3_000_000 + " password=Sup3rS3cret"
    started = time.perf_counter()
    content, truncated, _ = tail_and_redact(raw(line), 20_000)
    assert time.perf_counter() - started < 2.0
    assert truncated and "Sup3rS3cret" not in content


@pytest.mark.spec("S05-AC3")
async def test_chunked_download_keeps_the_real_end_within_the_memory_window() -> None:
    fake = canonical_scenario()
    fake.log_chunk_size = 1000
    big = "".join(f"noise {i:07d}\n" for i in range(4000)) + "ERROR the real cause\n"
    fake.set_log(DAG, RUN, "load_customers", 2, big)
    with fake_airflow(fake):
        client, _ = make_client(
            airflow_log_max_chars=2048,
            airflow_log_max_download_bytes=8192,
            airflow_log_download_ceiling_bytes=1_048_576,
        )
        log = await client.get_task_log(DAG, RUN, "load_customers", 2)
        assert len(log.text.encode()) <= 8192
        assert log.text.endswith("ERROR the real cause\n")
        assert log.tail_trimmed and not log.download_capped
        assert log.downloaded_bytes == len(big.encode())
        await client.aclose()


@pytest.mark.spec("S05-AC3")
async def test_download_stops_at_the_ceiling_and_is_marked() -> None:
    fake = canonical_scenario()
    fake.log_chunk_size = 1000
    huge = "z" * 50_000 + "\nERROR beyond the ceiling\n"
    fake.set_log(DAG, RUN, "load_customers", 2, huge)
    with fake_airflow(fake):
        client, _ = make_client(
            airflow_log_max_chars=2048,
            airflow_log_max_download_bytes=8192,
            airflow_log_download_ceiling_bytes=20_000,
        )
        log = await client.get_task_log(DAG, RUN, "load_customers", 2)
        assert log.download_capped
        assert 20_000 <= log.downloaded_bytes <= 20_000 + 1000  # stopped within one chunk
        assert "beyond the ceiling" not in log.text
        tools = AirflowReadTools(client, [DAG], log_max_chars=2048)
        shaped = await tools.get_task_logs(DAG, RUN, "load_customers", 2)
        assert shaped.truncated and "NOT the end of the log" in shaped.content
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


async def test_mapped_tasks_fetch_the_failed_instance_log() -> None:
    fake = canonical_scenario()
    mapped_run = "manual__mapped"
    fake.add_run(DAG, mapped_run, state="failed")
    fake.add_task(DAG, mapped_run, "load_part", state="success", try_number=1, map_index=0)
    fake.add_task(DAG, mapped_run, "load_part", state="failed", try_number=1, map_index=1)
    fake.add_task(DAG, mapped_run, "load_part", state="success", try_number=1, map_index=2)
    fake.set_log(DAG, mapped_run, "load_part", 1, "part 0 fine\n", map_index=0)
    fake.set_log(DAG, mapped_run, "load_part", 1, "ERROR part 1 broke\n", map_index=1)
    fake.set_log(DAG, mapped_run, "load_part", 1, "part 2 fine\n", map_index=2)
    with fake_airflow(fake):
        client, _ = make_client()
        tools = AirflowReadTools(client, [DAG], log_max_chars=1000)
        log = await tools.get_task_logs(DAG, mapped_run, "load_part")
        assert (log.map_index, log.content) == (1, "ERROR part 1 broke\n")
        explicit = await tools.get_task_logs(DAG, mapped_run, "load_part", 1, map_index=2)
        assert explicit.content == "part 2 fine\n"
        await client.aclose()
