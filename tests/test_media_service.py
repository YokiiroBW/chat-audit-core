import html
import json

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
        "[CQ:file,file=doc.pdf,url=http://media.local/doc.pdf] "
        "[CQ:at,qq=123]"
    )

    segments = parse_cq_media_segments(raw)

    assert [segment.media_type for segment in segments] == ["image", "voice", "file"]
    assert segments[0].url == "http://media.local/a.jpg"
    assert segments[0].ext == "jpg"
    assert segments[1].url == "http://media.local/v.silk"
    assert segments[1].ext == "silk"
    assert segments[2].url == "http://media.local/doc.pdf"
    assert segments[2].ext == "pdf"


def test_parse_cq_media_segments_decodes_html_escaped_ntqq_url():
    raw = (
        "[CQ:image,file=A963374C89F794FAD63222CFB5CB2EE5.png,"
        "url=https://multimedia.nt.qq.com.cn/download?appid=1407&amp;fileid=abc&amp;rkey=def,"
        "file_size=3231236]"
    )

    segments = parse_cq_media_segments(raw)

    assert len(segments) == 1
    assert segments[0].url == "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=abc&rkey=def"
    assert segments[0].ext == "png"


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
async def test_rewrite_cq_media_separates_adjacent_local_paths(db_session, tmp_path):
    client = StubAsyncClient(
        {
            "http://media.local/a.jpg": b"first image",
            "http://media.local/b.jpg": b"second image",
        }
    )
    raw = "[CQ:image,file=a.jpg,url=http://media.local/a.jpg][CQ:image,file=b.jpg,url=http://media.local/b.jpg]"

    rewritten = await MediaService.rewrite_cq_media_to_local_paths(
        db_session,
        raw_message=raw,
        http_client=client,
        storage_root=tmp_path,
        public_prefix="/static/storage",
    )

    assert rewritten.count("/static/storage/") == 2
    assert "\n" in rewritten


@pytest.mark.asyncio
async def test_rewrite_cq_media_skips_assets_over_size_limit(db_session, tmp_path):
    client = StubAsyncClient({"http://media.local/large.jpg": b"too large"})
    raw = "[CQ:image,file=large.jpg,url=http://media.local/large.jpg]"

    rewritten = await MediaService.rewrite_cq_media_to_local_paths(
        db_session,
        raw_message=raw,
        http_client=client,
        storage_root=tmp_path,
        public_prefix="/static/storage",
        max_bytes=4,
    )

    assets = await MessageService.list_media_assets(db_session)

    assert rewritten == raw
    assert assets == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_rewrite_cq_json_card_downloads_preview_asset(db_session, tmp_path):
    client = StubAsyncClient({"http://media.local/preview.jpg": b"preview bytes"})
    card = {"meta": {"detail_1": {"title": "Card", "preview": "http://media.local/preview.jpg", "url": "https://example.com/page"}}}
    raw = f"[CQ:json,data={json.dumps(card, ensure_ascii=False).replace(',', '&#44;')}]"

    rewritten = await MediaService.rewrite_cq_media_to_local_paths(
        db_session,
        raw_message=raw,
        http_client=client,
        storage_root=tmp_path,
        public_prefix="/static/storage",
    )

    assert "http://media.local/preview.jpg" not in rewritten
    assert "https://example.com/page" in rewritten
    assert "/static/storage/" in rewritten


