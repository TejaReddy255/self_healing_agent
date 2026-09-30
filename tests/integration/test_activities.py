from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from functools import partial

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.app import create_app
from backend.config import Settings
from backend.db.settings import DatabaseSettings
from backend.domain.audit import Activity
from backend.services.activity_service import ActivityService
from tests.conftest import ROLE_TOKENS, auth

pytestmark = [pytest.mark.integration, pytest.mark.timeout(30)]


@pytest.fixture
def db_app(migrated_db: DatabaseSettings, make_settings: Callable[..., Settings]) -> FastAPI:
    return create_app(make_settings(), db_settings=migrated_db)


@pytest.fixture
def client(db_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(db_app) as test_client:
        yield test_client


def publish(client: TestClient, type_: str, **details: object) -> Activity:
    """Publish on the app's own event loop, as a request handler would."""
    service: ActivityService = client.app.state.activity_service  # type: ignore[attr-defined]
    assert client.portal is not None
    result: Activity = client.portal.call(partial(service.publish, type_, details))
    return result


# --- S02-AC7: history API ---------------------------------------------------------------------


@pytest.mark.spec("S02-AC7")
def test_keyset_pagination_newest_first(client: TestClient) -> None:
    published = [publish(client, f"event.{i}", n=i) for i in range(5)]
    ids = [a.id for a in published]

    page1 = client.get("/activities", params={"limit": 2}, headers=auth("viewer"))
    assert page1.status_code == 200
    assert [a["id"] for a in page1.json()] == [ids[4], ids[3]]

    page2 = client.get(
        "/activities", params={"limit": 2, "before_id": ids[3]}, headers=auth("viewer")
    )
    assert [a["id"] for a in page2.json()] == [ids[2], ids[1]]

    page3 = client.get(
        "/activities", params={"limit": 2, "before_id": ids[1]}, headers=auth("viewer")
    )
    assert [a["id"] for a in page3.json()] == [ids[0]]
    assert page1.json()[0]["details"] == {"n": 4}


@pytest.mark.spec("S02-AC7")
@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 501}, {"before_id": 0}])
def test_limit_bounds_are_enforced(client: TestClient, params: dict[str, int]) -> None:
    response = client.get("/activities", params=params, headers=auth("viewer"))
    assert response.status_code == 422
    assert response.json()["error_code"] == "VALIDATION_ERROR"


@pytest.mark.spec("S02-AC7")
def test_default_limit_and_max_limit(client: TestClient) -> None:
    assert client.get("/activities", headers=auth("viewer")).status_code == 200
    assert (
        client.get("/activities", params={"limit": 500}, headers=auth("viewer")).status_code == 200
    )


@pytest.mark.spec("S02-AC7")
def test_activities_require_authentication(client: TestClient) -> None:
    assert client.get("/activities").status_code == 401
    assert client.get("/activities", headers=auth("service")).status_code == 403


def test_publish_counts_activities_and_redacts_details(client: TestClient) -> None:
    publish(client, "dag.triggered", token="should-not-persist", dag_id="customer_daily_load")
    response = client.get("/activities", params={"limit": 1}, headers=auth("viewer"))
    details = response.json()[0]["details"]
    assert details["token"] == "***REDACTED***"
    assert details["dag_id"] == "customer_daily_load"

    async def total_activities() -> int:
        database = client.app.state.database  # type: ignore[attr-defined]
        async with database.connection() as conn:
            return int(await conn.fetchval("SELECT total_activities FROM agent_performance"))

    assert client.portal is not None
    assert client.portal.call(total_activities) == 1


# --- S02-AC6: live WebSocket feed -------------------------------------------------------------


@pytest.mark.spec("S02-AC6")
def test_subscriber_receives_new_activity_within_two_seconds(client: TestClient) -> None:
    with client.websocket_connect(
        "/ws/activities", subprotocols=["bearer", ROLE_TOKENS["viewer"]]
    ) as ws:
        assert ws.accepted_subprotocol == "bearer"
        started = time.perf_counter()
        published = publish(client, "incident.created", severity="CRITICAL")
        received = ws.receive_json()
        elapsed = time.perf_counter() - started

    assert elapsed < 2.0
    assert received["id"] == published.id
    assert received["type"] == "incident.created"
    assert received["details"] == {"severity": "CRITICAL"}


@pytest.mark.spec("S02-AC6")
def test_authorization_header_is_accepted(client: TestClient) -> None:
    with client.websocket_connect("/ws/activities", headers=auth("admin")) as ws:
        published = publish(client, "via.header")
        assert ws.receive_json()["id"] == published.id


@pytest.mark.spec("S02-AC6")
def test_disconnected_client_does_not_break_publishing(client: TestClient) -> None:
    with client.websocket_connect("/ws/activities", headers=auth("viewer")):
        pass  # client disconnects immediately
    publish(client, "after.disconnect")  # must not raise

    with client.websocket_connect("/ws/activities", headers=auth("viewer")) as ws:
        published = publish(client, "second.subscriber")
        assert ws.receive_json()["id"] == published.id


@pytest.mark.spec("S02-AC6")
@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, auth("service")],
    ids=["no-token", "bad-token", "service-role"],
)
def test_websocket_rejects_unauthorised(client: TestClient, headers: dict[str, str]) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        client.websocket_connect("/ws/activities", headers=headers),
    ):
        pass
    assert closed.value.code == 1008
