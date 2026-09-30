"""Bearer-token authentication and role checks (S01; OIDC for humans arrives in S23).

Tokens are configured only as SHA-256 hex digests mapped to ``{subject, role}`` so plaintext
tokens never appear in configuration. ``Actor`` is the identity abstraction every mutating
service call receives; it survives the later switch to OIDC.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.api.errors import ApiError
from backend.config import ConfigurationError, Settings


class Role(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    REVIEWER = "reviewer"
    ADMIN = "admin"
    SERVICE = "service"  # machine role (Airflow callbacks, DQ checkpoints); not a human rank


HUMAN_ROLE_RANK: Mapping[Role, int] = {
    Role.VIEWER: 0,
    Role.OPERATOR: 1,
    Role.REVIEWER: 2,
    Role.ADMIN: 3,
}


@dataclass(frozen=True, slots=True)
class Actor:
    subject: str
    role: Role


_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenRegistry:
    """Resolves bearer tokens to actors by comparing SHA-256 digests."""

    def __init__(self, entries: Mapping[str, Actor]) -> None:
        self._entries = dict(entries)

    def __len__(self) -> int:
        return len(self._entries)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> TokenRegistry:
        entries: dict[str, Actor] = {}
        for digest, info in raw.items():
            if not _SHA256_HEX.fullmatch(digest):
                raise ConfigurationError(
                    "API token keys must be lowercase SHA-256 hex digests of the token, "
                    "never the plaintext token"
                )
            if not isinstance(info, Mapping) or not info.get("subject"):
                raise ConfigurationError("each API token entry needs a non-empty 'subject'")
            try:
                role = Role(str(info.get("role")))
            except ValueError as exc:
                raise ConfigurationError(
                    f"invalid role for subject {info['subject']!r}; "
                    f"expected one of {[r.value for r in Role]}"
                ) from exc
            entries[digest] = Actor(subject=str(info["subject"]), role=role)
        return cls(entries)

    @classmethod
    def from_settings(cls, settings: Settings) -> TokenRegistry:
        raw: dict[str, Any] = {}
        if settings.api_tokens_file is not None:
            try:
                text = settings.api_tokens_file.read_text(encoding="utf-8")
            except OSError as exc:
                raise ConfigurationError(
                    f"cannot read API_TOKENS_FILE {settings.api_tokens_file}"
                ) from exc
            raw.update(_load_json(text))
        if settings.api_tokens is not None:
            raw.update(_load_json(settings.api_tokens.get_secret_value()))
        return cls.from_mapping(raw)

    def resolve(self, token: str) -> Actor | None:
        digest = hash_token(token)
        for known, actor in self._entries.items():
            if hmac.compare_digest(known, digest):
                return actor
        return None


def _load_json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigurationError("API token configuration is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ConfigurationError("API token configuration must be a JSON object")
    return data


_bearer = HTTPBearer(auto_error=False)
_UNAUTHENTICATED_HEADERS = {"WWW-Authenticate": "Bearer"}


def _authenticate(request: Request, credentials: HTTPAuthorizationCredentials | None) -> Actor:
    registry: TokenRegistry = request.app.state.token_registry
    if credentials is None or not credentials.credentials:
        raise ApiError(401, "UNAUTHENTICATED", "Missing bearer token.", _UNAUTHENTICATED_HEADERS)
    actor = registry.resolve(credentials.credentials)
    if actor is None:
        raise ApiError(401, "UNAUTHENTICATED", "Invalid bearer token.", _UNAUTHENTICATED_HEADERS)
    return actor


Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]


def require_role(min_role: Role) -> Callable[..., Actor]:
    """Dependency accepting human actors whose role rank is at least ``min_role``."""
    if min_role not in HUMAN_ROLE_RANK:
        raise ValueError("require_role accepts human roles only; use require_service()")

    def dependency(request: Request, credentials: Credentials) -> Actor:
        actor = _authenticate(request, credentials)
        rank = HUMAN_ROLE_RANK.get(actor.role)
        if rank is None or rank < HUMAN_ROLE_RANK[min_role]:
            raise ApiError(403, "FORBIDDEN", f"Requires role '{min_role.value}' or higher.")
        return actor

    return dependency


def require_service() -> Callable[..., Actor]:
    """Dependency accepting only the machine ``service`` role."""

    def dependency(request: Request, credentials: Credentials) -> Actor:
        actor = _authenticate(request, credentials)
        if actor.role is not Role.SERVICE:
            raise ApiError(403, "FORBIDDEN", "Requires the 'service' role.")
        return actor

    return dependency
