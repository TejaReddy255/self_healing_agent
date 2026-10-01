from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from backend.metadata.models import ReloadResult
from backend.metadata.service import MetadataService
from backend.metadata.validators import Candidate, current_version
from tests.fakes.metadata_trees import KNOWN_ISSUE, PIPELINE, edit_doc, seed_tree, settings_for

pytestmark = pytest.mark.timeout(60)


class RecordingAuditor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(self, result: ReloadResult, subject: str, role: str) -> None:
        self.calls.append((result.outcome, subject, role))


def make_service(tree: Path, auditor: Any = None) -> MetadataService:
    service = MetadataService(settings_for(tree), auditor=auditor)
    service.load_initial()
    return service


def rename_pipeline(tree: Path, name: str) -> None:
    edit_doc(tree, PIPELINE, lambda d: d.update(name=name))


def break_tree(tree: Path) -> None:
    edit_doc(tree, KNOWN_ISSUE, lambda d: d.update(pipeline_ids=["missing_pipeline"]))


@pytest.mark.spec("S04-AC3")
async def test_invalid_reload_keeps_last_known_good(tmp_path: Path) -> None:
    tree = seed_tree(tmp_path)
    service = make_service(tree)
    before = service.snapshot()
    pipelines_before = await service.list_pipelines()

    break_tree(tree)
    result = await service.reload("admin@test", "admin")

    assert result.outcome == "REJECTED"
    assert {e.code for e in result.errors} == {"BROKEN_REFERENCE"}
    assert service.snapshot() is before
    assert service.health().version == before.version
    assert await service.list_pipelines() == pipelines_before
    assert service.state == "DEGRADED"
    rejected = service.health().rejected_candidate
    assert rejected is not None and rejected.version == result.version != before.version


@pytest.mark.spec("S04-AC3")
async def test_valid_reload_swaps_and_unchanged_is_reported(tmp_path: Path) -> None:
    tree = seed_tree(tmp_path)
    service = make_service(tree)
    old_version = service.snapshot().version

    rename_pipeline(tree, "Renamed")
    result = await service.reload()
    assert (result.outcome, result.previous_version) == ("RELOADED", old_version)
    pipeline = await service.get_pipeline("customer_daily_load")
    assert pipeline is not None and pipeline.name == "Renamed"

    again = await service.reload()
    assert again.outcome == "UNCHANGED"
    assert again.version == result.version == service.snapshot().version


@pytest.mark.spec("S04-AC3")
async def test_readers_never_observe_a_mixed_snapshot(tmp_path: Path) -> None:
    tree = seed_tree(tmp_path)
    service = make_service(tree)
    expected_name = {service.snapshot().version: "Customer Daily Load"}
    stop = asyncio.Event()
    observations = 0

    async def reader() -> None:
        nonlocal observations
        while not stop.is_set():
            snap = service.snapshot()
            pipeline = snap.get_pipeline("customer_daily_load")
            listed = snap.list_pipelines()
            assert pipeline is not None
            assert pipeline.name == expected_name[snap.version]
            assert listed == (pipeline,)
            observations += 1
            # Yield briefly; a pure sleep(0) spin would starve the reload thread of the GIL.
            await asyncio.sleep(0.001)

    readers = [asyncio.create_task(reader()) for _ in range(4)]
    for i in range(12):
        name = f"Generation {i}"
        rename_pipeline(tree, name)
        expected_name[current_version(tree, 1 << 20)] = name
        assert (await service.reload()).outcome == "RELOADED"
    stop.set()
    await asyncio.gather(*readers)  # re-raises any reader assertion
    assert observations > 12
    final = await service.get_pipeline("customer_daily_load")
    assert final is not None and final.name == "Generation 11"


@pytest.mark.spec("S04-AC3")
async def test_concurrent_reloads_are_serialised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = seed_tree(tmp_path)
    service = make_service(tree)
    active = 0
    peak = 0
    guard = threading.Lock()
    real_build = service._build

    def slow_build() -> Candidate:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.02)
            return real_build()
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(service, "_build", slow_build)
    results = await asyncio.gather(*(service.reload() for _ in range(5)))
    assert peak == 1
    assert [r.outcome for r in results] == ["UNCHANGED"] * 5


@pytest.mark.spec("S04-AC3")
async def test_every_reload_attempt_is_audited_and_audit_failure_does_not_undo(
    tmp_path: Path,
) -> None:
    tree = seed_tree(tmp_path)
    auditor = RecordingAuditor()
    service = make_service(tree, auditor)
    await service.reload("admin@test", "admin")  # unchanged
    rename_pipeline(tree, "New")
    await service.reload("admin@test", "admin")
    break_tree(tree)
    await service.reload("admin@test", "admin")
    assert auditor.calls == [
        ("UNCHANGED", "admin@test", "admin"),
        ("RELOADED", "admin@test", "admin"),
        ("REJECTED", "admin@test", "admin"),
    ]

    async def failing(result: ReloadResult, subject: str, role: str) -> None:
        raise RuntimeError("database down")

    flaky = make_service(seed_tree(tmp_path / "second"), failing)
    rename_pipeline(tmp_path / "second" / "metadata", "Still swapped")
    assert (await flaky.reload()).outcome == "RELOADED"
    pipeline = await flaky.get_pipeline("customer_daily_load")
    assert pipeline is not None and pipeline.name == "Still swapped"
