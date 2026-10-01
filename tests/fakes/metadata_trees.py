"""Metadata fixture trees for tests: a copy of the repository seed that each test may edit."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from backend.metadata.settings import DEFAULT_METADATA_DIR, MetadataSettings


def seed_tree(tmp_path: Path) -> Path:
    """Copy the committed seed metadata into ``tmp_path/metadata`` and return it."""
    target = tmp_path / "metadata"
    shutil.copytree(DEFAULT_METADATA_DIR, target)
    return target


def read_doc(tree: Path, relative: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((tree / relative).read_text(encoding="utf-8"))
    return data


def write_doc(tree: Path, relative: str, data: dict[str, Any]) -> Path:
    path = tree / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8", newline="\n")
    return path


def write_text(tree: Path, relative: str, text: str) -> Path:
    path = tree / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def edit_doc(tree: Path, relative: str, change: Callable[[dict[str, Any]], None]) -> None:
    data = read_doc(tree, relative)
    change(data)
    write_doc(tree, relative, data)


def settings_for(tree: Path, **overrides: Any) -> MetadataSettings:
    values: dict[str, Any] = {"metadata_dir": tree, "metadata_refresh_interval_s": 0}
    values.update(overrides)
    return MetadataSettings(_env_file=None, **values)


PIPELINE = "pipelines/customer_daily_load.yaml"
RULES = "dq_rules/customer_rules.yaml"
SOP_DUPLICATES = "sops/duplicate_data.yaml"
SOP_PARTITION = "sops/missing_partition.yaml"
RUNBOOK_PG = "runbooks/postgres_load_failure.yaml"
KNOWN_ISSUE = "known_issues/duplicate_incremental_replay.yaml"
OWNERSHIP = "ownership/customer_domain.yaml"
HISTORY = "historical_incidents/HIST-001.yaml"
