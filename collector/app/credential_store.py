from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path
from typing import Protocol


class CredentialStoreError(RuntimeError):
    pass


class DataProtector(Protocol):
    def protect(self, plaintext: bytes) -> bytes: ...

    def unprotect(self, ciphertext: bytes) -> bytes: ...


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


class WindowsDpapiProtector:
    _CRYPTPROTECT_UI_FORBIDDEN = 0x1

    def __init__(self, entropy: bytes = b"ChatAuditQQCollector/v1") -> None:
        if os.name != "nt":
            raise CredentialStoreError("Windows DPAPI is only available on Windows")
        self._entropy = entropy
        self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p

    @staticmethod
    def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
        buffer = ctypes.create_string_buffer(data)
        blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        return blob, buffer

    def _transform(self, data: bytes, *, protect: bool) -> bytes:
        input_blob, input_buffer = self._blob(data)
        entropy_blob, entropy_buffer = self._blob(self._entropy)
        output_blob = _DataBlob()
        description = wintypes.LPWSTR()
        if protect:
            ok = self._crypt32.CryptProtectData(
                ctypes.byref(input_blob),
                "Chat Audit QQ Collector",
                ctypes.byref(entropy_blob),
                None,
                None,
                self._CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output_blob),
            )
        else:
            ok = self._crypt32.CryptUnprotectData(
                ctypes.byref(input_blob),
                ctypes.byref(description),
                ctypes.byref(entropy_blob),
                None,
                None,
                self._CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output_blob),
            )
        del input_buffer, entropy_buffer
        if not ok:
            raise CredentialStoreError(f"DPAPI operation failed with Windows error {ctypes.get_last_error()}")
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            if output_blob.pbData:
                self._kernel32.LocalFree(output_blob.pbData)
            if description:
                self._kernel32.LocalFree(description)

    def protect(self, plaintext: bytes) -> bytes:
        return self._transform(plaintext, protect=True)

    def unprotect(self, ciphertext: bytes) -> bytes:
        return self._transform(ciphertext, protect=False)


class CredentialStore:
    _MAGIC = b"CAQCRED1\x00"

    def __init__(self, path: str | Path, protector: DataProtector | None = None) -> None:
        self.path = Path(path).expanduser().resolve()
        self._protector = protector

    @property
    def protector(self) -> DataProtector:
        if self._protector is None:
            self._protector = WindowsDpapiProtector()
        return self._protector

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        payload = self.path.read_bytes()
        if not payload.startswith(self._MAGIC):
            raise CredentialStoreError("credential store header is invalid")
        try:
            decoded = json.loads(self.protector.unprotect(payload[len(self._MAGIC) :]).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
            raise CredentialStoreError("credential store cannot be decrypted") from exc
        if not isinstance(decoded, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in decoded.items()):
            raise CredentialStoreError("credential store payload is invalid")
        return decoded

    def _save(self, values: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        plaintext = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        payload = self._MAGIC + self.protector.protect(plaintext)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_bytes(payload)
        os.replace(temporary, self.path)

    def set(self, name: str, value: str) -> None:
        normalized = name.strip()
        if not normalized or not value:
            raise CredentialStoreError("credential name and value are required")
        values = self._load()
        values[normalized] = value
        self._save(values)

    def get(self, name: str) -> str | None:
        return self._load().get(name.strip())

    def delete(self, name: str) -> bool:
        values = self._load()
        removed = values.pop(name.strip(), None) is not None
        if removed:
            self._save(values)
        return removed

    def list_names(self) -> list[str]:
        return sorted(self._load())
