import httpx
import pytest

from app.services.capture_policy_service import CapturePolicyService
from app.services.message_service import MessageService


class StubHttpClient:
    def __init__(self):
        self.urls = []

    async def get(self, url):
        self.urls.append(url)
        return httpx.Response(200, content=b"file-bytes", headers={"content-type": "application/octet-stream"})


def _payload(**overrides):
    payload = {
        "message_id": "msg-1",
        "room_id": "955973452",
        "message_type": "group",
        "sender_id": "user-1",
        "nickname": "Alice",
        "raw_message": "hello",
        "timestamp": 1783000000,
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_empty_capture_policy_records_all_messages_but_does_not_download_files(db_session, tmp_path):
    client = StubHttpClient()

    msg_hash = await MessageService.process_incoming_message(
        db_session,
        robot_id="robot-a",
        platform="qq",
        msg_data=_payload(raw_message="[CQ:file,file=doc.pdf,url=http://media.local/doc.pdf]"),
        media_http_client=client,
        media_storage_root=tmp_path,
        media_public_prefix="/static/storage",
    )

    messages = await MessageService.list_messages(db_session)
    assets = await MessageService.list_media_assets(db_session)

    assert msg_hash is not None
    assert len(messages) == 1
    assert messages[0].local_message == "[CQ:file,file=doc.pdf,url=http://media.local/doc.pdf]"
    assert assets == []
    assert client.urls == []


@pytest.mark.asyncio
async def test_blacklisted_target_is_not_stored(db_session):
    await CapturePolicyService.upsert_policy(
        db_session,
        robot_id="robot-a",
        target_type="group",
        target_id="955973452",
        list_mode="blacklist",
    )

    msg_hash = await MessageService.process_incoming_message(
        db_session,
        robot_id="robot-a",
        platform="qq",
        msg_data=_payload(),
    )

    assert msg_hash is None
    assert await MessageService.list_messages(db_session) == []
    assert await MessageService.list_robot_messages(db_session) == []


@pytest.mark.asyncio
async def test_whitelist_only_records_listed_targets(db_session):
    await CapturePolicyService.upsert_policy(
        db_session,
        robot_id="robot-a",
        target_type="group",
        target_id="955973452",
        list_mode="whitelist",
    )

    skipped = await MessageService.process_incoming_message(
        db_session,
        robot_id="robot-a",
        platform="qq",
        msg_data=_payload(message_id="msg-skipped", room_id="111111"),
    )
    allowed = await MessageService.process_incoming_message(
        db_session,
        robot_id="robot-a",
        platform="qq",
        msg_data=_payload(message_id="msg-allowed", room_id="955973452"),
    )

    messages = await MessageService.list_messages(db_session)

    assert skipped is None
    assert allowed is not None
    assert [message.room_id for message in messages] == ["955973452"]


@pytest.mark.asyncio
async def test_explicit_target_policy_can_disable_images(db_session):
    await CapturePolicyService.upsert_policy(
        db_session,
        robot_id="robot-a",
        target_type="group",
        target_id="955973452",
        list_mode="none",
        capture_text=True,
        capture_image=False,
        capture_voice=True,
        capture_video=True,
        capture_file=False,
    )

    msg_hash = await MessageService.process_incoming_message(
        db_session,
        robot_id="robot-a",
        platform="qq",
        msg_data=_payload(raw_message="[CQ:image,file=a.jpg,url=http://media.local/a.jpg]"),
    )

    assert msg_hash is None
    assert await MessageService.list_messages(db_session) == []


@pytest.mark.asyncio
async def test_file_capture_flag_does_not_disable_images_stickers_or_voice(db_session, tmp_path):
    client = StubHttpClient()
    await CapturePolicyService.upsert_policy(
        db_session,
        robot_id="robot-a",
        target_type="group",
        target_id="955973452",
        list_mode="none",
        capture_text=True,
        capture_image=True,
        capture_voice=True,
        capture_video=True,
        capture_file=False,
    )

    sticker_hash = await MessageService.process_incoming_message(
        db_session,
        robot_id="robot-a",
        platform="qq",
        msg_data=_payload(
            message_id="sticker-1",
            raw_message="[CQ:image,summary=&#91;动画表情&#93;,file=sticker.jpg,sub_type=1,url=http://media.local/sticker.jpg]",
        ),
        media_http_client=client,
        media_storage_root=tmp_path,
        media_public_prefix="/static/storage",
    )
    voice_hash = await MessageService.process_incoming_message(
        db_session,
        robot_id="robot-a",
        platform="qq",
        msg_data=_payload(
            message_id="voice-1",
            raw_message="[CQ:record,file=voice.silk,url=http://media.local/voice.silk]",
        ),
        media_http_client=client,
        media_storage_root=tmp_path,
        media_public_prefix="/static/storage",
    )

    messages = await MessageService.list_messages(db_session)

    assert sticker_hash is not None
    assert voice_hash is not None
    assert len(messages) == 2
    assert client.urls == ["http://media.local/sticker.jpg", "http://media.local/voice.silk"]


@pytest.mark.asyncio
async def test_capture_targets_include_discovered_rooms_and_policy(db_session):
    await MessageService.process_incoming_message(
        db_session,
        robot_id="robot-a",
        platform="qq",
        msg_data=_payload(),
    )
    await CapturePolicyService.upsert_policy(
        db_session,
        robot_id="robot-a",
        target_type="group",
        target_id="955973452",
        list_mode="whitelist",
        capture_file=True,
    )

    targets = await CapturePolicyService.list_target_settings(db_session, robot_id="robot-a")

    assert len(targets) == 1
    assert targets[0]["target_id"] == "955973452"
    assert targets[0]["policy"]["list_mode"] == "whitelist"
    assert targets[0]["policy"]["capture_file"] is True


@pytest.mark.asyncio
async def test_capture_targets_use_private_sender_name_when_profile_missing(db_session):
    await MessageService.process_incoming_message(
        db_session,
        robot_id="robot-a",
        platform="qq",
        msg_data={
            "room_id": "123456789",
            "message_type": "private",
            "sender_id": "123456789",
            "nickname": "Private Friend",
            "raw_message": "hello private",
            "timestamp": 1783000000,
        },
    )

    targets = await CapturePolicyService.list_target_settings(db_session, robot_id="robot-a")

    assert len(targets) == 1
    assert targets[0]["target_type"] == "private"
    assert targets[0]["target_id"] == "123456789"
    assert targets[0]["display_name"] == "Private Friend"
