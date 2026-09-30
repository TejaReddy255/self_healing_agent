"""Activity stream API (S02): paginated history and a live WebSocket feed."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, WebSocket, WebSocketDisconnect

from backend.api.errors import ApiError
from backend.domain.audit import Activity
from backend.security.auth import HUMAN_ROLE_RANK, Actor, Role, TokenRegistry, require_role
from backend.services.activity_service import ActivityBroadcaster, ActivityService

logger = logging.getLogger(__name__)

router = APIRouter()

WS_POLICY_VIOLATION = 1008
WS_TRY_AGAIN_LATER = 1013
# A subscriber that cannot take a message within this time is disconnected.
SEND_TIMEOUT_S = 10.0
BEARER_SUBPROTOCOL = "bearer"


def get_activity_service(request: Request) -> ActivityService:
    service: ActivityService | None = request.app.state.activity_service
    if service is None:
        raise ApiError(503, "DATABASE_UNAVAILABLE", "Persistence is not configured.")
    return service


@router.get("/activities", response_model=list[Activity])
async def list_activities(
    service: Annotated[ActivityService, Depends(get_activity_service)],
    _: Annotated[Actor, Depends(require_role(Role.VIEWER))],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    before_id: Annotated[int | None, Query(ge=1)] = None,
) -> list[Activity]:
    return await service.list_recent(limit, before_id)


def _websocket_token(websocket: WebSocket) -> tuple[str | None, bool]:
    """Token from ``Authorization: Bearer`` or from subprotocols ``["bearer", "<token>"]``.

    Query-string tokens are deliberately not supported: they end up in proxy/access logs.
    Returns ``(token, via_subprotocol)``.
    """
    header = websocket.headers.get("authorization", "")
    scheme, _, credentials = header.partition(" ")
    if scheme.lower() == "bearer" and credentials.strip():
        return credentials.strip(), False
    protocols = [p.strip() for p in websocket.headers.get("sec-websocket-protocol", "").split(",")]
    if len(protocols) >= 2 and protocols[0].lower() == BEARER_SUBPROTOCOL and protocols[1]:
        return protocols[1], True
    return None, False


def _authorize_websocket(websocket: WebSocket) -> tuple[Actor | None, bool]:
    token, via_subprotocol = _websocket_token(websocket)
    if token is None:
        return None, via_subprotocol
    registry: TokenRegistry = websocket.app.state.token_registry
    actor = registry.resolve(token)
    if actor is None or HUMAN_ROLE_RANK.get(actor.role) is None:
        return None, via_subprotocol
    return actor, via_subprotocol


async def _wait_for_disconnect(websocket: WebSocket) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


@router.websocket("/ws/activities")
async def activities_feed(websocket: WebSocket) -> None:
    """Push activities published after the connection is established (viewer or higher)."""
    actor, via_subprotocol = _authorize_websocket(websocket)
    if actor is None:
        await websocket.close(code=WS_POLICY_VIOLATION)
        return
    await websocket.accept(subprotocol=BEARER_SUBPROTOCOL if via_subprotocol else None)
    broadcaster: ActivityBroadcaster = websocket.app.state.broadcaster

    with broadcaster.subscribe() as queue:
        disconnected = asyncio.create_task(_wait_for_disconnect(websocket))
        next_item: asyncio.Task[Activity] | None = None
        try:
            while True:
                next_item = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {next_item, disconnected}, return_when=asyncio.FIRST_COMPLETED
                )
                if disconnected in done:
                    return
                payload = next_item.result().model_dump(mode="json")
                try:
                    await asyncio.wait_for(websocket.send_json(payload), timeout=SEND_TIMEOUT_S)
                except TimeoutError:
                    logger.warning(
                        "activity subscriber stalled; closing", extra={"subject": actor.subject}
                    )
                    with contextlib.suppress(WebSocketDisconnect, OSError, RuntimeError):
                        await websocket.close(code=WS_TRY_AGAIN_LATER)
                    return
                except RuntimeError:
                    # Starlette raises RuntimeError when sending after the socket closed.
                    return
        except (WebSocketDisconnect, OSError):
            logger.info("activity subscriber disconnected", extra={"subject": actor.subject})
        finally:
            # Cancel both helper tasks even when this handler itself is cancelled.
            for task in (next_item, disconnected):
                if task is not None and not task.done():
                    task.cancel()
            with contextlib.suppress(
                asyncio.CancelledError, WebSocketDisconnect, OSError, RuntimeError
            ):
                await disconnected
