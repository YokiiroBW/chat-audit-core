from __future__ import annotations

import hashlib
import logging
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx

from collector import __version__
from collector.app.config import CollectorConfig, transport_security_warnings
from collector.sync.queue import CollectorQueue, QueueItem
from collector.sync.state import CollectorStateStore


logger = logging.getLogger("collector.uploader")


class CollectorApiError(RuntimeError):
    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        retryable: bool = True,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
        self.status_code = status_code


class CollectorApiClient:
    def __init__(
        self,
        config: CollectorConfig,
        token_provider: Callable[[], str | None],
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self.token_provider = token_provider
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "CollectorApiClient":
        if self._client is None:
            # Surfaced every time a connection is opened rather than once at
            # startup: an operator who edits the server URL mid-session should
            # see the consequence in the same log they are already watching.
            for warning in transport_security_warnings(self.config.server):
                logger.warning("Collector transport is not protected", extra={"warning": warning})
            self._client = httpx.AsyncClient(
                base_url=self.config.server.base_url,
                timeout=self.config.server.request_timeout_seconds,
                verify=self.config.server.verify_tls,
            )
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        token = self.token_provider()
        if not token:
            raise CollectorApiError("SERVER_AUTH_FAILED", "collector API token is not configured", retryable=False)
        return {"Authorization": f"Bearer {token}"}

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if self._client is None:
            raise RuntimeError("CollectorApiClient must be used as an async context manager")
        headers = dict(kwargs.pop("headers", {}))
        headers.update(self._headers())
        try:
            response = await self._client.request(method, path, headers=headers, **kwargs)
        except httpx.RequestError as exc:
            raise CollectorApiError("SERVER_UNAVAILABLE", "Chat Audit Core is unavailable") from exc
        if response.status_code in {401, 403}:
            raise CollectorApiError("SERVER_AUTH_FAILED", "collector API authentication failed", retryable=False, status_code=response.status_code)
        if response.status_code >= 500:
            raise CollectorApiError("SERVER_UNAVAILABLE", f"server returned HTTP {response.status_code}", status_code=response.status_code)
        if response.status_code >= 400:
            detail = None
            try:
                body = response.json()
                detail = body.get("detail") if isinstance(body, dict) else None
            except ValueError:
                detail = None
            raise CollectorApiError(
                "SERVER_BATCH_REJECTED",
                str(detail or f"server rejected request with HTTP {response.status_code}"),
                retryable=response.status_code in {408, 409, 425, 429},
                status_code=response.status_code,
            )
        return response

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise CollectorApiError("SERVER_BATCH_REJECTED", "server returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise CollectorApiError("SERVER_BATCH_REJECTED", "server returned a non-object JSON response")
        return payload

    async def register_source(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source_type": "qqnt_local_db",
            "platform": "qq",
            "account_id": self.config.collector.account_id,
            "device_id": self.config.collector.device_id,
            "device_name": self.config.collector.device_name,
            "status": "active",
            "metadata": {"collector_version": __version__},
        }
        if self.config.server.source_id:
            payload["id"] = self.config.server.source_id
        return self._json_object(await self._request("POST", "/api/import/sources", json=payload))

    async def create_batch(self, *, batch_id: str, source_id: str, mode: str) -> dict[str, Any]:
        return self._json_object(
            await self._request(
                "POST",
                "/api/import/batches",
                json={"id": batch_id, "source_id": source_id, "mode": mode, "detail": {"client": "windows-collector"}},
            )
        )

    async def upload_media(self, item: QueueItem) -> dict[str, Any]:
        if not item.staging_path or not item.media_type:
            raise CollectorApiError("MEDIA_UPLOAD_FAILED", "media queue item is incomplete", retryable=False)
        path = Path(item.staging_path)
        if not path.is_file() or path.stat().st_size <= 0:
            raise CollectorApiError("MEDIA_SOURCE_MISSING", "staged media file is missing", retryable=False)
        with path.open("rb") as file:
            response = await self._request(
                "POST",
                "/api/external/media",
                data={"media_type": item.media_type, "file_name": item.file_name or path.name},
                files={"file": (item.file_name or path.name, file, "application/octet-stream")},
            )
        payload = self._json_object(response)
        if payload.get("file_hash") != item.file_hash or payload.get("file_size") != item.file_size:
            raise CollectorApiError("MEDIA_HASH_MISMATCH", "server media confirmation does not match staged content")
        return payload

    async def upload_messages(self, *, batch_id: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return self._json_object(
            await self._request(
                "POST",
                f"/api/import/batches/{batch_id}/messages",
                json={"messages": messages},
            )
        )

    async def complete_batch(self, *, batch_id: str, partial: bool) -> dict[str, Any]:
        return self._json_object(
            await self._request(
                "POST",
                f"/api/import/batches/{batch_id}/complete",
                json={"partial": partial, "detail": {"client": "windows-collector"}},
            )
        )

    async def fail_batch(self, *, batch_id: str, error_code: str, error_detail: str) -> dict[str, Any]:
        return self._json_object(
            await self._request(
                "POST",
                f"/api/import/batches/{batch_id}/fail",
                json={"error_code": error_code, "error_detail": error_detail, "detail": {"client": "windows-collector"}},
            )
        )


class QueueUploader:
    def __init__(
        self,
        config: CollectorConfig,
        store: CollectorStateStore,
        queue: CollectorQueue,
        api: CollectorApiClient,
        progress_callback: Callable[[str, str], None] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.queue = queue
        self.api = api
        self.progress_callback = progress_callback or (lambda _event, _detail: None)
        identity = f"qqnt_local_db:{config.collector.account_id}:{config.collector.device_id}"
        self.local_source_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def _emit_progress(self, event: str, detail: str) -> None:
        self.progress_callback(event, detail)

    def bootstrap_local_source(self) -> None:
        self.store.upsert_source(
            source_id=self.local_source_id,
            source_type="qqnt_local_db",
            account_id=self.config.collector.account_id,
            device_id=self.config.collector.device_id,
            metadata={"device_name": self.config.collector.device_name},
        )

    async def ensure_server_source(self) -> str:
        source = await self.api.register_source()
        server_source_id_value = source.get("id")
        if not isinstance(server_source_id_value, str) or not server_source_id_value:
            raise CollectorApiError("SERVER_BATCH_REJECTED", "source registration response is missing id")
        server_source_id = server_source_id_value
        self.store.update_server_source_id(self.local_source_id, server_source_id)
        return server_source_id

    async def _upload_media(self, limit: int) -> dict[str, int]:
        stats = {"leased": 0, "completed": 0, "retry": 0, "dead_letter": 0}
        items = self.queue.lease_ready("media", limit=limit)
        self._emit_progress("upload.media.started", str(len(items)))
        for item in items:
            stats["leased"] += 1
            try:
                response = await self.api.upload_media(item)
                if item.message_queue_id is None or item.ordinal is None:
                    raise CollectorApiError("MEDIA_UPLOAD_FAILED", "media queue relationship is incomplete", retryable=False)
                patch_payload = dict(response)
                if item.media_role == "artifact":
                    replace_token = item.payload.get("replace_token")
                    replacement_template = item.payload.get("replacement_template")
                    if not isinstance(replace_token, str) or not isinstance(replacement_template, str):
                        raise CollectorApiError("MEDIA_UPLOAD_FAILED", "artifact queue metadata is incomplete", retryable=False)
                    local_path = response.get("local_path")
                    if not isinstance(local_path, str) or not local_path:
                        # A server reply without a stored path used to raise
                        # KeyError here, which is not retryable and took the
                        # upload loop with it instead of failing this one item.
                        raise CollectorApiError(
                            "MEDIA_UPLOAD_FAILED",
                            "server accepted the upload without returning a stored path",
                            retryable=True,
                        )
                    patch_payload.update(
                        {
                            "replace_token": replace_token,
                            "replacement": replacement_template.format(local_path=local_path),
                        }
                    )
                self.queue.patch_message_media(
                    item.message_queue_id,
                    item.ordinal,
                    patch_payload,
                    media_role=item.media_role or "asset",
                )
                self.queue.mark_completed(item, response)
                stats["completed"] += 1
            except CollectorApiError as exc:
                status = self.queue.mark_retry(item, exc.error_code, max_attempts=8 if exc.retryable else 1)
                stats[status] += 1
                logger.warning(
                    "media upload deferred",
                    extra={"event": "media.upload", "error_code": exc.error_code},
                )
        self._emit_progress(
            "upload.media.completed",
            f"{stats['leased']}|{stats['completed']}|{stats['retry']}|{stats['dead_letter']}",
        )
        return stats

    @staticmethod
    def _batch_id(items: list[QueueItem], mode: str) -> str:
        identity = f"{mode}:" + ":".join(item.id for item in items)
        return "collector-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:48]

    @staticmethod
    def _fresh_batch_id(items: list[QueueItem], mode: str) -> str:
        identity = f"{mode}:" + ":".join(item.id for item in items)
        nonce = uuid.uuid4().hex
        return "collector-" + hashlib.sha256(f"{identity}:{nonce}".encode("utf-8")).hexdigest()[:48]

    async def _create_writable_batch(
        self,
        *,
        source_id: str,
        mode: str,
        items: list[QueueItem],
    ) -> str:
        batch_id = self._batch_id(items, mode)
        response = await self.api.create_batch(batch_id=batch_id, source_id=source_id, mode=mode)
        status = response.get("status")
        completed_at = response.get("completed_at")
        terminal = status in {"completed", "failed", "cancelled"} or (
            status == "partial" and completed_at is not None
        )
        if not terminal:
            return batch_id

        replacement_id = self._fresh_batch_id(items, mode)
        logger.info(
            "creating replacement batch for terminal batch",
            extra={
                "event": "batch.replaced",
                "batch_id": batch_id,
                "replacement_batch_id": replacement_id,
                "status": status,
            },
        )
        await self.api.create_batch(batch_id=replacement_id, source_id=source_id, mode=mode)
        return replacement_id

    @staticmethod
    def _strip_nul(value: Any) -> Any:
        if isinstance(value, str):
            return value.replace("\x00", "")
        if isinstance(value, dict):
            return {key: QueueUploader._strip_nul(item) for key, item in value.items()}
        if isinstance(value, list):
            return [QueueUploader._strip_nul(item) for item in value]
        return value

    @staticmethod
    def _normalized_message_payload(item: QueueItem) -> dict[str, Any]:
        payload = QueueUploader._strip_nul(dict(item.payload))
        message = dict(payload.get("message") or {})
        sender_id = str(message.get("sender_id") or "").strip()
        if not sender_id:
            source_record = dict(payload.get("source_record") or {})
            source_key = str(source_record.get("source_key") or item.id)
            source_table = str(source_record.get("source_table") or "message")
            sender_id = "unknown:" + hashlib.sha256(f"{source_table}:{source_key}".encode("utf-8")).hexdigest()[:32]
            message["sender_id"] = sender_id
            metadata = dict(source_record.get("metadata") or {})
            parser_failures = list(metadata.get("parser_failures") or [])
            if "MISSING_SENDER_ID" not in parser_failures:
                parser_failures.append("MISSING_SENDER_ID")
            metadata["parser_failures"] = parser_failures
            source_record["metadata"] = metadata
            payload["source_record"] = source_record
        payload["message"] = message
        return payload

    async def _upload_message_items(
        self,
        *,
        source_id: str,
        mode: str,
        items: list[QueueItem],
        stats: dict[str, int],
    ) -> None:
        if not items:
            return
        # Bound before the try: creating the batch is itself a request that can
        # fail, and the error handler below logs batch_id. Leaving it unbound turns
        # a routine retryable failure into an UnboundLocalError, which is not a
        # CollectorApiError and so escapes run_once and kills the daemon.
        batch_id: str | None = None
        try:
            batch_id = await self._create_writable_batch(
                source_id=source_id,
                mode=mode,
                items=items,
            )
            response = await self.api.upload_messages(
                batch_id=batch_id,
                messages=[self._normalized_message_payload(item) for item in items],
            )
            results = response.get("items")
            if not isinstance(results, list) or len(results) != len(items):
                raise CollectorApiError("SERVER_BATCH_REJECTED", "batch response item count is invalid")
            result_by_index: dict[int, dict[str, Any]] = {}
            for result in results:
                if not isinstance(result, dict) or not isinstance(result.get("index"), int):
                    raise CollectorApiError("SERVER_BATCH_REJECTED", "batch response contains an invalid item")
                index = int(result["index"])
                if index in result_by_index or not 0 <= index < len(items):
                    raise CollectorApiError("SERVER_BATCH_REJECTED", "batch response contains an invalid item index")
                if result.get("status") not in {"inserted", "updated", "unchanged", "failed"}:
                    raise CollectorApiError("SERVER_BATCH_REJECTED", "batch response contains an invalid item status")
                result_by_index[index] = result
            if set(result_by_index) != set(range(len(items))):
                raise CollectorApiError("SERVER_BATCH_REJECTED", "batch response item indexes are incomplete")
            failed_indexes = {
                index for index, result in result_by_index.items() if result.get("status") == "failed"
            }
            await self.api.complete_batch(batch_id=batch_id, partial=bool(failed_indexes))
        except CollectorApiError as exc:
            if exc.status_code == 422 and len(items) > 1:
                midpoint = len(items) // 2
                logger.warning(
                    "splitting rejected message batch",
                    extra={
                        "event": "batch.upload.split",
                        "batch_id": batch_id,
                        "error_code": exc.error_code,
                        "error_detail": str(exc),
                        "batch_size": len(items),
                    },
                )
                await self._upload_message_items(
                    source_id=source_id, mode=mode, items=items[:midpoint], stats=stats
                )
                await self._upload_message_items(
                    source_id=source_id, mode=mode, items=items[midpoint:], stats=stats
                )
                return
            for item in items:
                status = self.queue.mark_retry(item, exc.error_code, max_attempts=8 if exc.retryable else 1)
                stats[status] += 1
            logger.warning(
                "message batch upload deferred",
                extra={
                    "event": "batch.upload",
                    "batch_id": batch_id,
                    "error_code": exc.error_code,
                    "error_detail": str(exc),
                    "status_code": exc.status_code,
                    "batch_size": len(items),
                },
            )
            return

        for index, item in enumerate(items):
            if index in failed_indexes:
                status = self.queue.mark_retry(item, "SERVER_BATCH_REJECTED")
                stats[status] += 1
                stats["failed"] += 1
            else:
                self.queue.mark_completed(item, {"batch_id": batch_id})
                stats["completed"] += 1

    async def _upload_messages(self, *, source_id: str, mode: str, limit: int) -> dict[str, int]:
        stats = {"leased": 0, "completed": 0, "retry": 0, "dead_letter": 0, "failed": 0}
        items = self.queue.lease_ready("message", limit=limit)
        if not items:
            return stats
        stats["leased"] = len(items)
        self._emit_progress("upload.messages.started", f"{mode}|{len(items)}")
        await self._upload_message_items(source_id=source_id, mode=mode, items=items, stats=stats)
        self._emit_progress(
            "upload.messages.completed",
            f"{stats['leased']}|{stats['completed']}|{stats['retry']}|{stats['dead_letter']}|{stats['failed']}",
        )
        return stats

    async def run_cycle(self, *, mode: str = "incremental") -> dict[str, Any]:
        self.bootstrap_local_source()
        server_source_id = await self.ensure_server_source()
        media_stats = await self._upload_media(self.config.collector.incremental_batch_size)
        message_limit = (
            self.config.collector.initial_import_batch_size
            if mode == "initial"
            else self.config.collector.incremental_batch_size
        )
        message_stats = await self._upload_messages(source_id=server_source_id, mode=mode, limit=message_limit)
        return {"media": media_stats, "messages": message_stats}
