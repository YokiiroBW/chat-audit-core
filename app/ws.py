from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.onebot11 import normalize_message_event
from app.config import get_settings
from app.database import get_db_session
from app.models import Adapter
from app.services.message_service import MessageService

router = APIRouter()


async def get_media_http_client() -> AsyncIterator[Any]:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=settings.media_download_timeout_seconds) as client:
        yield client


def get_onebot_access_token() -> str:
    return get_settings().onebot_access_token


def _extract_bearer_token(authorization):
    if authorization is None or authorization == "":
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def _is_authorized(websocket: WebSocket, configured_token: str) -> bool:
    if not configured_token:
        return True

    query_token = websocket.query_params.get("access_token")
    bearer_token = _extract_bearer_token(websocket.headers.get("authorization"))
    return query_token == configured_token or bearer_token == configured_token


async def _is_registered_adapter(db: AsyncSession, robot_id: str) -> bool:
    result = await db.execute(select(Adapter.id).where(Adapter.id == robot_id))
    return result.scalar_one_or_none() is not None


@router.websocket("/onebot/v11/ws")
async def onebot11_reverse_ws(
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db_session),
    media_http_client: Any = Depends(get_media_http_client),
    configured_token: str = Depends(get_onebot_access_token),
) -> None:
    if not _is_authorized(websocket, configured_token):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    try:
        while True:
            event = await websocket.receive_json()
            normalized = normalize_message_event(event)
            if normalized is None:
                continue
            if not await _is_registered_adapter(db, normalized.robot_id):
                continue

            await MessageService.process_incoming_message(
                db,
                robot_id=normalized.robot_id,
                platform=normalized.platform,
                msg_data=normalized.msg_data,
                media_http_client=media_http_client,
            )
    except WebSocketDisconnect:
        return
