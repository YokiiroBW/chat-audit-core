import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.onebot11 import normalize_message_event
from app.config import get_settings
from app.database import AsyncSessionLocal, get_db_session
from app.models import Message, RoomProfile, UserProfile
from app.services.bot_profile_service import BotProfileService
from app.services.capture_policy_service import CapturePolicyService
from app.services.media_service import MediaService
from app.services.message_service import MessageService
from app.services.onebot_rpc_service import OneBotRPCService
from app.services.room_profile_service import RoomProfileService
from app.services.user_profile_service import UserProfileService

router = APIRouter()
logger = logging.getLogger(__name__)


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


def _extract_robot_id(event: dict[str, Any]) -> str | None:
    self_id = event.get("self_id")
    if self_id is None:
        return None
    return str(self_id)


async def _hydrate_forward_payloads(robot_id: str, connection_id: str, msg_hash: str) -> None:
    """后台任务：拉取合并转发详情并缓存。"""
    settings = get_settings()
    try:
        if not await OneBotRPCService.is_current_connection(robot_id, connection_id):
            logger.info("Skip forward hydration for stale OneBot connection: robot_id=%s, msg_hash=%s", robot_id, msg_hash)
            return
        async with AsyncSessionLocal() as session, httpx.AsyncClient(timeout=settings.media_download_timeout_seconds) as client:
            message = await session.get(Message, msg_hash)
            if message is None:
                return
            local_message = message.local_message
            decision = await CapturePolicyService.should_capture(
                session,
                robot_id=robot_id,
                msg_data={
                    "room_id": message.room_id,
                    "message_type": message.message_type,
                    "raw_message": message.raw_message,
                },
            )

            async def load_forward(forward_id: str) -> dict[str, Any]:
                return await OneBotRPCService.call_action(robot_id, "get_forward_msg", {"id": forward_id}, connection_id=connection_id)

            updated = await MediaService.cache_cq_forward_payloads(
                session,
                local_message=local_message,
                forward_loader=load_forward,
                http_client=client,
                storage_root=settings.storage_root,
                public_prefix=settings.public_storage_prefix,
                max_bytes=settings.media_max_bytes,
                allowed_media_types=decision.allowed_media_types,
                forward_depth=settings.forward_cache_max_depth,
            )
            if updated == local_message:
                return
            message.local_message = updated
            await session.commit()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception(f"Failed to hydrate forward payloads: robot_id={robot_id}, msg_hash={msg_hash}, error={exc}")


