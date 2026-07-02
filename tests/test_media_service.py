import httpx
import pytest

from app.services.media_service import MediaService, parse_cq_media_segments
from app.services.message_service import MessageService


class StubAsyncClient:
    def __init__(self, payloads: dict[str, bytes]):
        self.payloads = payloads
        self.requested_urls: list[str] = []

    async def get(self, url: str):
        self.requested_urls.append(url)
        return httpx.Response(200, content=self.payloads[url])


def test_parse_cq_media_segments_extracts_supported_media():
    raw = (
        "before "
        "[CQ:image,file=abc.image,url=http://media.local/a.jpg] "
        "[CQ:record,file=voice.silk,url=http://media.local/v.silk] "
        "[CQ:at,qq=123]"
    )

    segments = parse_cq_media_segments(raw)

    assert [segment.media_type for segment in segments] == ["image", "voice"]
    assert segments[0].url == "http://media.local/a.jpg"
    assert segments[0].ext == "jpg"
    assert segments[1].url == "http://media.local/v.silk"
    assert segments[1].ext == "silk"


@pytest.mark.asyncio
async def test_rewrite_cq_media_downloads_assets_and_rewrites_local_paths(db_session, tmp_path):
    client = StubAsyncClient(
        {
            "http://media.local/a.jpg": b"same image bytes",
            "http://media.local/duplicate.jpg": b"same image bytes",
        }
    )
    raw = (
        "[CQ:image,file=a.jpg,url=http://media.local/a.jpg]"
        " and "
        "[CQ:image,file=b.jpg,url=http://media.local/duplicate.jpg]"
    )

    rewritten = await MediaService.rewrite_cq_media_to_local_paths(
        db_session,
        raw_message=raw,
        http_client=client,
        storage_root=tmp_path,
        public_prefix="/static/storage",
    )

    assets = await MessageService.list_media_assets(db_session)

    assert len(client.requested_urls) == 2
    assert len(assets) == 1
    assert rewritten.count("/static/storage/") == 2
    assert "http://media.local" not in rewritten
    assert assets[0].local_path in rewritten


@pytest.mark.asyncio
async def test_process_incoming_message_rewrites_cq_media_when_http_client_is_supplied(db_session, tmp_path):
    client = StubAsyncClient({"http://media.local/a.jpg": b"image bytes"})
    payload = {
        "room_id": "group-media",
        "message_type": "group",
        "sender_id": "user-media",
        "nickname": "Media User",
        "raw_message": "[CQ:image,file=a.jpg,url=http://media.local/a.jpg]",
        "timestamp": 1783000300,
    }

    await MessageService.process_incoming_message(
        db_session,
        robot_id="robot-media",
        platform="qq",
        msg_data=payload,
        media_http_client=client,
        media_storage_root=tmp_path,
        media_public_prefix="/static/storage",
    )

    messages = await MessageService.list_messages(db_session)
    assets = await MessageService.list_media_assets(db_session)

    assert len(messages) == 1
    assert len(assets) == 1
    assert messages[0].raw_message == payload["raw_message"]
    assert messages[0].local_message == assets[0].local_path
