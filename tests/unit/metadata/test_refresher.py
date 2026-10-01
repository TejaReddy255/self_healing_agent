from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from backend.metadata.models import ReloadResult
from backend.metadata.refresher import MetadataRefresher
from backend.metadata.service import SYSTEM_SUBJECT, MetadataService
from backend.metadata.validators import Candidate
from tests.fakes.metadata_trees import KNOWN_ISSUE, PIPELINE, edit_doc, seed_tree, settings_for

pytestmark = [pytest.mark.spec("S04-AC13"), pytest.mark.timeout(30)]


class RecordingAuditor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, result: ReloadResult, subject: str, role: str) -> None:
        self.calls.append((result.outcome, subject))


def setup(tmp_path: Path) -> tuple[Path, MetadataService, RecordingAuditor, list[int]]:
    tree = seed_tree(tmp_path)
    auditor = RecordingAuditor()
    service = MetadataService(settings_for(tree), auditor=auditor, refresh_settle_s=0)
    service.load_initial()
    builds: list[int] = []
    real_build = service._build

    def counting_build() -> Candidate:
        builds.append(1)
        return real_build()

    service._build = counting_build  # type: ignore[method-assign]
    return tree, service, auditor, builds


async def test_valid_change_is_swapped_in_within_one_check(tmp_path: Path) -> None:
    tree, service, auditor, builds = setup(tmp_path)
    refresher = MetadataRefresher(service, 30)
    assert await refresher.run_once() is None  # nothing changed: no validation at all
    assert builds == []

    edit_doc(tree, PIPELINE, lambda d: d.update(name="Refreshed"))
    result = await refresher.run_once()
    assert result is not None and result.outcome == "RELOADED"
    pipeline = await service.get_pipeline("customer_daily_load")
    assert pipeline is not None and pipeline.name == "Refreshed"
    assert auditor.calls == [("RELOADED", SYSTEM_SUBJECT)]


async def test_invalid_change_keeps_last_known_good_and_is_not_rechecked(tmp_path: Path) -> None:
    tree, service, auditor, builds = setup(tmp_path)
    refresher = MetadataRefresher(service, 30)
    good = service.snapshot()
    original = (tree / KNOWN_ISSUE).read_bytes()

    edit_doc(tree, KNOWN_ISSUE, lambda d: d.update(pipeline_ids=["missing"]))
    result = await refresher.run_once()
    assert result is not None and result.outcome == "REJECTED"
    assert service.snapshot() is good
    assert service.state == "DEGRADED"
    assert service.health().rejected_candidate is not None

    for _ in range(3):  # unchanged broken tree: no re-validation, no repeated audit rows
        assert await refresher.run_once() is None
    assert len(builds) == 1

    (tree / KNOWN_ISSUE).write_bytes(original)
    # Restoring the original bytes makes the disk match the active tree again.
    assert await refresher.run_once() is None
    assert service.health().rejected_candidate is None
    assert service.state == "LOADED"
    assert auditor.calls == [("REJECTED", SYSTEM_SUBJECT)]


async def test_fixing_with_new_content_swaps_and_clears_rejection(tmp_path: Path) -> None:
    tree, service, auditor, _ = setup(tmp_path)
    refresher = MetadataRefresher(service, 30)
    edit_doc(tree, KNOWN_ISSUE, lambda d: d.update(pipeline_ids=["missing"]))
    await refresher.run_once()
    edit_doc(tree, KNOWN_ISSUE, lambda d: d.update(pipeline_ids=[], title="Fixed differently"))
    result = await refresher.run_once()
    assert result is not None and result.outcome == "RELOADED"
    assert service.state == "LOADED"
    assert auditor.calls == [("REJECTED", SYSTEM_SUBJECT), ("RELOADED", SYSTEM_SUBJECT)]


async def test_zero_interval_starts_no_task(tmp_path: Path) -> None:
    _, service, _, _ = setup(tmp_path)
    refresher = MetadataRefresher(service, 0)
    refresher.start()
    assert not refresher.enabled and not refresher.running
    await refresher.stop()


async def test_failing_check_is_logged_and_the_loop_continues(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _, service, _, _ = setup(tmp_path)
    calls = 0

    async def flaky_refresh() -> ReloadResult | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("disk unavailable")
        return None

    service.refresh_if_changed = flaky_refresh  # type: ignore[method-assign]
    ticks: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        ticks.append(seconds)
        if len(ticks) > 3:
            raise asyncio.CancelledError
        await asyncio.sleep(0)

    refresher = MetadataRefresher(service, 30, sleep=fake_sleep)
    with caplog.at_level(logging.ERROR), pytest.raises(asyncio.CancelledError):
        await refresher.run_forever()
    assert ticks == [30, 30, 30, 30]
    assert calls == 3
    assert "metadata refresh check failed" in caplog.text


async def test_start_and_stop_manage_the_background_task(tmp_path: Path) -> None:
    _, service, _, _ = setup(tmp_path)
    refresher = MetadataRefresher(service, 3600)
    refresher.start()
    assert refresher.running
    await refresher.stop()
    assert not refresher.running


async def test_reapplying_a_previously_rejected_tree_is_rejected_again(tmp_path: Path) -> None:
    tree, service, auditor, _ = setup(tmp_path)
    refresher = MetadataRefresher(service, 30)
    original = (tree / KNOWN_ISSUE).read_bytes()
    edit_doc(tree, KNOWN_ISSUE, lambda d: d.update(pipeline_ids=["missing"]))
    broken = (tree / KNOWN_ISSUE).read_bytes()

    assert (await refresher.run_once()).outcome == "REJECTED"  # type: ignore[union-attr]
    (tree / KNOWN_ISSUE).write_bytes(original)
    assert await refresher.run_once() is None  # restored: state back to LOADED
    (tree / KNOWN_ISSUE).write_bytes(broken)
    result = await refresher.run_once()
    assert result is not None and result.outcome == "REJECTED"
    assert service.state == "DEGRADED"
    assert auditor.calls == [("REJECTED", SYSTEM_SUBJECT), ("REJECTED", SYSTEM_SUBJECT)]


async def test_a_tree_still_being_written_is_not_reloaded(tmp_path: Path) -> None:
    _, service, _, builds = setup(tmp_path)
    service._refresh_settle_s = 0.01
    versions = iter(["changing-1", "changing-2"])

    async def moving_version() -> str:
        return next(versions)

    service._disk_version = moving_version  # type: ignore[method-assign]
    assert await service.refresh_if_changed() is None
    assert builds == []  # never validated a half-written tree
