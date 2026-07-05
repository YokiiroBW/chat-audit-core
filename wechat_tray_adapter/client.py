from __future__ import annotations

import json
import mimetypes
from pathlib import Path
import secrets
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError

from wechat_tray_adapter.config import AdapterConfig


class NasClientError(RuntimeError):
    pass


def encode_multipart_form(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    boundary = f"----chat-audit-{secrets.token_hex(16)}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    for name, (filename, content, content_type) in files.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode("utf-8"),
                f"Content-Type: {content_type}\r\n\r\n".encode("ascii"),
                content,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class NasClient:
    def __init__(self, config: AdapterConfig, timeout: int = 30):
        self.config = config
        self.timeout = timeout

    def send_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request_json("POST", "/api/receive_external_msg", payload)

    def upload_media(self, path: str | Path, media_type: str, file_name: str | None = None) -> dict[str, Any]:
        file_path = Path(path)
        content = file_path.read_bytes()
        display_name = file_name or file_path.name
        content_type = mimetypes.guess_type(display_name)[0] or "application/octet-stream"
        body, content_type_header = encode_multipart_form(
            {"media_type": media_type, "file_name": display_name},
            {"file": (display_name, content, content_type)},
        )
        return self._request_bytes("POST", "/api/external/media", body, content_type_header)

    def _request_json(self, method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self._request_bytes(method, path, body, "application/json")

    def _request_bytes(self, method: str, path: str, body: bytes, content_type: str) -> dict[str, Any]:
        req = request.Request(
            f"{self.config.normalized_nas_url}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.config.token}",
                "Content-Type": content_type,
                "Accept": "application/json",
                "User-Agent": "chat-audit-wechat-tray/0.1",
            },
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                response_body = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise NasClientError(f"NAS HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise NasClientError(f"NAS connection failed: {exc.reason}") from exc

        if not response_body:
            return {}
        return json.loads(response_body.decode("utf-8"))
