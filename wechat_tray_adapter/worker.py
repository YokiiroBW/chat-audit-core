from __future__ import annotations

from typing import Any, Protocol

from wechat_tray_adapter.client import NasClientError
from wechat_tray_adapter.config import AdapterConfig
from wechat_tray_adapter.mapper import build_nas_event, discover_media_path, media_upload_type
from wechat_tray_adapter.queue import PendingEventQueue, PendingItem


class ClientProtocol(Protocol):
    def upload_media(self, path: str, media_type: str, file_name: str | None = None) -> dict[str, Any]:
        ...

    def send_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        ...


class SyncWorker:
    def __init__(self, config: AdapterConfig, client: ClientProtocol, queue: PendingEventQueue):
        self.config = config
        self.client = client
        self.queue = queue

    def handle_wcf_message(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        if self.config.paused:
            return None
        try:
            payload = self._build_payload(raw)
            return self.client.send_event(payload)
        except Exception as exc:
            self.queue.enqueue("message", {"raw": raw, "error": str(exc)})
            return None

    def flush_pending(self, limit: int = 100) -> int:
        sent = 0
        for item in self.queue.list_pending(limit=limit):
            if self._flush_one(item):
                sent += 1
        return sent

    def _flush_one(self, item: PendingItem) -> bool:
        try:
            payload = item.payload.get("payload")
            if payload is None:
                raw = item.payload["raw"]
                payload = self._build_payload(raw)
            self.client.send_event(payload)
        except Exception as exc:
            self.queue.mark_failed(item.id, str(exc))
            return False
        self.queue.mark_done(item.id)
        return True

    def _build_payload(self, raw: dict[str, Any]) -> dict[str, Any]:
        uploaded = None
        media_type = media_upload_type(raw)
        media_path = discover_media_path(raw) if self.config.auto_download_media else None
        if media_type and media_path is not None:
            try:
                uploaded = self.client.upload_media(str(media_path), media_type=media_type, file_name=media_path.name)
            except NasClientError:
                raise
        return build_nas_event(raw, self.config, uploaded_media=uploaded)
