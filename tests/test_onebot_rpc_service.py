import asyncio

import pytest

from app.services.onebot_rpc_service import OneBotRPCService


class FakeWebSocket:
    def __init__(self):
        self.sent_json = []

    async def send_json(self, payload):
        self.sent_json.append(payload)


@pytest.mark.asyncio
async def test_onebot_rpc_call_action_resolves_echo_response():
    OneBotRPCService._connections.clear()
    OneBotRPCService._pending.clear()
    websocket = FakeWebSocket()
    OneBotRPCService.register_connection("robot-a", websocket)

    task = asyncio.create_task(OneBotRPCService.call_action("robot-a", "get_forward_msg", {"id": "forward-id"}))
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
