from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.db.settings import DatabaseSettings
from backend.metadata.service import SYSTEM_SUBJECT
from tests.conftest import auth
from tests.fakes.metadata_trees import KNOWN_ISSUE, PIPELINE, edit_doc, seed_tree, settings_for
from tests.integration.conftest import connect

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]


def reload_audit_rows(db: DatabaseSettings) -> list[dict[str, Any]]:
    async def fetch() -> list[dict[str, Any]]:
        conn = await connect(db)
        try:
            rows = await conn.fetch(
                "SELECT status, actor_subject, actor_role, details FROM operation_audit "
                "WHERE event_type = 'METADATA_RELOAD' ORDER BY id"
            )
        finally:
            await conn.close()
        return [
            dict(r) | {"details": json.loads(r["details"]) if isinstance(r["details"], str)
                       else r["details"]}
            for r in rows
        ]  # fmt: skip

    return asyncio.run(fetch())


@pytest.mark.spec("S04-AC8")
def test_every_reload_attempt_writes_one_audit_row(
    migrated_db: DatabaseSettings, make_settings: Callable[..., Settings], tmp_path: Path
) -> None:
    tree = seed_tree(tmp_path)
    app = create_app(make_settings(), db_settings=migrated_db, metadata_settings=settings_for(tree))
    with TestClient(app) as client:
        assert client.post("/metadata/reload", headers=auth("admin")).status_code == 200
        edit_doc(tree, PIPELINE, lambda d: d.update(name="Audited"))
        reloaded = client.post("/metadata/reload", headers=auth("admin")).json()
        edit_doc(tree, KNOWN_ISSUE, lambda d: d.update(pipeline_ids=["missing"]))
        assert client.post("/metadata/reload", headers=auth("admin")).status_code == 422
        assert client.post("/metadata/reload", headers=auth("viewer")).status_code == 403

    rows = reload_audit_rows(migrated_db)
    assert [(r["status"], r["actor_subject"], r["actor_role"]) for r in rows] == [
        ("UNCHANGED", "admin@test", "admin"),
        ("RELOADED", "admin@test", "admin"),
        ("REJECTED", "admin@test", "admin"),
    ]
    assert rows[1]["details"]["version"] == reloaded["version"]
    assert rows[1]["details"]["previous_version"] == reloaded["previous_version"]
    assert rows[2]["details"]["error_count"] == 1


@pytest.mark.spec("S04-AC8")
def test_automatic_refresh_is_audited_as_the_system(
    migrated_db: DatabaseSettings, make_settings: Callable[..., Settings], tmp_path: Path
) -> None:
    tree = seed_tree(tmp_path)
    app = create_app(make_settings(), db_settings=migrated_db, metadata_settings=settings_for(tree))
    with TestClient(app) as client:
        edit_doc(tree, PIPELINE, lambda d: d.update(name="Refreshed"))
        refresher = client.app.state.metadata_refresher  # type: ignore[attr-defined]
        assert client.portal is not None
        result = client.portal.call(refresher.run_once)
        assert result is not None and result.outcome == "RELOADED"

    rows = reload_audit_rows(migrated_db)
    assert [(r["status"], r["actor_subject"], r["actor_role"]) for r in rows] == [
        ("RELOADED", SYSTEM_SUBJECT, "service")
    ]
