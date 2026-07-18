import asyncio
import base64
import contextlib
import datetime as dt
import gzip
import hashlib
import hmac
import json
import logging
import re
import secrets
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.import_contract import (
    IMPORT_BATCH_MODES,
    IMPORT_BATCH_STATUSES,
    IMPORT_SOURCE_STATUSES,
    SQL_INTEGER_MAX,
    SQL_INTEGER_MIN,
)
from app.models import (
    ImportBatch,
    ImportSource,
    MediaAsset,
    Message,
    MessageMediaReference,
    MessageSourceRecord,
    RobotMessage,
    RoomProfile,
    UserProfile,
)
from app.schemas import MessageMediaReferenceRequest
from app.services.import_service import ImportService
from app.time_utils import format_utc_z, parse_utc_datetime, to_utc_naive, utc_now

BACKUP_SCHEMA = "chat-audit-core.backup.v2"
LEGACY_BACKUP_SCHEMAS = frozenset({"chat-audit-core.backup.v1"})
SUPPORTED_BACKUP_SCHEMAS = frozenset({BACKUP_SCHEMA, *LEGACY_BACKUP_SCHEMAS})
BACKUP_SIGNATURE_ALGORITHM = "hmac-sha256"
logger = logging.getLogger(__name__)


class BackupService:
    _REQUIRED_FIELDS = {
        "messages": ("msg_hash", "platform", "room_id", "message_type", "sender_id", "raw_message", "local_message", "timestamp"),
        "robot_messages": ("robot_id", "msg_hash"),
        "media_assets": ("file_hash", "file_type", "file_size", "local_path"),
        "media_files": ("local_path", "file_size", "file_checksum", "content_base64"),
        "room_profiles": ("room_id", "platform"),
        "user_profiles": ("user_id", "platform"),
    }
    _V2_REQUIRED_FIELDS = {
        "import_sources": ("id", "source_type", "platform", "account_id", "device_id", "status", "first_seen_at", "last_seen_at"),
        "import_batches": ("id", "source_id", "mode", "status", "started_at"),
        "message_source_records": ("msg_hash", "source_id", "source_table", "source_primary_key", "imported_at", "last_seen_at"),
        "message_media_references": ("msg_hash", "ordinal", "media_type", "source_state", "archive_state", "first_seen_at", "last_checked_at"),
    }
    _REQUIRED_FIELD_TYPES = {
        "messages": {
            "msg_hash": str,
            "platform": str,
            "room_id": str,
            "message_type": str,
            "sender_id": str,
            "raw_message": str,
            "local_message": str,
            "timestamp": int,
        },
        "robot_messages": {"robot_id": str, "msg_hash": str},
        "media_assets": {"file_hash": str, "file_type": str, "file_size": int, "local_path": str},
        "media_files": {"local_path": str, "file_size": int, "file_checksum": dict, "content_base64": str},
        "room_profiles": {"room_id": str, "platform": str},
        "user_profiles": {"user_id": str, "platform": str},
        "import_sources": {
            "id": str,
            "source_type": str,
            "platform": str,
            "account_id": str,
            "device_id": str,
            "status": str,
            "first_seen_at": str,
            "last_seen_at": str,
        },
        "import_batches": {"id": str, "source_id": str, "mode": str, "status": str, "started_at": str},
        "message_source_records": {
            "msg_hash": str,
            "source_id": str,
            "source_table": str,
            "source_primary_key": str,
            "imported_at": str,
            "last_seen_at": str,
        },
        "message_media_references": {
            "msg_hash": str,
            "ordinal": int,
            "media_type": str,
            "source_state": str,
            "archive_state": str,
            "first_seen_at": str,
            "last_checked_at": str,
        },
    }
    _OPTIONAL_FIELD_TYPES = {
        "messages": {"external_message_id": str, "nickname": str},
        "media_assets": {"file_checksum": dict},
        "room_profiles": {"display_name": str, "avatar_path": str},
        "user_profiles": {"display_name": str, "avatar_path": str},
        "import_sources": {
            "device_name": str,
            "qq_version": str,
            "schema_version": str,
            "metadata": dict,
        },
        "import_batches": {
            "completed_at": str,
            "scanned_messages": int,
            "inserted_messages": int,
            "updated_messages": int,
            "skipped_messages": int,
            "uploaded_media": int,
            "not_downloaded_media": int,
            "missing_media": int,
            "failed_media": int,
            "detail": dict,
        },
        "message_source_records": {
            "batch_id": str,
            "source_external_message_id": str,
            "platform_message_id": str,
            "schema_version": str,
            "raw_columns": dict,
            "raw_40800_protobuf": str,
            "raw_40900_protobuf": str,
            "metadata": dict,
        },
        "message_media_references": {"archived_at": str},
    }
    _DATETIME_FIELDS = {
        "import_sources": ("first_seen_at", "last_seen_at"),
        "import_batches": ("started_at", "completed_at"),
        "message_source_records": ("imported_at", "last_seen_at"),
        "message_media_references": ("first_seen_at", "last_checked_at", "archived_at"),
    }
    _INTEGER_FIELDS = {
        "messages": ("timestamp",),
        "media_assets": ("file_size",),
        "media_files": ("file_size",),
        "import_batches": (
            "scanned_messages",
            "inserted_messages",
            "updated_messages",
            "skipped_messages",
            "uploaded_media",
            "not_downloaded_media",
            "missing_media",
            "failed_media",
        ),
        "message_media_references": (
            "ordinal",
            "declared_file_size",
            "actual_file_size",
            "duration_ms",
            "width",
            "height",
        ),
    }

    @staticmethod
    def _section_list(package: dict[str, Any], section: str) -> list[Any]:
        value = package.get(section, [])
        return value if isinstance(value, list) else []

    @staticmethod
    def _field_type_matches(value: Any, expected_type: type) -> bool:
        if expected_type is int:
            return isinstance(value, int) and not isinstance(value, bool)
        return isinstance(value, expected_type)

    @staticmethod
    def calculate_package_checksum(package: dict[str, Any]) -> str:
        canonical_package = json.loads(json.dumps(package, ensure_ascii=False))
        manifest = canonical_package.get("manifest")
        if not isinstance(manifest, dict):
            raise ValueError("manifest must be an object")
        manifest.pop("checksum", None)
        manifest.pop("signature", None)
        payload = json.dumps(canonical_package, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def calculate_package_signature(package: dict[str, Any], signing_key: str) -> str:
        canonical_package = json.loads(json.dumps(package, ensure_ascii=False))
        manifest = canonical_package.get("manifest")
        if not isinstance(manifest, dict):
            raise ValueError("manifest must be an object")
        manifest.pop("checksum", None)
        manifest.pop("signature", None)
        payload = json.dumps(canonical_package, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hmac.new(signing_key.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    @staticmethod
    def attach_package_checksum(package: dict[str, Any]) -> dict[str, Any]:
        manifest = package.setdefault("manifest", {})
        manifest.pop("checksum", None)
        manifest["checksum"] = {
            "algorithm": "sha256",
            "value": BackupService.calculate_package_checksum(package),
        }
        return package

    @staticmethod
    def attach_package_signature(package: dict[str, Any], *, system_id: str, signing_key: str) -> dict[str, Any]:
        manifest = package.setdefault("manifest", {})
        manifest["source"] = {
            "system": "chat-audit-core",
            "instance_id": system_id,
        }
        manifest.pop("signature", None)
        manifest["signature"] = {
            "algorithm": BACKUP_SIGNATURE_ALGORITHM,
            "key_id": system_id,
            "value": BackupService.calculate_package_signature(package, signing_key),
        }
        return package

    @staticmethod
    def validate_package_checksum(package: dict[str, Any]) -> None:
        manifest = package.get("manifest")
        if not isinstance(manifest, dict):
            raise ValueError("manifest must be an object")
        checksum = manifest.get("checksum")
        if checksum is None:
            return
        if not isinstance(checksum, dict):
            raise ValueError("backup package checksum must be an object")
        if checksum.get("algorithm") != "sha256":
            raise ValueError(f"unsupported checksum algorithm: {checksum.get('algorithm')!r}")
        expected = checksum.get("value")
        if not isinstance(expected, str):
            raise ValueError("backup package checksum value must be a string")
        actual = BackupService.calculate_package_checksum(package)
        if expected != actual:
            raise ValueError("backup package checksum mismatch")

    @staticmethod
    def validate_package_signature(package: dict[str, Any], signing_key: str) -> None:
        manifest = package.get("manifest")
        if not isinstance(manifest, dict):
            raise ValueError("manifest must be an object")
        signature = manifest.get("signature")
        if signature is None:
            raise ValueError("backup package signature missing")
        if not isinstance(signature, dict):
            raise ValueError("backup package signature must be an object")
        if signature.get("algorithm") != BACKUP_SIGNATURE_ALGORITHM:
            raise ValueError(f"unsupported signature algorithm: {signature.get('algorithm')!r}")
        expected = signature.get("value")
        if not isinstance(expected, str):
            raise ValueError("backup package signature value must be a string")
        actual = BackupService.calculate_package_signature(package, signing_key)
        if not hmac.compare_digest(str(expected or ""), actual):
            raise ValueError("backup package signature mismatch")

    @staticmethod
    def _message_to_dict(message: Message) -> dict[str, Any]:
        return {
            "msg_hash": message.msg_hash,
            "platform": message.platform,
            "room_id": message.room_id,
            "message_type": message.message_type,
            "external_message_id": message.external_message_id,
            "sender_id": message.sender_id,
            "nickname": message.nickname,
            "raw_message": message.raw_message,
            "local_message": message.local_message,
            "timestamp": message.timestamp,
        }

    @staticmethod
    def _media_asset_to_dict(asset: MediaAsset, storage_root: Path | None = None, public_storage_prefix: str = "/static/storage") -> dict[str, Any]:
        item = {
            "file_hash": asset.file_hash,
            "file_type": asset.file_type,
            "file_size": asset.file_size,
            "local_path": asset.local_path,
        }
        if storage_root is not None:
            file_path = BackupService._local_media_file_path(asset.local_path, storage_root, public_storage_prefix)
            if file_path is not None and file_path.exists():
                item["file_checksum"] = {
                    "algorithm": "sha256",
                    "value": hashlib.sha256(file_path.read_bytes()).hexdigest(),
                }
        return item

    @staticmethod
    def _json_object(value: str | None) -> dict[str, Any]:
        if not value:
            return {}
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _import_source_to_dict(source: ImportSource) -> dict[str, Any]:
        return {
            "id": source.id,
            "source_type": source.source_type,
            "platform": source.platform,
            "account_id": source.account_id,
            "device_id": source.device_id,
            "device_name": source.device_name,
            "qq_version": source.qq_version,
            "schema_version": source.schema_version,
            "status": source.status,
            "first_seen_at": format_utc_z(source.first_seen_at),
            "last_seen_at": format_utc_z(source.last_seen_at),
            "metadata": BackupService._json_object(source.metadata_json),
        }

    @staticmethod
    def _import_batch_to_dict(batch: ImportBatch) -> dict[str, Any]:
        return {
            "id": batch.id,
            "source_id": batch.source_id,
            "mode": batch.mode,
            "status": batch.status,
            "started_at": format_utc_z(batch.started_at),
            "completed_at": format_utc_z(batch.completed_at) if batch.completed_at else None,
            "scanned_messages": batch.scanned_messages,
            "inserted_messages": batch.inserted_messages,
            "updated_messages": batch.updated_messages,
            "skipped_messages": batch.skipped_messages,
            "uploaded_media": batch.uploaded_media,
            "not_downloaded_media": batch.not_downloaded_media,
            "missing_media": batch.missing_media,
            "failed_media": batch.failed_media,
            "detail": BackupService._json_object(batch.detail_json),
        }

    @staticmethod
    def _message_source_record_to_dict(record: MessageSourceRecord) -> dict[str, Any]:
        return {
            "msg_hash": record.msg_hash,
            "source_id": record.source_id,
            "batch_id": record.batch_id,
            "source_table": record.source_table,
            "source_primary_key": record.source_primary_key,
            "source_external_message_id": record.source_external_message_id,
            "platform_message_id": record.platform_message_id,
            "schema_version": record.schema_version,
            "raw_columns": BackupService._json_object(record.raw_columns_json),
            "raw_40800_protobuf": record.raw_40800_protobuf,
            "raw_40900_protobuf": record.raw_40900_protobuf,
            "metadata": BackupService._json_object(record.metadata_json),
            "imported_at": format_utc_z(record.imported_at),
            "last_seen_at": format_utc_z(record.last_seen_at),
        }

    @staticmethod
    def _message_media_reference_to_dict(reference: MessageMediaReference) -> dict[str, Any]:
        return {
            "msg_hash": reference.msg_hash,
            "ordinal": reference.ordinal,
            "media_type": reference.media_type,
            "source_state": reference.source_state,
            "archive_state": reference.archive_state,
            "asset_file_hash": reference.asset_file_hash,
            "thumbnail_file_hash": reference.thumbnail_file_hash,
            "file_name": reference.file_name,
            "file_ext": reference.file_ext,
            "declared_file_size": reference.declared_file_size,
            "actual_file_size": reference.actual_file_size,
            "source_md5": reference.source_md5,
            "source_sha1": reference.source_sha1,
            "source_uuid": reference.source_uuid,
            "source_local_path": reference.source_local_path,
            "duration_ms": reference.duration_ms,
            "width": reference.width,
            "height": reference.height,
            "failure_code": reference.failure_code,
            "failure_detail": reference.failure_detail,
            "metadata": BackupService._json_object(reference.metadata_json),
            "first_seen_at": format_utc_z(reference.first_seen_at),
            "last_checked_at": format_utc_z(reference.last_checked_at),
            "archived_at": format_utc_z(reference.archived_at) if reference.archived_at else None,
        }

    @staticmethod
    def _room_profile_to_dict(profile: RoomProfile) -> dict[str, Any]:
        return {
            "room_id": profile.room_id,
            "platform": profile.platform,
            "display_name": profile.display_name,
            "avatar_path": profile.avatar_path,
        }

    @staticmethod
    def _user_profile_to_dict(profile: UserProfile) -> dict[str, Any]:
        return {
            "user_id": profile.user_id,
            "platform": profile.platform,
            "display_name": profile.display_name,
            "avatar_path": profile.avatar_path,
        }

    @staticmethod
    def _extract_local_media_paths(local_message: str, public_storage_prefix: str = "/static/storage") -> set[str]:
        prefix = public_storage_prefix.rstrip("/") + "/"
        pattern = re.compile(rf"{re.escape(prefix)}[^\s\"'<>),\]]+?\.[a-z0-9]+", re.I)
        return set(pattern.findall(local_message))

    @staticmethod
    def _media_file_to_dict(
        asset: MediaAsset,
        storage_root: Path,
        public_storage_prefix: str,
        max_media_bytes: int | None = None,
    ) -> dict[str, Any] | None:
        file_path = BackupService._local_media_file_path(asset.local_path, storage_root, public_storage_prefix)
        if file_path is None or not file_path.exists():
            return None
        if max_media_bytes is not None and file_path.stat().st_size > max_media_bytes:
            return None

        content = file_path.read_bytes()
        return {
            "local_path": asset.local_path,
            "file_size": len(content),
            "file_checksum": {
                "algorithm": "sha256",
                "value": hashlib.sha256(content).hexdigest(),
            },
            "content_base64": base64.b64encode(content).decode("ascii"),
        }

    @staticmethod
    async def export_package(
        db: AsyncSession,
        robot_id: str | None = None,
        room_id: str | None = None,
        start_timestamp: int | None = None,
        end_timestamp: int | None = None,
        storage_root: Path | None = None,
        public_storage_prefix: str = "/static/storage",
        max_media_bytes: int | None = None,
        system_id: str | None = None,
        signing_key: str | None = None,
    ) -> dict[str, Any]:
        stmt = select(Message)
        if robot_id is not None:
            stmt = stmt.join(RobotMessage, RobotMessage.msg_hash == Message.msg_hash).where(RobotMessage.robot_id == robot_id)
        if room_id is not None:
            stmt = stmt.where(Message.room_id == room_id)
        if start_timestamp is not None:
            stmt = stmt.where(Message.timestamp >= start_timestamp)
        if end_timestamp is not None:
            stmt = stmt.where(Message.timestamp <= end_timestamp)
        stmt = stmt.order_by(Message.timestamp.asc(), Message.msg_hash.asc())

        result = await db.execute(stmt)
        messages = list(result.scalars().unique().all())
        msg_hashes = [message.msg_hash for message in messages]
        room_ids = sorted({message.room_id for message in messages if message.message_type == "group"})
        user_ids = sorted(
            {message.sender_id for message in messages}
            | {message.room_id for message in messages if message.message_type == "private"}
        )

        robot_messages: list[dict[str, str]] = []
        if msg_hashes:
            assoc_stmt = select(RobotMessage).where(RobotMessage.msg_hash.in_(msg_hashes))
            if robot_id is not None:
                assoc_stmt = assoc_stmt.where(RobotMessage.robot_id == robot_id)
            assoc_result = await db.execute(assoc_stmt.order_by(RobotMessage.robot_id.asc(), RobotMessage.msg_hash.asc()))
            robot_messages = [
                {"robot_id": assoc.robot_id, "msg_hash": assoc.msg_hash}
                for assoc in assoc_result.scalars().all()
            ]

        message_source_records: list[dict[str, Any]] = []
        message_media_references: list[dict[str, Any]] = []
        import_sources: list[dict[str, Any]] = []
        import_batches: list[dict[str, Any]] = []
        referenced_media_hashes: set[str] = set()
        if msg_hashes:
            source_record_result = await db.execute(
                select(MessageSourceRecord)
                .where(MessageSourceRecord.msg_hash.in_(msg_hashes))
                .order_by(MessageSourceRecord.msg_hash.asc(), MessageSourceRecord.source_id.asc(), MessageSourceRecord.id.asc())
            )
            source_record_models = list(source_record_result.scalars().all())
            message_source_records = [BackupService._message_source_record_to_dict(record) for record in source_record_models]
            source_ids = sorted({record.source_id for record in source_record_models})
            batch_ids = sorted({record.batch_id for record in source_record_models if record.batch_id})
            if source_ids:
                source_result = await db.execute(select(ImportSource).where(ImportSource.id.in_(source_ids)).order_by(ImportSource.id.asc()))
                import_sources = [BackupService._import_source_to_dict(source) for source in source_result.scalars().all()]
            if batch_ids:
                batch_result = await db.execute(select(ImportBatch).where(ImportBatch.id.in_(batch_ids)).order_by(ImportBatch.started_at.asc(), ImportBatch.id.asc()))
                import_batches = [BackupService._import_batch_to_dict(batch) for batch in batch_result.scalars().all()]

            reference_result = await db.execute(
                select(MessageMediaReference)
                .where(MessageMediaReference.msg_hash.in_(msg_hashes))
                .order_by(MessageMediaReference.msg_hash.asc(), MessageMediaReference.ordinal.asc())
            )
            reference_models = list(reference_result.scalars().all())
            message_media_references = [BackupService._message_media_reference_to_dict(reference) for reference in reference_models]
            referenced_media_hashes = {
                file_hash
                for reference in reference_models
                for file_hash in (reference.asset_file_hash, reference.thumbnail_file_hash)
                if file_hash
            }

        room_profiles: list[dict[str, Any]] = []
        user_profiles: list[dict[str, Any]] = []
        profile_media_paths: set[str] = set()
        if room_ids:
            room_profile_result = await db.execute(select(RoomProfile).where(RoomProfile.room_id.in_(room_ids)).order_by(RoomProfile.room_id.asc()))
            room_profile_models = list(room_profile_result.scalars().all())
            room_profiles = [BackupService._room_profile_to_dict(profile) for profile in room_profile_models]
            profile_media_paths.update(profile.avatar_path for profile in room_profile_models if profile.avatar_path)
        if user_ids:
            user_profile_result = await db.execute(select(UserProfile).where(UserProfile.user_id.in_(user_ids)).order_by(UserProfile.user_id.asc()))
            user_profile_models = list(user_profile_result.scalars().all())
            user_profiles = [BackupService._user_profile_to_dict(profile) for profile in user_profile_models]
            profile_media_paths.update(profile.avatar_path for profile in user_profile_models if profile.avatar_path)

        media_paths = sorted(
            profile_media_paths
            | {
                local_path
                for message in messages
                if isinstance(message.local_message, str)
                for local_path in BackupService._extract_local_media_paths(message.local_message, public_storage_prefix)
            }
        )
        media_assets: list[dict[str, Any]] = []
        media_files: list[dict[str, Any]] = []
        if media_paths or referenced_media_hashes:
            media_conditions = []
            if media_paths:
                media_conditions.append(MediaAsset.local_path.in_(media_paths))
            if referenced_media_hashes:
                media_conditions.append(MediaAsset.file_hash.in_(referenced_media_hashes))
            media_result = await db.execute(select(MediaAsset).where(or_(*media_conditions)).order_by(MediaAsset.file_hash.asc()))
            assets = list(media_result.scalars().all())
            media_assets = [
                BackupService._media_asset_to_dict(asset, storage_root=storage_root, public_storage_prefix=public_storage_prefix)
                for asset in assets
            ]
            if storage_root is not None:
                media_files = [
                    file_item
                    for asset in assets
                    if (file_item := BackupService._media_file_to_dict(asset, storage_root, public_storage_prefix, max_media_bytes)) is not None
                ]

        package = {
            "manifest": {
                "schema": BACKUP_SCHEMA,
                "created_at": format_utc_z(utc_now()),
                "filters": {
                    "robot_id": robot_id,
                    "room_id": room_id,
                    "start_timestamp": start_timestamp,
                    "end_timestamp": end_timestamp,
                },
                "counts": {
                    "messages": len(messages),
                    "robot_messages": len(robot_messages),
                    "media_assets": len(media_assets),
                    "media_files": len(media_files),
                    "room_profiles": len(room_profiles),
                    "user_profiles": len(user_profiles),
                    "import_sources": len(import_sources),
                    "import_batches": len(import_batches),
                    "message_source_records": len(message_source_records),
                    "message_media_references": len(message_media_references),
                },
            },
            "messages": [BackupService._message_to_dict(message) for message in messages],
            "robot_messages": robot_messages,
            "media_assets": media_assets,
            "media_files": media_files,
            "room_profiles": room_profiles,
            "user_profiles": user_profiles,
            "import_sources": import_sources,
            "import_batches": import_batches,
            "message_source_records": message_source_records,
            "message_media_references": message_media_references,
        }
        BackupService.attach_package_checksum(package)
        if system_id and signing_key:
            BackupService.attach_package_signature(package, system_id=system_id, signing_key=signing_key)
            BackupService.attach_package_checksum(package)
        return package

    @staticmethod
    def package_to_json_bytes(package: dict[str, Any]) -> bytes:
        return json.dumps(package, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def compress_package(package: dict[str, Any], compresslevel: int = 6) -> bytes:
        return gzip.compress(BackupService.package_to_json_bytes(package), compresslevel=compresslevel)

    @staticmethod
    def decode_package_bytes(data: bytes) -> dict[str, Any]:
        payload = gzip.decompress(data) if data.startswith(b"\x1f\x8b") else data
        package = json.loads(payload.decode("utf-8"))
        if not isinstance(package, dict):
            raise ValueError("backup package must be a JSON object")
        return package

    @staticmethod
    async def export_package_compressed(
        db: AsyncSession,
        robot_id: str | None = None,
        room_id: str | None = None,
        start_timestamp: int | None = None,
        end_timestamp: int | None = None,
        storage_root: Path | None = None,
        public_storage_prefix: str = "/static/storage",
        max_media_bytes: int | None = None,
        system_id: str | None = None,
        signing_key: str | None = None,
    ) -> bytes:
        package = await BackupService.export_package(
            db,
            robot_id=robot_id,
            room_id=room_id,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            storage_root=storage_root,
            public_storage_prefix=public_storage_prefix,
            max_media_bytes=max_media_bytes,
            system_id=system_id,
            signing_key=signing_key,
        )
        return BackupService.compress_package(package)

    @staticmethod
    async def write_auto_backup_file(
        db: AsyncSession,
        backup_root: Path,
        storage_root: Path | None = None,
        public_storage_prefix: str = "/static/storage",
        max_media_bytes: int | None = None,
        keep_latest: int = 7,
        system_id: str | None = None,
        signing_key: str | None = None,
    ) -> Path:
        backup_root.mkdir(parents=True, exist_ok=True)
        package = await BackupService.export_package(
            db,
            storage_root=storage_root,
            public_storage_prefix=public_storage_prefix,
            max_media_bytes=max_media_bytes,
            system_id=system_id,
            signing_key=signing_key,
        )
        package["manifest"]["backup_type"] = "auto"
        package["manifest"]["created_by"] = "auto_backup_scheduler"
        BackupService.attach_package_checksum(package)
        if system_id and signing_key:
            BackupService.attach_package_signature(package, system_id=system_id, signing_key=signing_key)
            BackupService.attach_package_checksum(package)

        timestamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
        backup_path = backup_root / f"auto-backup-{timestamp}-{secrets.token_hex(4)}.json"
        while backup_path.exists():
            backup_path = backup_root / f"auto-backup-{timestamp}-{secrets.token_hex(4)}.json"

        backup_path.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")

        if keep_latest > 0:
            backups = sorted(backup_root.glob("auto-backup-*.json"), key=lambda path: (path.stat().st_mtime, path.name))
            for old_path in backups[:-keep_latest]:
                with contextlib.suppress(FileNotFoundError):
                    old_path.unlink()

        return backup_path

    @staticmethod
    def next_run_from_cron(cron_expr: str, now: dt.datetime | None = None) -> dt.datetime:
        now = to_utc_naive(now or utc_now()).replace(microsecond=0)
        parts = cron_expr.split()
        if len(parts) != 5:
            raise ValueError(f"unsupported cron expression: {cron_expr!r}")
        minute_raw, hour_raw, day_raw, month_raw, weekday_raw = parts
        if (day_raw, month_raw, weekday_raw) != ("*", "*", "*"):
            raise ValueError(f"only daily cron is supported: {cron_expr!r}")
        if not minute_raw.isdigit() or not hour_raw.isdigit():
            raise ValueError(f"only fixed hour/minute cron is supported: {cron_expr!r}")
        minute = int(minute_raw)
        hour = int(hour_raw)
        if not (0 <= minute <= 59 and 0 <= hour <= 23):
            raise ValueError(f"invalid cron time: {cron_expr!r}")

        candidate = now.replace(hour=hour, minute=minute, second=0)
        if candidate <= now:
            candidate += dt.timedelta(days=1)
        return candidate

    @staticmethod
    def _local_media_file_path(local_path: Any, storage_root: Path, public_storage_prefix: str) -> Path | None:
        if not isinstance(local_path, str) or not local_path:
            return None
        prefix = public_storage_prefix.rstrip("/") + "/"
        if not local_path.startswith(prefix):
            return None
        relative = local_path[len(prefix):].lstrip("/")
        candidate = (storage_root / relative).resolve()
        storage_root_resolved = storage_root.resolve()
        if storage_root_resolved != candidate and storage_root_resolved not in candidate.parents:
            return None
        return candidate

    @staticmethod
    def _decode_embedded_media_file(media_file: dict[str, Any]) -> bytes:
        content_base64 = media_file.get("content_base64")
        if not isinstance(content_base64, str):
            raise ValueError(f"invalid embedded media content: {media_file.get('local_path')}")
        try:
            content = base64.b64decode(content_base64, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"invalid embedded media content: {media_file.get('local_path')}") from exc

        expected_size = media_file.get("file_size")
        if not isinstance(expected_size, int) or isinstance(expected_size, bool) or expected_size < 0:
            raise ValueError(f"invalid embedded media size: {media_file.get('local_path')}")
        if expected_size is not None and len(content) != expected_size:
            raise ValueError(f"embedded media size mismatch: {media_file.get('local_path')}")

        checksum = media_file.get("file_checksum") or {}
        if not isinstance(checksum, dict):
            raise ValueError(f"invalid embedded media checksum: {media_file.get('local_path')}")
        if checksum.get("algorithm") != "sha256":
            raise ValueError(f"unsupported embedded media checksum algorithm: {checksum.get('algorithm')!r}")
        if not isinstance(checksum.get("value"), str):
            raise ValueError(f"invalid embedded media checksum value: {media_file.get('local_path')}")
        actual = hashlib.sha256(content).hexdigest()
        if actual != checksum.get("value"):
            raise ValueError(f"embedded media checksum mismatch: {media_file.get('local_path')}")

        return content

    @staticmethod
    def _embedded_media_files_by_path(package: dict[str, Any], errors: list[str] | None = None) -> dict[str, dict[str, Any]]:
        embedded: dict[str, dict[str, Any]] = {}
        for media_file in BackupService._section_list(package, "media_files"):
            if not isinstance(media_file, dict):
                continue
            local_path = media_file.get("local_path")
            if not isinstance(local_path, str) or not local_path:
                if errors is not None:
                    errors.append("embedded media file has an invalid local_path")
                continue
            try:
                BackupService._decode_embedded_media_file(media_file)
            except ValueError as exc:
                if errors is not None:
                    errors.append(str(exc))
                continue
            embedded[local_path] = media_file
        return embedded

    @staticmethod
    def _validate_required_package_fields(package: dict[str, Any], errors: list[str], *, schema: str | None = None) -> None:
        required_sections = dict(BackupService._REQUIRED_FIELDS)
        if schema == BACKUP_SCHEMA:
            required_sections.update(BackupService._V2_REQUIRED_FIELDS)
        for section, required_fields in required_sections.items():
            if schema == BACKUP_SCHEMA and section in BackupService._V2_REQUIRED_FIELDS and section not in package:
                errors.append(f"{section} section is required for {BACKUP_SCHEMA}")
                continue
            items = package.get(section, [])
            if not isinstance(items, list):
                errors.append(f"{section} must be a list")
                continue

            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    errors.append(f"{section}[{index}] must be an object")
                    continue
                missing = [field for field in required_fields if field not in item]
                if missing:
                    errors.append(f"{section}[{index}] missing required field(s): {', '.join(missing)}")
                required_types = BackupService._REQUIRED_FIELD_TYPES.get(section, {})
                optional_types = BackupService._OPTIONAL_FIELD_TYPES.get(section, {})
                for field, expected_type in required_types.items():
                    if field in item and not BackupService._field_type_matches(item[field], expected_type):
                        errors.append(f"{section}[{index}].{field} must be a {expected_type.__name__}")
                for field, expected_type in optional_types.items():
                    value = item.get(field)
                    if value is not None and not BackupService._field_type_matches(value, expected_type):
                        errors.append(f"{section}[{index}].{field} must be a {expected_type.__name__} or null")
                for field in BackupService._DATETIME_FIELDS.get(section, ()):
                    value = item.get(field)
                    if value is None:
                        continue
                    if not isinstance(value, str):
                        continue
                    try:
                        parse_utc_datetime(value)
                    except (TypeError, ValueError):
                        errors.append(f"{section}[{index}].{field} must be an ISO-8601 datetime")
                for field in BackupService._INTEGER_FIELDS.get(section, ()):
                    value = item.get(field)
                    if value is None or not isinstance(value, int) or isinstance(value, bool):
                        continue
                    if not SQL_INTEGER_MIN <= value <= SQL_INTEGER_MAX:
                        errors.append(
                            f"{section}[{index}].{field} must fit a signed 32-bit SQL integer"
                        )

                if section == "import_sources":
                    status = item.get("status")
                    if not isinstance(status, str) or status not in IMPORT_SOURCE_STATUSES:
                        errors.append(f"import_sources[{index}].status is not supported")
                if section == "import_batches":
                    mode = item.get("mode")
                    status = item.get("status")
                    if not isinstance(mode, str) or mode not in IMPORT_BATCH_MODES:
                        errors.append(f"import_batches[{index}].mode is not supported")
                    if not isinstance(status, str) or status not in IMPORT_BATCH_STATUSES:
                        errors.append(f"import_batches[{index}].status is not supported")
                    for field in (
                        "scanned_messages",
                        "inserted_messages",
                        "updated_messages",
                        "skipped_messages",
                        "uploaded_media",
                        "not_downloaded_media",
                        "missing_media",
                        "failed_media",
                    ):
                        value = item.get(field)
                        if isinstance(value, int) and not isinstance(value, bool) and value < 0:
                            errors.append(f"import_batches[{index}].{field} must be non-negative")
                if section in {"media_assets", "media_files"}:
                    file_size = item.get("file_size")
                    if isinstance(file_size, int) and not isinstance(file_size, bool) and file_size < 0:
                        errors.append(f"{section}[{index}].file_size must be non-negative")

    @staticmethod
    def _validate_v2_references(package: dict[str, Any], errors: list[str]) -> None:
        messages = [item for item in BackupService._section_list(package, "messages") if isinstance(item, dict)]
        sources = [item for item in BackupService._section_list(package, "import_sources") if isinstance(item, dict)]
        batches = [item for item in BackupService._section_list(package, "import_batches") if isinstance(item, dict)]
        assets = [item for item in BackupService._section_list(package, "media_assets") if isinstance(item, dict)]
        source_records = [item for item in BackupService._section_list(package, "message_source_records") if isinstance(item, dict)]
        media_references = [item for item in BackupService._section_list(package, "message_media_references") if isinstance(item, dict)]
        message_hashes = {item.get("msg_hash") for item in messages if isinstance(item.get("msg_hash"), str)}
        source_ids = {item.get("id") for item in sources if isinstance(item.get("id"), str)}
        batch_ids = {item.get("id") for item in batches if isinstance(item.get("id"), str)}
        asset_by_hash = {
            item.get("file_hash"): item
            for item in assets
            if isinstance(item.get("file_hash"), str)
        }
        asset_hashes = set(asset_by_hash)
        for index, batch in enumerate(batches):
            source_id = batch.get("source_id")
            if not isinstance(source_id, str) or source_id not in source_ids:
                errors.append(f"import_batches[{index}] references an unknown import source")
        for index, record in enumerate(source_records):
            msg_hash = record.get("msg_hash")
            source_id = record.get("source_id")
            batch_id = record.get("batch_id")
            if not isinstance(msg_hash, str) or msg_hash not in message_hashes:
                errors.append(f"message_source_records[{index}] references an unknown message")
            if not isinstance(source_id, str) or source_id not in source_ids:
                errors.append(f"message_source_records[{index}] references an unknown import source")
            if batch_id is not None and (not isinstance(batch_id, str) or batch_id not in batch_ids):
                errors.append(f"message_source_records[{index}] references an unknown import batch")
        for index, reference in enumerate(media_references):
            try:
                MessageMediaReferenceRequest.model_validate(reference)
            except ValueError as exc:
                errors.append(f"message_media_references[{index}] violates the media state contract: {exc}")
            msg_hash = reference.get("msg_hash")
            if not isinstance(msg_hash, str) or msg_hash not in message_hashes:
                errors.append(f"message_media_references[{index}] references an unknown message")
            for field in ("asset_file_hash", "thumbnail_file_hash"):
                file_hash = reference.get(field)
                if file_hash is not None and not isinstance(file_hash, str):
                    errors.append(f"message_media_references[{index}].{field} must be a str or null")
                    continue
                if file_hash and file_hash not in asset_hashes:
                    errors.append(f"message_media_references[{index}].{field} references an unknown media asset")
                elif file_hash:
                    try:
                        asset_size = int(asset_by_hash[file_hash].get("file_size") or 0)
                    except (TypeError, ValueError):
                        errors.append(f"media asset {file_hash} has an invalid file_size")
                    else:
                        if asset_size <= 0:
                            errors.append(f"message_media_references[{index}].{field} references an empty media asset")

    @staticmethod
    def _validate_core_references(package: dict[str, Any], errors: list[str]) -> None:
        message_hashes = {
            item.get("msg_hash")
            for item in BackupService._section_list(package, "messages")
            if isinstance(item, dict) and isinstance(item.get("msg_hash"), str)
        }
        for index, robot_message in enumerate(BackupService._section_list(package, "robot_messages")):
            if not isinstance(robot_message, dict):
                continue
            msg_hash = robot_message.get("msg_hash")
            if not isinstance(msg_hash, str) or msg_hash not in message_hashes:
                errors.append(f"robot_messages[{index}] references an unknown message")

    @staticmethod
    def _validate_media_files(
        package: dict[str, Any],
        storage_root: Path | None,
        public_storage_prefix: str,
        errors: list[str],
    ) -> dict[str, int]:
        media_files = {"checked": 0, "missing": 0, "mismatch": 0}
        embedded = BackupService._embedded_media_files_by_path(package, errors)
        if storage_root is None:
            return media_files

        for asset in BackupService._section_list(package, "media_assets"):
            if not isinstance(asset, dict):
                continue
            checksum = asset.get("file_checksum")
            if checksum is None:
                continue
            media_files["checked"] += 1
            if not isinstance(checksum, dict):
                media_files["mismatch"] += 1
                errors.append(f"invalid media checksum for {asset.get('local_path')}")
                continue
            if checksum.get("algorithm") != "sha256":
                media_files["mismatch"] += 1
                errors.append(f"unsupported media checksum algorithm for {asset.get('local_path')}: {checksum.get('algorithm')!r}")
                continue
            if not isinstance(checksum.get("value"), str):
                media_files["mismatch"] += 1
                errors.append(f"invalid media checksum value for {asset.get('local_path')}")
                continue

            local_path_value = asset.get("local_path")
            local_path = local_path_value if isinstance(local_path_value, str) else ""
            file_path = BackupService._local_media_file_path(local_path, storage_root, public_storage_prefix)
            if file_path is None or not file_path.exists():
                if local_path not in embedded:
                    media_files["missing"] += 1
                    errors.append(f"media file missing: {local_path}")
                continue

            mismatch = False
            expected_size = asset.get("file_size")
            if expected_size is not None and file_path.stat().st_size != expected_size:
                mismatch = True
            actual = hashlib.sha256(file_path.read_bytes()).hexdigest()
            if actual != checksum.get("value"):
                mismatch = True
            if mismatch:
                if local_path not in embedded:
                    media_files["mismatch"] += 1
                    errors.append(f"media checksum mismatch: {local_path}")

        return media_files

    @staticmethod
    def _restore_embedded_media_files(
        package: dict[str, Any],
        storage_root: Path | None,
        public_storage_prefix: str,
    ) -> int:
        if storage_root is None:
            return 0

        restored = 0
        for media_file in package.get("media_files", []) or []:
            local_path = media_file.get("local_path") or ""
            file_path = BackupService._local_media_file_path(local_path, storage_root, public_storage_prefix)
            if file_path is None:
                raise ValueError(f"invalid embedded media path: {local_path}")

            content = BackupService._decode_embedded_media_file(media_file)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            if not file_path.exists() or hashlib.sha256(file_path.read_bytes()).hexdigest() != media_file["file_checksum"]["value"]:
                file_path.write_bytes(content)
                restored += 1
        return restored

    @staticmethod
    def validate_import_package(
        package: dict[str, Any],
        storage_root: Path | None = None,
        public_storage_prefix: str = "/static/storage",
        signing_key: str | None = None,
    ) -> dict[str, Any]:
        manifest_value = package.get("manifest")
        manifest = manifest_value if isinstance(manifest_value, dict) else {}
        schema = manifest.get("schema")
        errors: list[str] = []
        if not isinstance(manifest_value, dict):
            errors.append("manifest must be an object")
        checksum_valid: bool | None = None
        signature_valid: bool | None = None

        if not isinstance(schema, str) or schema not in SUPPORTED_BACKUP_SCHEMAS:
            errors.append(f"unsupported backup schema: {schema!r}")

        BackupService._validate_required_package_fields(package, errors, schema=schema)
        BackupService._validate_core_references(package, errors)
        if schema == BACKUP_SCHEMA:
            BackupService._validate_v2_references(package, errors)

        checksum = manifest.get("checksum")
        if checksum is not None:
            try:
                BackupService.validate_package_checksum(package)
                checksum_valid = True
            except ValueError as exc:
                checksum_valid = False
                errors.append(str(exc))

        signature = manifest.get("signature")
        if signature is not None:
            if not isinstance(signature, dict):
                errors.append("backup package signature must be an object")
            elif signing_key:
                try:
                    BackupService.validate_package_signature(package, signing_key)
                    signature_valid = True
                except ValueError as exc:
                    signature_valid = False
                    errors.append(str(exc))
            else:
                signature_valid = None
        elif signing_key:
            signature_valid = None

        source = manifest.get("source")
        if source is not None and not isinstance(source, dict):
            errors.append("manifest.source must be an object or null")

        counts = {
            "messages": len(BackupService._section_list(package, "messages")),
            "robot_messages": len(BackupService._section_list(package, "robot_messages")),
            "media_assets": len(BackupService._section_list(package, "media_assets")),
            "media_files": len(BackupService._section_list(package, "media_files")),
            "room_profiles": len(BackupService._section_list(package, "room_profiles")),
            "user_profiles": len(BackupService._section_list(package, "user_profiles")),
            "import_sources": len(BackupService._section_list(package, "import_sources")) if schema == BACKUP_SCHEMA else 0,
            "import_batches": len(BackupService._section_list(package, "import_batches")) if schema == BACKUP_SCHEMA else 0,
            "message_source_records": len(BackupService._section_list(package, "message_source_records")) if schema == BACKUP_SCHEMA else 0,
            "message_media_references": len(BackupService._section_list(package, "message_media_references")) if schema == BACKUP_SCHEMA else 0,
        }
        media_files = BackupService._validate_media_files(package, storage_root, public_storage_prefix, errors)

        return {
            "valid": not errors,
            "schema": schema if isinstance(schema, str) else None,
            "checksum_valid": checksum_valid,
            "signature_valid": signature_valid,
            "source": source if isinstance(source, dict) else None,
            "errors": errors,
            "counts": counts,
            "media_files": media_files,
        }

    @staticmethod
    def _message_matches_existing(existing: Message, item: dict[str, Any]) -> bool:
        return all(
            getattr(existing, key) == item.get(key)
            for key in ("platform", "room_id", "message_type", "external_message_id", "sender_id", "nickname", "raw_message", "local_message", "timestamp")
        )

    @staticmethod
    def _media_asset_matches_existing(existing: MediaAsset, item: dict[str, Any]) -> bool:
        return all(
            getattr(existing, key) == item.get(key)
            for key in ("file_type", "file_size", "local_path")
        )

    @staticmethod
    async def preview_import_package(
        db: AsyncSession,
        package: dict[str, Any],
        storage_root: Path | None = None,
        public_storage_prefix: str = "/static/storage",
        signing_key: str | None = None,
    ) -> dict[str, Any]:
        report = BackupService.validate_import_package(
            package,
            storage_root=storage_root,
            public_storage_prefix=public_storage_prefix,
            signing_key=signing_key,
        )
        schema = report.get("schema")
        if not report["valid"]:
            report["diff"] = {}
            return report
        message_diff = {"new": 0, "update": 0, "unchanged": 0}
        robot_message_diff = {"new": 0, "existing": 0}
        media_asset_diff = {"new": 0, "update": 0, "unchanged": 0}
        import_source_diff = {"new": 0, "update": 0, "unchanged": 0}
        import_batch_diff = {"new": 0, "update": 0, "unchanged": 0}
        source_record_diff = {"new": 0, "update": 0, "unchanged": 0}
        media_reference_diff = {"new": 0, "update": 0, "unchanged": 0}

        for item in package.get("messages", []) or []:
            result = await db.execute(select(Message).where(Message.msg_hash == item.get("msg_hash")))
            existing = result.scalar_one_or_none()
            if existing is None:
                message_diff["new"] += 1
            elif BackupService._message_matches_existing(existing, item):
                message_diff["unchanged"] += 1
            else:
                message_diff["update"] += 1

        for item in package.get("robot_messages", []) or []:
            result = await db.execute(
                select(RobotMessage).where(
                    RobotMessage.robot_id == item.get("robot_id"),
                    RobotMessage.msg_hash == item.get("msg_hash"),
                )
            )
            if result.scalar_one_or_none() is None:
                robot_message_diff["new"] += 1
            else:
                robot_message_diff["existing"] += 1

        for item in package.get("media_assets", []) or []:
            result = await db.execute(select(MediaAsset).where(MediaAsset.file_hash == item.get("file_hash")))
            existing = result.scalar_one_or_none()
            if existing is None:
                media_asset_diff["new"] += 1
            elif BackupService._media_asset_matches_existing(existing, item):
                media_asset_diff["unchanged"] += 1
            else:
                media_asset_diff["update"] += 1

        for item in (package.get("import_sources", []) or []) if schema == BACKUP_SCHEMA else []:
            identity_result = await db.execute(
                select(ImportSource.id).where(
                    ImportSource.source_type == item.get("source_type"),
                    ImportSource.account_id == item.get("account_id"),
                    ImportSource.device_id == item.get("device_id"),
                    ImportSource.id != item.get("id"),
                )
            )
            if identity_result.scalar_one_or_none() is not None:
                report["valid"] = False
                report["errors"].append(
                    f"import source {item.get('id')} conflicts with an existing source identity"
                )
            existing = await db.get(ImportSource, item.get("id"))
            if existing is None:
                import_source_diff["new"] += 1
            elif all(getattr(existing, key) == item.get(key) for key in ("source_type", "platform", "account_id", "device_id", "device_name", "qq_version", "schema_version", "status")):
                import_source_diff["unchanged"] += 1
            else:
                import_source_diff["update"] += 1

        for item in (package.get("import_batches", []) or []) if schema == BACKUP_SCHEMA else []:
            existing = await db.get(ImportBatch, item.get("id"))
            if existing is None:
                import_batch_diff["new"] += 1
            elif all(getattr(existing, key) == item.get(key) for key in ("source_id", "mode", "status")):
                import_batch_diff["unchanged"] += 1
            else:
                import_batch_diff["update"] += 1

        for item in (package.get("message_source_records", []) or []) if schema == BACKUP_SCHEMA else []:
            result = await db.execute(
                select(MessageSourceRecord).where(
                    MessageSourceRecord.source_id == item.get("source_id"),
                    MessageSourceRecord.source_table == item.get("source_table"),
                    MessageSourceRecord.source_primary_key == item.get("source_primary_key"),
                )
            )
            existing = result.scalar_one_or_none()
            if existing is None:
                source_record_diff["new"] += 1
            elif existing.msg_hash == item.get("msg_hash"):
                source_record_diff["unchanged"] += 1
            else:
                source_record_diff["update"] += 1

        for item in (package.get("message_media_references", []) or []) if schema == BACKUP_SCHEMA else []:
            result = await db.execute(
                select(MessageMediaReference).where(
                    MessageMediaReference.msg_hash == item.get("msg_hash"),
                    MessageMediaReference.ordinal == item.get("ordinal"),
                )
            )
            existing = result.scalar_one_or_none()
            if existing is None:
                media_reference_diff["new"] += 1
            elif (existing.source_state, existing.archive_state, existing.asset_file_hash, existing.thumbnail_file_hash) == (
                item.get("source_state"),
                item.get("archive_state"),
                item.get("asset_file_hash"),
                item.get("thumbnail_file_hash"),
            ):
                media_reference_diff["unchanged"] += 1
            else:
                media_reference_diff["update"] += 1

        report["diff"] = {
            "messages": message_diff,
            "robot_messages": robot_message_diff,
            "media_assets": media_asset_diff,
            "import_sources": import_source_diff,
            "import_batches": import_batch_diff,
            "message_source_records": source_record_diff,
            "message_media_references": media_reference_diff,
        }
        return report

    @staticmethod
    def write_failure_log(backup_root: Path, event: str, error: str, context: dict[str, Any] | None = None) -> Path:
        backup_root.mkdir(parents=True, exist_ok=True)
        log_path = backup_root / "failures.log"
        record = {
            "created_at": format_utc_z(utc_now()),
            "event": event,
            "error": error,
            "context": context or {},
        }
        with log_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return log_path

    @staticmethod
    async def import_package(
        db: AsyncSession,
        package: dict[str, Any],
        storage_root: Path | None = None,
        public_storage_prefix: str = "/static/storage",
        signing_key: str | None = None,
    ) -> dict[str, int]:
        manifest_value = package.get("manifest")
        manifest = manifest_value if isinstance(manifest_value, dict) else {}
        schema = manifest.get("schema")
        if not isinstance(schema, str) or schema not in SUPPORTED_BACKUP_SCHEMAS:
            raise ValueError(f"unsupported backup schema: {schema!r}")
        report = BackupService.validate_import_package(
            package,
            storage_root=storage_root,
            public_storage_prefix=public_storage_prefix,
            signing_key=signing_key,
        )
        if not report["valid"]:
            raise ValueError("; ".join(report["errors"]))

        import_source_count = 0
        for item in (package.get("import_sources", []) or []) if schema == BACKUP_SCHEMA else []:
            source = await db.get(ImportSource, item["id"])
            identity_result = await db.execute(
                select(ImportSource.id).where(
                    ImportSource.source_type == item["source_type"],
                    ImportSource.account_id == item["account_id"],
                    ImportSource.device_id == item["device_id"],
                    ImportSource.id != item["id"],
                )
            )
            if identity_result.scalar_one_or_none() is not None:
                raise ValueError("backup import source identity conflicts with an existing source id")
            if source is None:
                source = ImportSource(id=item["id"])
                db.add(source)
            source.source_type = item["source_type"]
            source.platform = item["platform"]
            source.account_id = item["account_id"]
            source.device_id = item["device_id"]
            source.device_name = item.get("device_name")
            source.qq_version = item.get("qq_version")
            source.schema_version = item.get("schema_version")
            source.status = item["status"]
            source.first_seen_at = parse_utc_datetime(item["first_seen_at"])
            source.last_seen_at = parse_utc_datetime(item["last_seen_at"])
            source.metadata_json = json.dumps(item.get("metadata") or {}, ensure_ascii=False, sort_keys=True)
            import_source_count += 1
        await db.flush()

        import_batch_count = 0
        for item in (package.get("import_batches", []) or []) if schema == BACKUP_SCHEMA else []:
            batch = await db.get(ImportBatch, item["id"])
            if batch is None:
                batch = ImportBatch(id=item["id"])
                db.add(batch)
            batch.source_id = item["source_id"]
            batch.mode = item["mode"]
            batch.status = item["status"]
            batch.started_at = parse_utc_datetime(item["started_at"])
            batch.completed_at = parse_utc_datetime(item["completed_at"]) if item.get("completed_at") else None
            for field in (
                "scanned_messages",
                "inserted_messages",
                "updated_messages",
                "skipped_messages",
                "uploaded_media",
                "not_downloaded_media",
                "missing_media",
                "failed_media",
            ):
                setattr(batch, field, int(item.get(field) or 0))
            batch.detail_json = json.dumps(item.get("detail") or {}, ensure_ascii=False, sort_keys=True)
            import_batch_count += 1
        await db.flush()

        message_count = 0
        for item in package.get("messages", []):
            result = await db.execute(select(Message).where(Message.msg_hash == item["msg_hash"]))
            message = result.scalar_one_or_none()
            if message is None:
                db.add(
                    Message(
                        msg_hash=item["msg_hash"],
                        platform=item["platform"],
                        room_id=item["room_id"],
                        message_type=item["message_type"],
                        external_message_id=item.get("external_message_id"),
                        sender_id=item["sender_id"],
                        nickname=item.get("nickname"),
                        raw_message=item["raw_message"],
                        local_message=item["local_message"],
                        timestamp=item["timestamp"],
                    )
                )
            else:
                message.platform = item["platform"]
                message.room_id = item["room_id"]
                message.message_type = item["message_type"]
                message.external_message_id = item.get("external_message_id")
                message.sender_id = item["sender_id"]
                message.nickname = item.get("nickname")
                message.raw_message = item["raw_message"]
                message.local_message = item["local_message"]
                message.timestamp = item["timestamp"]
            message_count += 1

        robot_message_count = 0
        for item in package.get("robot_messages", []):
            result = await db.execute(
                select(RobotMessage).where(
                    RobotMessage.robot_id == item["robot_id"],
                    RobotMessage.msg_hash == item["msg_hash"],
                )
            )
            assoc = result.scalar_one_or_none()
            if assoc is None:
                db.add(RobotMessage(robot_id=item["robot_id"], msg_hash=item["msg_hash"]))
            robot_message_count += 1

        media_asset_count = 0
        for item in package.get("media_assets", []):
            result = await db.execute(select(MediaAsset).where(MediaAsset.file_hash == item["file_hash"]))
            asset = result.scalar_one_or_none()
            if asset is None:
                db.add(
                    MediaAsset(
                        file_hash=item["file_hash"],
                        file_type=item["file_type"],
                        file_size=item["file_size"],
                        local_path=item["local_path"],
                    )
                )
            else:
                asset.file_type = item["file_type"]
                asset.file_size = item["file_size"]
                asset.local_path = item["local_path"]
            media_asset_count += 1

        await db.flush()

        message_source_record_count = 0
        for item in (package.get("message_source_records", []) or []) if schema == BACKUP_SCHEMA else []:
            result = await db.execute(
                select(MessageSourceRecord).where(
                    MessageSourceRecord.source_id == item["source_id"],
                    MessageSourceRecord.source_table == item["source_table"],
                    MessageSourceRecord.source_primary_key == item["source_primary_key"],
                )
            )
            record = result.scalar_one_or_none()
            imported_at = parse_utc_datetime(item["imported_at"])
            last_seen_at = parse_utc_datetime(item["last_seen_at"])
            if record is None:
                record = MessageSourceRecord(
                    msg_hash=item["msg_hash"],
                    source_id=item["source_id"],
                    source_table=item["source_table"],
                    source_primary_key=item["source_primary_key"],
                    imported_at=imported_at,
                    last_seen_at=last_seen_at,
                )
                db.add(record)
            elif record.msg_hash != item["msg_hash"]:
                raise ValueError("backup source record is bound to a different canonical message")
            else:
                record.imported_at = min(record.imported_at, imported_at)
                record.last_seen_at = max(record.last_seen_at, last_seen_at)
            record.batch_id = item.get("batch_id")
            record.source_external_message_id = item.get("source_external_message_id")
            record.platform_message_id = item.get("platform_message_id")
            record.schema_version = item.get("schema_version")
            record.raw_columns_json = json.dumps(item.get("raw_columns") or {}, ensure_ascii=False, sort_keys=True)
            record.raw_40800_protobuf = item.get("raw_40800_protobuf")
            record.raw_40900_protobuf = item.get("raw_40900_protobuf")
            record.metadata_json = json.dumps(item.get("metadata") or {}, ensure_ascii=False, sort_keys=True)
            message_source_record_count += 1

        message_media_reference_count = 0
        for item in (package.get("message_media_references", []) or []) if schema == BACKUP_SCHEMA else []:
            reference_request = MessageMediaReferenceRequest.model_validate(item)
            await ImportService._upsert_media_reference(
                db,
                msg_hash=item["msg_hash"],
                reference=reference_request,
            )
            result = await db.execute(
                select(MessageMediaReference).where(
                    MessageMediaReference.msg_hash == item["msg_hash"],
                    MessageMediaReference.ordinal == item["ordinal"],
                )
            )
            reference = result.scalar_one()
            incoming_first_seen = parse_utc_datetime(item["first_seen_at"])
            incoming_last_checked = parse_utc_datetime(item["last_checked_at"])
            reference.first_seen_at = min(reference.first_seen_at, incoming_first_seen)
            reference.last_checked_at = max(reference.last_checked_at, incoming_last_checked)
            if item.get("archived_at") and reference.archived_at is None:
                reference.archived_at = parse_utc_datetime(item["archived_at"])
            message_media_reference_count += 1

        for item in package.get("room_profiles", []) or []:
            profile = await db.get(RoomProfile, item["room_id"])
            if profile is None:
                db.add(
                    RoomProfile(
                        room_id=item["room_id"],
                        platform=item["platform"],
                        display_name=item.get("display_name"),
                        avatar_path=item.get("avatar_path"),
                    )
                )
            else:
                profile.platform = item["platform"]
                profile.display_name = item.get("display_name")
                profile.avatar_path = item.get("avatar_path")

        for item in package.get("user_profiles", []) or []:
            profile = await db.get(UserProfile, item["user_id"])
            if profile is None:
                db.add(
                    UserProfile(
                        user_id=item["user_id"],
                        platform=item["platform"],
                        display_name=item.get("display_name"),
                        avatar_path=item.get("avatar_path"),
                    )
                )
            else:
                profile.platform = item["platform"]
                profile.display_name = item.get("display_name")
                profile.avatar_path = item.get("avatar_path")

        BackupService._restore_embedded_media_files(package, storage_root, public_storage_prefix)
        await db.commit()
        result = {
            "messages": message_count,
            "robot_messages": robot_message_count,
            "media_assets": media_asset_count,
        }
        if schema == BACKUP_SCHEMA:
            result.update(
                {
                    "import_sources": import_source_count,
                    "import_batches": import_batch_count,
                    "message_source_records": message_source_record_count,
                    "message_media_references": message_media_reference_count,
                }
            )
        return result

async def _auto_backup_loop(settings, sessionmaker) -> None:
    from app.services.backup_config_service import BackupConfigService

    while True:
        try:
            async with sessionmaker() as session:
                backup_config = await BackupConfigService.get_effective_config(session, settings)
            if not backup_config.enabled:
                await asyncio.sleep(60)
                continue
            now = utc_now().replace(microsecond=0)
            next_run = BackupService.next_run_from_cron(backup_config.cron, now)
            seconds_until_run = max(0, (next_run - now).total_seconds())
            if seconds_until_run > 60:
                await asyncio.sleep(60)
                continue
            await asyncio.sleep(seconds_until_run)
            async with sessionmaker() as session:
                backup_config = await BackupConfigService.get_effective_config(session, settings)
                if not backup_config.enabled:
                    continue
                backup_path = await BackupService.write_auto_backup_file(
                    session,
                    backup_root=settings.backup_root,
                    storage_root=settings.storage_root,
                    public_storage_prefix=settings.public_storage_prefix,
                    max_media_bytes=getattr(settings, "media_max_bytes", None),
                    keep_latest=backup_config.keep_latest,
                    system_id=getattr(settings, "system_instance_id", None),
                    signing_key=getattr(settings, "app_secret_key", None),
                )
                logger.info(
                    "Auto backup completed",
                    extra={"backup_path": str(backup_path), "cron": backup_config.cron, "keep_latest": backup_config.keep_latest},
                )
        except Exception as exc:
            logger.exception(
                "Auto backup failed",
                extra={"cron": getattr(settings, "auto_backup_cron", None), "error": str(exc)},
            )
            BackupService.write_failure_log(
                settings.backup_root,
                event="auto_backup",
                error=str(exc),
                context={"cron": getattr(settings, "auto_backup_cron", None)},
            )
            await asyncio.sleep(60)


def start_auto_backup_scheduler(*, settings, sessionmaker) -> asyncio.Task | None:
    return asyncio.create_task(_auto_backup_loop(settings, sessionmaker))