async def _hydrate_group_profile(robot_id: str, connection_id: str, platform: str, room_id: str) -> None:
    """后台任务：拉取群组资料并缓存头像。"""
    settings = get_settings()
    try:
        if not await OneBotRPCService.is_current_connection(robot_id, connection_id):
            logger.info("Skip group profile hydration for stale OneBot connection: robot_id=%s, room_id=%s", robot_id, room_id)
            return
        async with AsyncSessionLocal() as session:
            existing = await session.get(RoomProfile, room_id)
            if existing is not None and existing.display_name and existing.avatar_path:
                return

        group_info = None
        try:
            payload = await OneBotRPCService.call_action(
                robot_id,
                "get_group_info",
                {"group_id": int(room_id), "no_cache": False},
                connection_id=connection_id,
            )
            if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                group_info = payload["data"]
        except Exception as exc:
            logger.warning(
                "Failed to load QQ group info: robot_id=%s, connection_id=%s, room_id=%s, error=%s",
                robot_id,
                connection_id,
                room_id,
                exc,
            )
            group_info = None

        async with AsyncSessionLocal() as session, httpx.AsyncClient(timeout=settings.media_download_timeout_seconds) as client:
            await RoomProfileService.cache_qq_group_profile(
                session,
                room_id=room_id,
                platform=platform,
                group_info=group_info,
                http_client=client,
                storage_root=settings.storage_root,
                public_prefix=settings.public_storage_prefix,
                max_bytes=settings.media_max_bytes,
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception(f"Failed to hydrate group profile: robot_id={robot_id}, platform={platform}, room_id={room_id}, error={exc}")


async def _hydrate_user_profile(robot_id: str, connection_id: str, platform: str, user_id: str, display_name: str | None = None) -> None:
    """后台任务：拉取用户资料并缓存头像。"""
    settings = get_settings()
    try:
        if not await OneBotRPCService.is_current_connection(robot_id, connection_id):
            logger.info("Skip user profile hydration for stale OneBot connection: robot_id=%s, user_id=%s", robot_id, user_id)
            return
        async with AsyncSessionLocal() as session:
            existing = await session.get(UserProfile, user_id)
            if existing is not None and existing.avatar_path and (existing.display_name or not display_name):
                return

        async with AsyncSessionLocal() as session, httpx.AsyncClient(timeout=settings.media_download_timeout_seconds) as client:
            await UserProfileService.cache_qq_user_profile(
                session,
                user_id=user_id,
                platform=platform,
                display_name=display_name,
                http_client=client,
                storage_root=settings.storage_root,
                public_prefix=settings.public_storage_prefix,
                max_bytes=settings.media_max_bytes,
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception(f"Failed to hydrate user profile: platform={platform}, user_id={user_id}, error={exc}")


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
    adapter_id = websocket.query_params.get("adapter_id") or websocket.headers.get("x-adapter-id")
    background_tasks: set[asyncio.Task] = set()
    connection_id: str | None = None
    current_robot_id: str | None = None

    def track_background_task(coro) -> None:
        task = asyncio.create_task(coro)
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    try:
        while True:
            event = await websocket.receive_json()

            robot_id = _extract_robot_id(event)
            if robot_id:
                if robot_id != current_robot_id:
                    connection_id = await OneBotRPCService.register_connection(robot_id, websocket)
                    current_robot_id = robot_id
                    logger.info(f"OneBot WebSocket connected: adapter_id={adapter_id}, robot_id={robot_id}, connection_id={connection_id}")
                elif not await OneBotRPCService.is_current_connection(robot_id, connection_id):
                    logger.info(
                        "Stop stale OneBot WebSocket: adapter_id=%s, robot_id=%s, connection_id=%s",
                        adapter_id,
                        robot_id,
                        connection_id,
                    )
                    await websocket.close(code=4000)
                    return

            if OneBotRPCService.resolve_response(event):
                continue

            normalized = normalize_message_event(event)
            if normalized is None:
                continue

            display_name = None
            if normalized.msg_data.get("sender_id") == normalized.robot_id:
                display_name = normalized.msg_data.get("nickname")
            await BotProfileService.upsert_bot_profile(
                db,
                robot_id=normalized.robot_id,
                platform=normalized.platform,
                display_name=display_name,
                adapter_id=adapter_id,
            )

            msg_hash = await MessageService.process_incoming_message(
                db,
                robot_id=normalized.robot_id,
                platform=normalized.platform,
                msg_data=normalized.msg_data,
                media_http_client=media_http_client,
            )
            if connection_id:
                if msg_hash and "[CQ:forward," in normalized.msg_data.get("raw_message", ""):
                    track_background_task(_hydrate_forward_payloads(normalized.robot_id, connection_id, msg_hash))
                track_background_task(
                    _hydrate_user_profile(
                        normalized.robot_id,
                        connection_id,
                        normalized.platform,
                        normalized.msg_data["sender_id"],
                        normalized.msg_data.get("nickname"),
                    )
                )
                if normalized.msg_data.get("message_type") == "group":
                    track_background_task(_hydrate_group_profile(normalized.robot_id, connection_id, normalized.platform, normalized.msg_data["room_id"]))
    except WebSocketDisconnect:
        logger.info(f"OneBot WebSocket disconnected: adapter_id={adapter_id}, robot_id={current_robot_id}")
    except Exception as exc:
        logger.exception(f"OneBot WebSocket error: adapter_id={adapter_id}, robot_id={current_robot_id}, error={exc}")
    finally:
        for task in list(background_tasks):
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        if current_robot_id and connection_id:
            await OneBotRPCService.unregister_connection(current_robot_id, connection_id)
