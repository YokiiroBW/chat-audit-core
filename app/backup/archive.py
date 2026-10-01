from __future__ import annotations

import contextlib
import copy
import errno
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import struct
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO


BACKUP_ARCHIVE_SCHEMA = "chat-audit-core.backup.v4"
BACKUP_ARCHIVE_FORMAT = "tar+gzip"
BACKUP_SIGNATURE_ALGORITHM = "hmac-sha256"
INTEGRITY_FRAMING = "cacb-member-frame-v1"
DEFAULT_CHUNK_BYTES = 8 * 1024 * 1024
COPY_CHUNK_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024

_DB_MEMBER_RE = re.compile(r"^db/(?P<section>[a-z][a-z0-9_]*)/(?P<chunk>[0-9]{8})\.jsonl$")
_MEDIA_MEMBER_RE = re.compile(r"^media/(?P<file_hash>[A-Za-z0-9._-]{1,160})$")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_without_overwrite(source: Path, destination: Path) -> None:
    """Atomically add the final name while refusing to replace any file.

    Both paths are created in the backup directory. A same-filesystem hard
    link therefore publishes the already-fsynced inode in one atomic directory
    operation and, unlike ``os.replace``, cannot clobber an operator's existing
    backup if a name is reused. The hidden source name is removed only after
    the final name is durable.
    """

    os.link(source, destination, follow_symlinks=False)
    _fsync_directory(destination.parent)
    try:
        source.unlink()
    except FileNotFoundError:
        pass
    _fsync_directory(destination.parent)


def _safe_error_text(error: BaseException, limit: int = 1000) -> str:
    value = f"{type(error).__name__}: {error}".replace("\x00", "?")
    return value if len(value) <= limit else value[: limit - 3] + "..."


class ArchiveValidationError(ValueError):
    pass


class _IntegrityAccumulator:
    def __init__(self, signing_key: str):
        self.sha256 = hashlib.sha256()
        self.hmac_sha256 = hmac.new(signing_key.encode("utf-8"), digestmod=hashlib.sha256)
        self.members = 0
        self.uncompressed_bytes = 0

    @staticmethod
    def _frame_header(name: str, size: int) -> bytes:
        header = canonical_json_bytes({"name": name, "size": size})
        return struct.pack(">Q", len(header)) + header

    def start_member(self, name: str, size: int) -> None:
        framed = self._frame_header(name, size)
        self.sha256.update(framed)
        self.hmac_sha256.update(framed)
        self.members += 1
        self.uncompressed_bytes += size

    def update(self, content: bytes) -> None:
        self.sha256.update(content)
        self.hmac_sha256.update(content)


class _DigestingReader(io.RawIOBase):
    def __init__(self, source: BinaryIO, integrity: _IntegrityAccumulator, member_digest: Any | None = None):
        self._source = source
        self._integrity = integrity
        self._member_digest = member_digest
        self.bytes_read = 0

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        content = self._source.read(size)
        if content:
            self._integrity.update(content)
            if self._member_digest is not None:
                self._member_digest.update(content)
            self.bytes_read += len(content)
        return content


def _tar_info(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name=name)
    info.size = size
    info.mode = 0o600
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def _manifest_envelope_bytes(manifest: dict[str, Any]) -> bytes:
    envelope = copy.deepcopy(manifest)
    integrity = envelope.get("integrity")
    if not isinstance(integrity, dict):
        raise ArchiveValidationError("manifest.integrity must be an object")
    integrity.pop("manifest_checksum", None)
    integrity.pop("signature", None)
    return canonical_json_bytes(envelope)


@dataclass(frozen=True)
class ArchiveValidationReport:
    valid: bool
    manifest: dict[str, Any]
    counts: dict[str, int]
    members: int
    uncompressed_bytes: int
    checksum_valid: bool
    signature_valid: bool
    extracted_root: Path | None = None


