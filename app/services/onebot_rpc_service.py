import asyncio
import logging
import uuid
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class OneBotRPCService:
    """OneBot RPC 服务，管理 WebSocket 连接和 RPC 调用。

    使用连接 ID 机制防止同一 robot_id 重复连接时的竞态条件。
    """
    _connections: dict[str, tuple[WebSocket, str]] = {}  # robot_id -> (websocket, connection_id)
    _lock: asyncio.Lock = asyncio.Lock()
    _pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    @classmethod
    async def register_connection(cls, robot_id: str, websocket: WebSocket) -> str:
        """注册 WebSocket 连接，返回连接 ID 用于后续清理。

        如果同一 robot_id 已存在连接，旧连接会被标记为过期。

        Args:
            robot_id: 机器人账号标识
            websocket: WebSocket 连接对象

        Returns:
            connection_id: 连接唯一标识
        """
        connection_id = uuid.uuid4().hex
        old_websocket: WebSocket | None = None
        old_connection_id: str | None = None
        async with cls._lock:
            old = cls._connections.get(robot_id)
            if old is not None:
                old_websocket, old_connection_id = old
                logger.warning(
                    f"Robot {robot_id} already connected (old connection_id={old_connection_id}), "
                    f"new connection {connection_id} will replace it"
                )
            cls._connections[robot_id] = (websocket, connection_id)

        if old_websocket is not None:
            try:
                await old_websocket.close(code=4000)
            except Exception as exc:
                logger.debug(
                    "Failed to close replaced OneBot connection: "
                    "robot_id=%s, old_connection_id=%s, error=%s",
                    robot_id,
                    old_connection_id,
                    exc,
                )

        logger.info(f"Registered OneBot connection: robot_id={robot_id}, connection_id={connection_id}")
        return connection_id

    @classmethod
    async def unregister_connection(cls, robot_id: str, connection_id: str) -> None:
        """注销 WebSocket 连接。

        只有当前连接 ID 匹配时才会注销，防止误删新连接。

        Args:
            robot_id: 机器人账号标识
            connection_id: 连接唯一标识
        """
        async with cls._lock:
            current = cls._connections.get(robot_id)
            if current is not None:
                _, current_id = current
                if current_id == connection_id:
                    cls._connections.pop(robot_id, None)
                    logger.info(f"Unregistered OneBot connection: robot_id={robot_id}, connection_id={connection_id}")
                else:
                    logger.warning(
                        f"Connection ID mismatch for robot {robot_id}: "
                        f"requested={connection_id}, current={current_id}, skipping unregister"
                    )

    @classmethod
    async def is_current_connection(cls, robot_id: str, connection_id: str | None) -> bool:
        if connection_id is None:
            return False
        async with cls._lock:
            current = cls._connections.get(robot_id)
            return current is not None and current[1] == connection_id

    @classmethod
    def resolve_response(cls, event: dict[str, Any]) -> bool:
        """解析 OneBot 响应事件，匹配到对应的 RPC 调用。

        Args:
            event: OneBot 事件字典

        Returns:
            是否成功解析并匹配到挂起的 RPC 调用
        """
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
        connection_id: str | None = None,
    ) -> dict[str, Any]:
        """调用 OneBot API 操作。

        Args:
            robot_id: 机器人账号标识
            action: OneBot 操作名称
            params: 操作参数
            timeout_seconds: 超时时间（秒）

        Returns:
            OneBot 响应字典

        Raises:
            LookupError: WebSocket 未连接
            asyncio.TimeoutError: 调用超时
        """
        async with cls._lock:
            connection = cls._connections.get(robot_id)
            if connection is None:
                raise LookupError(f"OneBot websocket is not connected for robot {robot_id}")
            websocket, active_connection_id = connection
            if connection_id is not None and active_connection_id != connection_id:
                raise LookupError(
                    f"OneBot websocket connection is stale for robot {robot_id}: "
                    f"requested={connection_id}, current={active_connection_id}"
                )

        echo = f"chat-audit:{uuid.uuid4().hex}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        cls._pending[echo] = future

        try:
            await websocket.send_json({"action": action, "params": params, "echo": echo})
            logger.debug(f"Sent RPC call: robot_id={robot_id}, action={action}, echo={echo}")
            result = await asyncio.wait_for(future, timeout=timeout_seconds)
            logger.debug(f"Received RPC response: robot_id={robot_id}, echo={echo}")
            return result
        except asyncio.TimeoutError:
            logger.warning(f"RPC call timeout: robot_id={robot_id}, action={action}, echo={echo}, timeout={timeout_seconds}s")
            raise
        except Exception as exc:
            logger.exception(f"RPC call failed: robot_id={robot_id}, action={action}, echo={echo}, error={exc}")
            raise
        finally:
            cls._pending.pop(echo, None)