@pytest.mark.asyncio
async def test_rewrite_cq_json_card_caches_page_snapshot(db_session, tmp_path):
    client = StubAsyncClient(
        {
            "http://media.local/preview.jpg": b"preview bytes",
            "https://example.com/page": b"<html><title>snapshot</title></html>",
        }
    )
    card = {"meta": {"detail_1": {"title": "Card", "preview": "http://media.local/preview.jpg", "url": "https://example.com/page"}}}
    raw = f"[CQ:json,data={json.dumps(card, ensure_ascii=False).replace(',', '&#44;')}]"

    rewritten = await MediaService.rewrite_cq_media_to_local_paths(
        db_session,
        raw_message=raw,
        http_client=client,
        storage_root=tmp_path,
        public_prefix="/static/storage",
    )
    data = parse_cq_media_segments(rewritten)
    assets = await MessageService.list_media_assets(db_session)
    payload = json.loads(html.unescape(rewritten.removeprefix("[CQ:json,data=").removesuffix("]")))
    detail = payload["meta"]["detail_1"]

    assert data == []
    assert detail["url"] == "https://example.com/page"
    assert detail["preview"].startswith("/static/storage/")
    assert detail["local_page"].startswith("/static/storage/")
    assert {asset.file_type for asset in assets} == {"image", "card_page"}


@pytest.mark.asyncio
async def test_rewrite_cq_json_card_keeps_url_when_snapshot_fails(db_session, tmp_path):
    client = StubAsyncClient({"http://media.local/preview.jpg": b"preview bytes"})
    card = {"meta": {"detail_1": {"title": "Card", "preview": "http://media.local/preview.jpg", "url": "https://example.com/missing"}}}
    raw = f"[CQ:json,data={json.dumps(card, ensure_ascii=False).replace(',', '&#44;')}]"

    rewritten = await MediaService.rewrite_cq_media_to_local_paths(
        db_session,
        raw_message=raw,
        http_client=client,
        storage_root=tmp_path,
        public_prefix="/static/storage",
    )
    payload = json.loads(html.unescape(rewritten.removeprefix("[CQ:json,data=").removesuffix("]")))
    detail = payload["meta"]["detail_1"]

    assert detail["url"] == "https://example.com/missing"
    assert "local_page" not in detail


@pytest.mark.asyncio
async def test_localize_onebot_forward_payload_downloads_nested_media(db_session, tmp_path):
    client = StubAsyncClient(
        {
            "http://media.local/forward.jpg": b"forward image",
            "http://media.local/forward.mp4": b"forward video",
        }
    )
    payload = {
        "status": "ok",
        "data": {
            "messages": [
                {
                    "raw_message": "[CQ:image,file=f.jpg,url=http://media.local/forward.jpg]",
                    "message": [
                        {"type": "video", "data": {"file": "v.mp4", "url": "http://media.local/forward.mp4"}},
                    ],
                }
            ]
        },
    }

    localized = await MediaService.localize_onebot_payload(
        db_session,
        payload,
        http_client=client,
        storage_root=tmp_path,
        public_prefix="/static/storage",
    )

    message = localized["data"]["messages"][0]
    assert "http://media.local" not in message["raw_message"]
    assert message["message"][0]["data"]["url"].startswith("/static/storage/")


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


@pytest.mark.asyncio
async def test_process_incoming_message_caches_forward_payload_when_loader_is_supplied(db_session, tmp_path):
    client = StubAsyncClient({"http://media.local/in-forward.jpg": b"inside forward"})

    async def load_forward(forward_id):
        assert forward_id == "forward-1"
        return {
            "status": "ok",
            "data": {
                "messages": [
                    {
                        "raw_message": "[CQ:image,file=f.jpg,url=http://media.local/in-forward.jpg]",
                    }
                ]
            },
        }

    await MessageService.process_incoming_message(
        db_session,
        robot_id="robot-forward",
        platform="qq",
        msg_data={
            "room_id": "group-forward",
            "message_type": "group",
            "sender_id": "user-forward",
            "nickname": "Forward User",
            "raw_message": "[CQ:forward,id=forward-1]",
            "timestamp": 1783000300,
        },
        media_http_client=client,
        media_storage_root=tmp_path,
        media_public_prefix="/static/storage",
        forward_payload_loader=load_forward,
    )

    messages = await MessageService.list_messages(db_session)
    assets = await MessageService.list_media_assets(db_session)

    assert "local=/static/storage/" in messages[0].local_message
    assert "forward-1" in messages[0].local_message
    assert {asset.file_type for asset in assets} == {"image", "forward"}
