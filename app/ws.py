from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.onebot11 import normalize_message_event
from app.config import get_settings
from app.database import get_db_session
from app.services.message_service import MessageService

router = APIRouter()


async def get_media_http_client() -> AsyncIterator[Any]:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=settings.media_download_timeout_seconds) as client:
        yield client


@router.websocket("/onebot/v11/ws")
async def onebot11_reverse_ws(
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db_session),
    media_http_client: Any = Depends(get_media_http_client),
) -> None:
    await websocket.accept()
    try:
        while True:
            event = await websocket.receive_json()
            normalized = normalize_message_event(event)
            if normalized is None:
                await websocket.send_json({"status": "ignored"})
                continue

            msg_hash = await MessageService.process_incoming_message(
                db,
                robot_id=normalized.robot_id,
                platform=normalized.platform,
                msg_data=normalized.msg_data,
                media_http_client=media_http_client,
            )
            await websocket.send_json({"status": "stored", "msg_hash": msg_hash})
    except WebSocketDisconnect:
        return