class JsonlSectionWriter:
    def __init__(self, archive: "BackupArchiveWriter", section: str, chunk_bytes: int):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", section):
            raise ValueError(f"invalid backup section name: {section!r}")
        self._archive = archive
        self.section = section
        self.chunk_bytes = max(1, int(chunk_bytes))
        self.count = 0
        self._chunk_index = 0
        self._path: Path | None = None
        self._file: BinaryIO | None = None
        self._size = 0

    def _open_chunk(self) -> None:
        if self._file is not None:
            return
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{self.section}-",
            suffix=".jsonl",
            dir=self._archive.workspace,
        )
        self._path = Path(raw_path)
        self._file = os.fdopen(descriptor, "wb")
        self._size = 0

    def write(self, record: dict[str, Any]) -> None:
        self._archive._ensure_open()
        self._open_chunk()
        assert self._file is not None
        line = canonical_json_bytes(record) + b"\n"
        self._file.write(line)
        self._size += len(line)
        self.count += 1
        if self._size >= self.chunk_bytes:
            self.flush_chunk()

    def flush_chunk(self) -> None:
        if self._file is None or self._path is None:
            return
        path = self._path
        file_object = self._file
        file_object.flush()
        os.fsync(file_object.fileno())
        file_object.close()
        self._file = None
        self._path = None
        member_name = f"db/{self.section}/{self._chunk_index:08d}.jsonl"
        self._chunk_index += 1
        try:
            self._archive.add_file(member_name, path)
        finally:
            path.unlink(missing_ok=True)
        self._size = 0

    def close(self) -> int:
        self.flush_chunk()
        return self.count

    def abort(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        if self._path is not None:
            self._path.unlink(missing_ok=True)
            self._path = None

    def __enter__(self) -> "JsonlSectionWriter":
        return self

    def __exit__(self, exc_type, exc, _traceback) -> bool:
        if exc is None:
            self.close()
        else:
            self.abort()
        return False


class BackupArchiveWriter:
    """Write a v4 archive without retaining database sections or media in RAM."""

    def __init__(
        self,
        *,
        final_path: Path,
        metadata: dict[str, Any],
        signing_key: str,
        key_id: str,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
        min_free_bytes: int = 512 * 1024 * 1024,
    ):
        if not signing_key:
            raise ValueError("v4 backup signing key is required")
        self.final_path = Path(final_path)
        self.metadata = copy.deepcopy(metadata)
        self.signing_key = signing_key
        self.key_id = key_id
        self.chunk_bytes = chunk_bytes
        self.min_free_bytes = max(0, int(min_free_bytes))
        self.final_path.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(8)
        self.incomplete_path = self.final_path.parent / f".{self.final_path.name}.{token}.incomplete"
        self.failed_path = self.final_path.parent / f".{self.final_path.name}.{token}.failed"
        self.workspace = self.final_path.parent / f".backup-work-{token}"
        self.workspace.mkdir(mode=0o700)
        self._output = self.incomplete_path.open("xb")
        self._tar = tarfile.open(fileobj=self._output, mode="w|gz", format=tarfile.PAX_FORMAT)
        self._integrity = _IntegrityAccumulator(signing_key)
        self._counts: dict[str, int] = {}
        self._closed = False
        self._last_member_name: str | None = None

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("backup archive writer is closed")

    def section(self, name: str) -> JsonlSectionWriter:
        return JsonlSectionWriter(self, name, self.chunk_bytes)

    def set_count(self, name: str, value: int) -> None:
        if int(value) < 0:
            raise ValueError(f"negative backup count for {name}")
        self._counts[name] = int(value)

    def _check_member_name(self, name: str) -> None:
        if name == "manifest.json" or _DB_MEMBER_RE.fullmatch(name) or _MEDIA_MEMBER_RE.fullmatch(name):
            return
        raise ValueError(f"invalid backup archive member name: {name!r}")

    def add_file(self, name: str, path: Path, *, calculate_sha256: bool = False) -> str | None:
        self._ensure_open()
        self._check_member_name(name)
        source_path = Path(path)
        stat_before = source_path.stat()
        if not source_path.is_file():
            raise ValueError(f"backup member source is not a regular file: {source_path}")
        free_bytes = shutil.disk_usage(self.final_path.parent).free
        if free_bytes < self.min_free_bytes + stat_before.st_size:
            raise OSError(
                errno.ENOSPC,
                f"insufficient backup space: free={free_bytes} required={self.min_free_bytes + stat_before.st_size}",
            )
        self._integrity.start_member(name, stat_before.st_size)
        member_digest = hashlib.sha256() if calculate_sha256 else None
        with source_path.open("rb", buffering=0) as source:
            reader = _DigestingReader(source, self._integrity, member_digest)
            self._tar.addfile(_tar_info(name, stat_before.st_size), reader)
            if reader.bytes_read != stat_before.st_size:
                raise OSError(f"backup member changed or became unreadable while copying: {source_path}")
        stat_after = source_path.stat()
        if (
            stat_after.st_size != stat_before.st_size
            or stat_after.st_mtime_ns != stat_before.st_mtime_ns
            or stat_after.st_ino != stat_before.st_ino
        ):
            raise OSError(f"backup member changed while copying: {source_path}")
        self._last_member_name = name
        return member_digest.hexdigest() if member_digest is not None else None

    def add_bytes(self, name: str, content: bytes) -> None:
        self._ensure_open()
        self._check_member_name(name)
        free_bytes = shutil.disk_usage(self.final_path.parent).free
        if free_bytes < self.min_free_bytes + len(content):
            raise OSError(
                errno.ENOSPC,
                f"insufficient backup space: free={free_bytes} required={self.min_free_bytes + len(content)}",
            )
        self._integrity.start_member(name, len(content))
        self._integrity.update(content)
        self._tar.addfile(_tar_info(name, len(content)), io.BytesIO(content))
        self._last_member_name = name

    def add_media(self, *, file_hash: str, path: Path) -> dict[str, Any]:
        name = f"media/{file_hash}"
        checksum = self.add_file(name, path, calculate_sha256=True)
        assert checksum is not None
        return {
            "archive_member": name,
            "file_checksum": {"algorithm": "sha256", "value": checksum},
            "archived_size": Path(path).stat().st_size,
        }

    def _build_manifest(self) -> dict[str, Any]:
        manifest: dict[str, Any] = {
            "schema": BACKUP_ARCHIVE_SCHEMA,
            "archive_format": BACKUP_ARCHIVE_FORMAT,
            **copy.deepcopy(self.metadata),
            "counts": dict(sorted(self._counts.items())),
            "payload": {
                "members": self._integrity.members,
                "uncompressed_bytes": self._integrity.uncompressed_bytes,
            },
            "integrity": {
                "framing": INTEGRITY_FRAMING,
                "checksum": {"algorithm": "sha256", "value": self._integrity.sha256.hexdigest()},
                "payload_signature": {
                    "algorithm": BACKUP_SIGNATURE_ALGORITHM,
                    "key_id": self.key_id,
                    "value": self._integrity.hmac_sha256.hexdigest(),
                },
            },
        }
        envelope = _manifest_envelope_bytes(manifest)
        manifest["integrity"]["manifest_checksum"] = {
            "algorithm": "sha256",
            "value": hashlib.sha256(envelope).hexdigest(),
        }
        manifest["integrity"]["signature"] = {
            "algorithm": BACKUP_SIGNATURE_ALGORITHM,
            "key_id": self.key_id,
            "value": hmac.new(self.signing_key.encode("utf-8"), envelope, hashlib.sha256).hexdigest(),
        }
        return manifest

    def finalize(self) -> tuple[Path, dict[str, Any]]:
        self._ensure_open()
        manifest = self._build_manifest()
        manifest_bytes = canonical_json_bytes(manifest) + b"\n"
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise ValueError("backup manifest exceeds its bounded size")
        self._tar.addfile(_tar_info("manifest.json", len(manifest_bytes)), io.BytesIO(manifest_bytes))
        self._tar.close()
        self._output.flush()
        os.fsync(self._output.fileno())
        self._output.close()
        self._closed = True

        validate_backup_archive(self.incomplete_path, signing_key=self.signing_key, require_signature=True)
        try:
            _publish_without_overwrite(self.incomplete_path, self.final_path)
        except FileExistsError as exc:
            raise FileExistsError(f"refusing to overwrite an existing backup: {self.final_path}") from exc
        shutil.rmtree(self.workspace, ignore_errors=True)
        return self.final_path, manifest

    def abort(self, error: BaseException | None = None) -> None:
        if not self._closed:
            with contextlib.suppress(Exception):
                self._tar.close()
            with contextlib.suppress(Exception):
                self._output.flush()
                os.fsync(self._output.fileno())
            with contextlib.suppress(Exception):
                self._output.close()
            self._closed = True
        if self.incomplete_path.exists():
            with contextlib.suppress(OSError):
                os.replace(self.incomplete_path, self.failed_path)
                if error is not None:
                    reason_path = self.failed_path.with_suffix(self.failed_path.suffix + ".reason")
                    reason_path.write_text(_safe_error_text(error) + "\n", encoding="utf-8")
        shutil.rmtree(self.workspace, ignore_errors=True)

    def __enter__(self) -> "BackupArchiveWriter":
        return self

    def __exit__(self, exc_type, exc, _traceback) -> bool:
        if exc is not None:
            self.abort(exc)
        elif not self._closed:
            self.abort(RuntimeError("backup archive writer exited without finalize"))
        return False


def _validation_index(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        CREATE TABLE members(name TEXT PRIMARY KEY);
        CREATE TABLE media_actual(name TEXT PRIMARY KEY, size INTEGER NOT NULL, checksum TEXT NOT NULL);
        CREATE TABLE media_expected(name TEXT PRIMARY KEY, size INTEGER NOT NULL, checksum TEXT NOT NULL);
        """
    )
    return connection


def _record_expected_media(index: sqlite3.Connection, record: dict[str, Any]) -> None:
    member = record.get("archive_member")
    if member is None:
        return
    checksum = record.get("file_checksum")
    if not isinstance(checksum, dict) or checksum.get("algorithm") != "sha256":
        raise ArchiveValidationError(f"media asset {record.get('file_hash')!r} has invalid archive checksum metadata")
    value = checksum.get("value")
    size = record.get("archived_size")
    if not isinstance(member, str) or not _MEDIA_MEMBER_RE.fullmatch(member):
        raise ArchiveValidationError(f"media asset {record.get('file_hash')!r} has an invalid archive member")
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ArchiveValidationError(f"media asset {record.get('file_hash')!r} has an invalid SHA-256 value")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ArchiveValidationError(f"media asset {record.get('file_hash')!r} has an invalid archived size")
    try:
        index.execute("INSERT INTO media_expected(name, size, checksum) VALUES (?, ?, ?)", (member, size, value))
    except sqlite3.IntegrityError as exc:
        raise ArchiveValidationError(f"duplicate media metadata for {member}") from exc


def _read_jsonl_member(
    member_file: BinaryIO,
    *,
    member_name: str,
    size: int,
    integrity: _IntegrityAccumulator,
    destination: Path | None,
    index: sqlite3.Connection,
) -> tuple[str, int]:
    match = _DB_MEMBER_RE.fullmatch(member_name)
    assert match is not None
    section = match.group("section")
    integrity.start_member(member_name, size)
    count = 0
    consumed = 0
    destination_file: BinaryIO | None = None
    if destination is not None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination_file = destination.open("xb")
    try:
        while True:
            line = member_file.readline()
            if not line:
                break
            consumed += len(line)
            integrity.update(line)
            if destination_file is not None:
                destination_file.write(line)
            if not line.endswith(b"\n"):
                raise ArchiveValidationError(f"unterminated JSONL record in {member_name}")
            try:
                record = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ArchiveValidationError(f"invalid JSONL record in {member_name}: {exc}") from exc
            if not isinstance(record, dict):
                raise ArchiveValidationError(f"non-object JSONL record in {member_name}")
            if section == "media_assets":
                _record_expected_media(index, record)
            count += 1
        if consumed != size:
            raise ArchiveValidationError(f"member size mismatch for {member_name}")
        if destination_file is not None:
            destination_file.flush()
            os.fsync(destination_file.fileno())
    finally:
        if destination_file is not None:
            destination_file.close()
    return section, count


def _read_media_member(
    member_file: BinaryIO,
    *,
    member_name: str,
    size: int,
    integrity: _IntegrityAccumulator,
    destination: Path | None,
    index: sqlite3.Connection,
) -> None:
    integrity.start_member(member_name, size)
    member_sha256 = hashlib.sha256()
    consumed = 0
    destination_file: BinaryIO | None = None
    if destination is not None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination_file = destination.open("xb")
    try:
        while consumed < size:
            content = member_file.read(min(COPY_CHUNK_BYTES, size - consumed))
            if not content:
                break
            consumed += len(content)
            integrity.update(content)
            member_sha256.update(content)
            if destination_file is not None:
                destination_file.write(content)
        if consumed != size:
            raise ArchiveValidationError(f"member size mismatch for {member_name}")
        if destination_file is not None:
            destination_file.flush()
            os.fsync(destination_file.fileno())
    finally:
        if destination_file is not None:
            destination_file.close()
    try:
        index.execute(
            "INSERT INTO media_actual(name, size, checksum) VALUES (?, ?, ?)",
            (member_name, size, member_sha256.hexdigest()),
        )
    except sqlite3.IntegrityError as exc:
        raise ArchiveValidationError(f"duplicate media member: {member_name}") from exc


def _validate_manifest(
    manifest: dict[str, Any],
    *,
    counts: dict[str, int],
    integrity: _IntegrityAccumulator,
    signing_key: str | None,
    require_signature: bool,
) -> tuple[bool, bool]:
    if manifest.get("schema") != BACKUP_ARCHIVE_SCHEMA:
        raise ArchiveValidationError(f"unsupported backup schema: {manifest.get('schema')!r}")
    if manifest.get("archive_format") != BACKUP_ARCHIVE_FORMAT:
        raise ArchiveValidationError(f"unsupported backup archive format: {manifest.get('archive_format')!r}")
    manifest_counts = manifest.get("counts")
    if not isinstance(manifest_counts, dict) or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in manifest_counts.values()
    ):
        raise ArchiveValidationError("backup manifest counts must be non-negative integers")
    extra_sections = sorted(set(counts) - set(manifest_counts))
    if extra_sections:
        raise ArchiveValidationError(
            f"backup manifest omits archive count(s): {', '.join(extra_sections)}"
        )
    calculated_counts = {name: counts.get(name, 0) for name in manifest_counts}
    if "missing_media_files" in calculated_counts:
        calculated_counts["missing_media_files"] = max(
            0,
            calculated_counts.get("media_assets", 0) - calculated_counts.get("media_files", 0),
        )
    if manifest_counts != dict(sorted(calculated_counts.items())):
        raise ArchiveValidationError("backup manifest counts do not match archive records")
    counts.clear()
    counts.update(calculated_counts)
    payload = manifest.get("payload")
    if not isinstance(payload, dict):
        raise ArchiveValidationError("manifest.payload must be an object")
    if payload.get("members") != integrity.members or payload.get("uncompressed_bytes") != integrity.uncompressed_bytes:
        raise ArchiveValidationError("backup manifest payload totals do not match archive members")
    values = manifest.get("integrity")
    if not isinstance(values, dict) or values.get("framing") != INTEGRITY_FRAMING:
        raise ArchiveValidationError("backup manifest has unsupported integrity framing")

    checksum = values.get("checksum")
    checksum_valid = (
        isinstance(checksum, dict)
        and checksum.get("algorithm") == "sha256"
        and isinstance(checksum.get("value"), str)
        and hmac.compare_digest(checksum["value"], integrity.sha256.hexdigest())
    )
    if not checksum_valid:
        raise ArchiveValidationError("backup archive payload checksum mismatch")

    payload_signature = values.get("payload_signature")
    payload_signature_valid = False
    if signing_key:
        payload_signature_valid = (
            isinstance(payload_signature, dict)
            and payload_signature.get("algorithm") == BACKUP_SIGNATURE_ALGORITHM
            and isinstance(payload_signature.get("value"), str)
            and hmac.compare_digest(payload_signature["value"], integrity.hmac_sha256.hexdigest())
        )
        if not payload_signature_valid:
            raise ArchiveValidationError("backup archive payload signature mismatch")
    elif require_signature:
        raise ArchiveValidationError("backup archive signature cannot be verified without a signing key")

    manifest_checksum = values.get("manifest_checksum")
    signature = values.get("signature")
    envelope = _manifest_envelope_bytes(manifest)
    envelope_checksum_valid = (
        isinstance(manifest_checksum, dict)
        and manifest_checksum.get("algorithm") == "sha256"
        and isinstance(manifest_checksum.get("value"), str)
        and hmac.compare_digest(manifest_checksum["value"], hashlib.sha256(envelope).hexdigest())
    )
    if not envelope_checksum_valid:
        raise ArchiveValidationError("backup manifest checksum mismatch")
    signature_valid = False
    if signing_key:
        expected = hmac.new(signing_key.encode("utf-8"), envelope, hashlib.sha256).hexdigest()
        signature_valid = (
            isinstance(signature, dict)
            and signature.get("algorithm") == BACKUP_SIGNATURE_ALGORITHM
            and isinstance(signature.get("value"), str)
            and hmac.compare_digest(signature["value"], expected)
            and payload_signature_valid
        )
        if not signature_valid:
            raise ArchiveValidationError("backup manifest signature mismatch")
    elif require_signature:
        raise ArchiveValidationError("backup manifest signature cannot be verified without a signing key")
    return checksum_valid and envelope_checksum_valid, signature_valid


def validate_backup_archive(
    archive_path: Path,
    *,
    signing_key: str | None,
    require_signature: bool = True,
    extract_root: Path | None = None,
) -> ArchiveValidationReport:
    """Validate every archive byte and optionally stage members for restore."""

    source_path = Path(archive_path)
    if extract_root is not None:
        extract_root = Path(extract_root)
        extract_root.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="cacb-validate-", dir=source_path.parent) as temporary:
        index = _validation_index(Path(temporary) / "index.sqlite3")
        integrity = _IntegrityAccumulator(signing_key or "")
        counts: dict[str, int] = {}
        manifest: dict[str, Any] | None = None
        saw_manifest = False
        try:
            with tarfile.open(source_path, mode="r|gz") as archive:
                for member in archive:
                    if not member.isfile():
                        raise ArchiveValidationError(f"unsupported non-file archive member: {member.name}")
                    if saw_manifest:
                        raise ArchiveValidationError("manifest.json must be the final archive member")
                    try:
                        index.execute("INSERT INTO members(name) VALUES (?)", (member.name,))
                    except sqlite3.IntegrityError as exc:
                        raise ArchiveValidationError(f"duplicate archive member: {member.name}") from exc
                    member_file = archive.extractfile(member)
                    if member_file is None:
                        raise ArchiveValidationError(f"cannot read archive member: {member.name}")
                    if member.name == "manifest.json":
                        saw_manifest = True
                        if member.size > MAX_MANIFEST_BYTES:
                            raise ArchiveValidationError("backup manifest exceeds its bounded size")
                        content = member_file.read(MAX_MANIFEST_BYTES + 1)
                        if len(content) != member.size:
                            raise ArchiveValidationError("backup manifest size mismatch")
                        try:
                            parsed = json.loads(content)
                        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                            raise ArchiveValidationError(f"invalid backup manifest: {exc}") from exc
                        if not isinstance(parsed, dict):
                            raise ArchiveValidationError("backup manifest must be an object")
                        manifest = parsed
                        continue
                    db_match = _DB_MEMBER_RE.fullmatch(member.name)
                    media_match = _MEDIA_MEMBER_RE.fullmatch(member.name)
                    if db_match:
                        destination = extract_root / member.name if extract_root is not None else None
                        section, count = _read_jsonl_member(
                            member_file,
                            member_name=member.name,
                            size=member.size,
                            integrity=integrity,
                            destination=destination,
                            index=index,
                        )
                        counts[section] = counts.get(section, 0) + count
                    elif media_match:
                        destination = extract_root / member.name if extract_root is not None else None
                        _read_media_member(
                            member_file,
                            member_name=member.name,
                            size=member.size,
                            integrity=integrity,
                            destination=destination,
                            index=index,
                        )
                        counts["media_files"] = counts.get("media_files", 0) + 1
                    else:
                        raise ArchiveValidationError(f"invalid archive member name: {member.name!r}")
            if manifest is None:
                raise ArchiveValidationError("backup archive manifest is missing")
            mismatch = index.execute(
                """
                SELECT e.name
                FROM media_expected e
                LEFT JOIN media_actual a ON a.name = e.name
                WHERE a.name IS NULL OR e.size != a.size OR e.checksum != a.checksum
                UNION ALL
                SELECT a.name
                FROM media_actual a
                LEFT JOIN media_expected e ON e.name = a.name
                WHERE e.name IS NULL
                LIMIT 1
                """
            ).fetchone()
            if mismatch is not None:
                raise ArchiveValidationError(f"media archive checksum or membership mismatch: {mismatch[0]}")
            checksum_valid, signature_valid = _validate_manifest(
                manifest,
                counts=counts,
                integrity=integrity,
                signing_key=signing_key,
                require_signature=require_signature,
            )
            if extract_root is not None:
                _fsync_directory(extract_root)
            return ArchiveValidationReport(
                valid=True,
                manifest=manifest,
                counts=counts,
                members=integrity.members,
                uncompressed_bytes=integrity.uncompressed_bytes,
                checksum_valid=checksum_valid,
                signature_valid=signature_valid,
                extracted_root=extract_root,
            )
        except Exception:
            if extract_root is not None:
                shutil.rmtree(extract_root, ignore_errors=True)
            raise
        finally:
            index.close()
