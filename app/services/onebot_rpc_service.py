import asyncio
import uuid
from typing import Any

from fastapi import WebSocket


class OneBotRPCService:
    _connections: dict[str, WebSocket] = {}
    _pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    @classmethod
    def register_connection(cls, robot_id: str, websocket: WebSocket) -> None:
        cls._connections[robot_id] = websocket

    @classmethod
    def unregister_connection(cls, websocket: WebSocket) -> None:
        stale_robot_ids = [robot_id for robot_id, active in cls._connections.items() if active is websocket]
        for robot_id in stale_robot_ids:
            cls._connections.pop(robot_id, None)

    @classmethod
    def resolve_response(cls, event: dict[str, Any]) -> bool:
        echo = event.get("echo")
        if echo is None:
            return False
        future = cls._pending.get(str(echo))
        if future is None:
            return False
        if not future.done():
            future.set_result(event)
        return True

    @classmethod
    async def call_action(
        cls,
        robot_id: str,
        action: str,
        params: dict[str, Any],
        timeout_seconds: float = 10,
    ) -> dict[str, Any]:
        websocket = cls._connections.get(robot_id)
        if websocket is None:
            raise LookupError("onebot websocket is not connected for robot")

        echo = f"chat-audit:{uuid.uuid4().hex}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        cls._pending[echo] = future
        try:
            await websocket.send_json({"action": action, "params": params, "echo": echo})
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        finally:
            cls._pending.pop(echo, None)
