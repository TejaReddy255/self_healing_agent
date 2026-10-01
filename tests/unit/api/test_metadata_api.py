from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from tests.conftest import auth
from tests.fakes.metadata_trees import KNOWN_ISSUE, PIPELINE, edit_doc, seed_tree, settings_for

ROUTES = [
    "/metadata/health",
    "/metadata/pipelines",
    "/metadata/pipelines/customer_daily_load",
    "/metadata/sops",
    "/metadata/known-issues",
    "/metadata/business-rules",
    "/metadata/runbooks",
    "/metadata/ownership",
]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    return seed_tree(tmp_path)


@pytest.fixture
def make_app(make_settings: Callable[..., Settings], tree: Path) -> Callable[[], FastAPI]:
    def factory() -> FastAPI:
        return create_app(make_settings(), metadata_settings=settings_for(tree))

    return factory


@pytest.fixture
def client(make_app: Callable[[], FastAPI]) -> Iterator[TestClient]:
    with TestClient(make_app(), raise_server_exceptions=False) as test_client:
        yield test_client


def metadata_readiness(client: TestClient) -> dict[str, str | None]:
    body = client.get("/health/ready", headers=auth("viewer")).json()
    component: dict[str, str | None] = body["components"]["metadata"]
    return component


# --- AC10: read API --------------------------------------------------------------------------


@pytest.mark.spec("S04-AC10")
def test_read_routes_return_documented_shapes(client: TestClient) -> None:
    health = client.get("/metadata/health", headers=auth("viewer")).json()
    version = health["version"]
    assert health["state"] == "LOADED" and health["errors"] == []
    assert health["counts_by_type"]["SOP"] == 2

    pipelines = client.get("/metadata/pipelines", headers=auth("viewer")).json()
    assert pipelines["version"] == version
    assert [p["pipeline_id"] for p in pipelines["items"]] == ["customer_daily_load"]
    one = client.get("/metadata/pipelines/customer_daily_load", headers=auth("viewer")).json()
    assert one["source"]["schema"] == "sales"
    assert one["self_healing"]["human_approval_required"] is True

    sops = client.get(
        "/metadata/sops", params={"pipeline_id": "customer_daily_load"}, headers=auth("viewer")
    ).json()
    assert [s["sop_id"] for s in sops["items"]] == ["SOP-PG-001", "SOP-PG-002"]
    assert all(len(s["content_sha256"]) == 64 for s in sops["items"])
    for route, key in [
        ("/metadata/known-issues", "issue_id"),
        ("/metadata/business-rules", "rule_set_id"),
        ("/metadata/runbooks", "runbook_id"),
        ("/metadata/ownership", "ownership_id"),
    ]:
        body = client.get(
            route, params={"pipeline_id": "customer_daily_load"}, headers=auth("viewer")
        ).json()
        assert body["version"] == version and body["items"], route
        assert all(key in item for item in body["items"])


@pytest.mark.spec("S04-AC10")
def test_filters_and_not_found(client: TestClient) -> None:
    by_category = client.get(
        "/metadata/sops", params={"category": "MISSING_PARTITION"}, headers=auth("viewer")
    ).json()
    assert [s["sop_id"] for s in by_category["items"]] == ["SOP-PG-001"]
    drafts = client.get("/metadata/sops", params={"status": "DRAFT"}, headers=auth("viewer"))
    assert drafts.json()["items"] == []
    for path, params in [
        ("/metadata/pipelines/nope", {}),
        ("/metadata/sops", {"pipeline_id": "nope"}),
        ("/metadata/ownership", {"pipeline_id": "nope"}),
    ]:
        response = client.get(path, params=params, headers=auth("viewer"))
        assert response.status_code == 404
        assert response.json()["error_code"] == "METADATA_NOT_FOUND"


@pytest.mark.spec("S04-AC10")
@pytest.mark.parametrize("route", ROUTES)
def test_read_routes_require_authentication(client: TestClient, route: str) -> None:
    assert client.get(route).status_code == 401


@pytest.mark.spec("S04-AC10")
def test_readiness_reports_ok_degraded_and_recovers(client: TestClient, tree: Path) -> None:
    assert metadata_readiness(client) == {"status": "ok", "detail": None}
    original = (tree / KNOWN_ISSUE).read_bytes()

    edit_doc(tree, KNOWN_ISSUE, lambda d: d.update(pipeline_ids=["missing"]))
    rejected = client.post("/metadata/reload", headers=auth("admin"))
    assert rejected.status_code == 422
    body = rejected.json()
    assert body["error_code"] == "METADATA_INVALID"
    assert [e["code"] for e in body["details"]["errors"]] == ["BROKEN_REFERENCE"]
    assert body["details"]["active_version"] != body["details"]["candidate_version"]
    status = metadata_readiness(client)
    assert status["status"] == "degraded" and "rejected: 1 errors" in str(status["detail"])
    # Last-known-good is still served.
    assert client.get("/metadata/pipelines", headers=auth("viewer")).json()["items"]

    (tree / KNOWN_ISSUE).write_bytes(original)
    assert client.post("/metadata/reload", headers=auth("admin")).json()["outcome"] == "UNCHANGED"
    assert metadata_readiness(client)["status"] == "ok"


# --- AC8: reload is admin-only ----------------------------------------------------------------


@pytest.mark.spec("S04-AC8")
@pytest.mark.parametrize("role", ["viewer", "operator", "reviewer", "service"])
def test_reload_is_forbidden_for_non_admins(client: TestClient, role: str) -> None:
    response = client.post("/metadata/reload", headers=auth(role))
    assert response.status_code == 403


@pytest.mark.spec("S04-AC8")
def test_admin_reload_swaps_valid_changes(client: TestClient, tree: Path) -> None:
    edit_doc(tree, PIPELINE, lambda d: d.update(name="Renamed by admin"))
    result = client.post("/metadata/reload", headers=auth("admin")).json()
    assert result["outcome"] == "RELOADED"
    assert result["health"]["version"] == result["version"]
    one = client.get("/metadata/pipelines/customer_daily_load", headers=auth("viewer")).json()
    assert one["name"] == "Renamed by admin"


# --- AC14: startup with an invalid tree ----------------------------------------------------------


@pytest.mark.spec("S04-AC14")
def test_invalid_tree_at_startup_starts_empty_and_recovers(
    make_app: Callable[[], FastAPI], tree: Path
) -> None:
    original = (tree / KNOWN_ISSUE).read_bytes()
    edit_doc(tree, KNOWN_ISSUE, lambda d: d.update(pipeline_ids=["missing"]))
    with TestClient(make_app(), raise_server_exceptions=False) as client:
        assert client.get("/health").status_code == 200
        ready = client.get("/health/ready", headers=auth("viewer"))
        assert ready.status_code == 503
        assert ready.json()["components"]["metadata"] == {"status": "down", "detail": "1 errors"}
        health = client.get("/metadata/health", headers=auth("viewer")).json()
        assert health["state"] == "EMPTY" and health["version"] is None
        assert [e["code"] for e in health["errors"]] == ["BROKEN_REFERENCE"]
        pipeline = client.get("/metadata/pipelines/customer_daily_load", headers=auth("viewer"))
        assert pipeline.status_code == 404

        (tree / KNOWN_ISSUE).write_bytes(original)
        reload = client.post("/metadata/reload", headers=auth("admin"))
        assert reload.status_code == 200 and reload.json()["outcome"] == "RELOADED"
        assert metadata_readiness(client)["status"] == "ok"
