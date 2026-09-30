from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from backend.config import ConfigurationError
from backend.security.auth import (
    Actor,
    Role,
    TokenRegistry,
    hash_token,
    require_role,
    require_service,
)
from tests.conftest import ROLE_TOKENS, auth

pytestmark = pytest.mark.spec("S01-AC6")


@pytest.fixture
def auth_client(app: FastAPI) -> Iterator[TestClient]:
    @app.get("/_test/operator")
    async def operator_route(
        actor: Annotated[Actor, Depends(require_role(Role.OPERATOR))],
    ) -> dict[str, str]:
        return {"subject": actor.subject, "role": actor.role.value}

    @app.post("/_test/ingest")
    async def service_route(actor: Annotated[Actor, Depends(require_service())]) -> dict[str, str]:
        return {"subject": actor.subject}

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_missing_token_is_401_with_challenge(auth_client: TestClient) -> None:
    response = auth_client.get("/_test/operator")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["error_code"] == "UNAUTHENTICATED"


def test_invalid_token_is_401(auth_client: TestClient) -> None:
    response = auth_client.get("/_test/operator", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("role", "expected"),
    [("viewer", 403), ("operator", 200), ("reviewer", 200), ("admin", 200), ("service", 403)],
)
def test_role_ordering(auth_client: TestClient, role: str, expected: int) -> None:
    response = auth_client.get("/_test/operator", headers=auth(role))
    assert response.status_code == expected
    if expected == 403:
        assert response.json()["error_code"] == "FORBIDDEN"
    else:
        assert response.json() == {"subject": f"{role}@test", "role": role}


@pytest.mark.parametrize(
    ("role", "expected"),
    [("service", 200), ("viewer", 403), ("admin", 403)],
)
def test_service_route_accepts_only_service(
    auth_client: TestClient, role: str, expected: int
) -> None:
    assert auth_client.post("/_test/ingest", headers=auth(role)).status_code == expected


def test_readiness_requires_viewer(client: TestClient) -> None:
    assert client.get("/health/ready").status_code == 401
    assert client.get("/health/ready", headers=auth("viewer")).status_code == 200


def test_registry_stores_only_digests() -> None:
    registry = TokenRegistry.from_mapping({hash_token("plain"): {"subject": "a", "role": "admin"}})
    assert registry.resolve("plain") == Actor("a", Role.ADMIN)
    assert registry.resolve(hash_token("plain")) is None  # the digest itself is not a token


@pytest.mark.parametrize(
    "raw",
    [
        {ROLE_TOKENS["admin"]: {"subject": "a", "role": "admin"}},  # plaintext key
        {hash_token("x"): {"subject": "a", "role": "superuser"}},  # unknown role
        {hash_token("x"): {"role": "admin"}},  # missing subject
    ],
)
def test_registry_rejects_bad_entries(raw: dict[str, object]) -> None:
    with pytest.raises(ConfigurationError):
        TokenRegistry.from_mapping(raw)


def test_require_role_rejects_service_as_rank() -> None:
    with pytest.raises(ValueError, match="human roles"):
        require_role(Role.SERVICE)
