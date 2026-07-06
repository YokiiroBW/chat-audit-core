import asyncio

import pytest

from app.services.onebot_rpc_service import OneBotRPCService


class FakeWebSocket:
    def __init__(self):
        self.sent_json = []
        self.closed_codes = []

    async def send_json(self, payload):
        self.sent_json.append(payload)

    async def close(self, code=None):
        self.closed_codes.append(code)


@pytest.mark.asyncio
async def test_onebot_rpc_call_action_resolves_echo_response():
    OneBotRPCService._connections.clear()
    OneBotRPCService._pending.clear()
    websocket = FakeWebSocket()
    connection_id = await OneBotRPCService.register_connection("robot-a", websocket)

    task = asyncio.create_task(OneBotRPCService.call_action("robot-a", "get_forward_msg", {"id": "forward-id"}, connection_id=connection_id))
    await asyncio.sleep(0)
    sent = websocket.sent_json[0]
    OneBotRPCService.resolve_response({"status": "ok", "data": {"messages": []}, "echo": sent["echo"]})

    response = await task

    assert sent["action"] == "get_forward_msg"
    assert sent["params"] == {"id": "forward-id"}
    assert response["status"] == "ok"


@pytest.mark.asyncio
async def test_onebot_rpc_call_action_requires_connected_robot():
    OneBotRPCService._connections.clear()
    OneBotRPCService._pending.clear()

    with pytest.raises(LookupError):
        await OneBotRPCService.call_action("missing", "get_forward_msg", {"id": "forward-id"})


@pytest.mark.asyncio
async def test_onebot_rpc_replaced_connection_cannot_unregister_new_connection():
    OneBotRPCService._connections.clear()
    OneBotRPCService._pending.clear()
    old_websocket = FakeWebSocket()
    new_websocket = FakeWebSocket()

    old_connection_id = await OneBotRPCService.register_connection("robot-a", old_websocket)
    new_connection_id = await OneBotRPCService.register_connection("robot-a", new_websocket)

    assert old_websocket.closed_codes == [4000]
    assert await OneBotRPCService.is_current_connection("robot-a", old_connection_id) is False
    assert await OneBotRPCService.is_current_connection("robot-a", new_connection_id) is True

    await OneBotRPCService.unregister_connection("robot-a", old_connection_id)

    assert await OneBotRPCService.is_current_connection("robot-a", new_connection_id) is True

    with pytest.raises(LookupError):
        await OneBotRPCService.call_action("robot-a", "get_group_info", {"group_id": 1}, connection_id=old_connection_id)
