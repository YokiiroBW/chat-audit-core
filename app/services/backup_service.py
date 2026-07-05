import asyncio
import base64
import contextlib
import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MediaAsset, Message, RobotMessage, RoomProfile, UserProfile
from app.time_utils import utc_now

BACKUP_SCHEMA = "chat-audit-core.backup.v1"


class BackupService:
    _REQUIRED_FIELDS = {
        "messages": ("msg_hash", "platform", "room_id", "message_type", "sender_id", "raw_message", "local_message", "timestamp"),
        "robot_messages": ("robot_id", "msg_hash"),
        "media_assets": ("file_hash", "file_type", "file_size", "local_path"),
        "media_files": ("local_path", "file_size", "file_checksum", "content_base64"),
        "room_profiles": ("room_id", "platform"),
        "user_profiles": ("user_id", "platform"),
    }

    @staticmethod
    def calculate_package_checksum(package: dict[str, Any]) -> str:
        canonical_package = json.loads(json.dumps(package, ensure_ascii=False))
        manifest = canonical_package.setdefault("manifest", {})
        manifest.pop("checksum", None)
        payload = json.dumps(canonical_package, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

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
    def validate_package_checksum(package: dict[str, Any]) -> None:
        checksum = (package.get("manifest") or {}).get("checksum")
        if checksum is None:
            return
        if checksum.get("algorithm") != "sha256":
            raise ValueError(f"unsupported checksum algorithm: {checksum.get('algorithm')!r}")
        expected = checksum.get("value")
        actual = BackupService.calculate_package_checksum(package)
        if expected != actual:
            raise ValueError("backup package checksum mismatch")

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
        if media_paths:
            media_result = await db.execute(select(MediaAsset).where(MediaAsset.local_path.in_(media_paths)).order_by(MediaAsset.file_hash.asc()))
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
                "created_at": utc_now().isoformat(timespec="seconds") + "Z",
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
                },
            },
            "messages": [BackupService._message_to_dict(message) for message in messages],
            "robot_messages": robot_messages,
            "media_assets": media_assets,
            "media_files": media_files,
            "room_profiles": room_profiles,
            "user_profiles": user_profiles,
        }
        return BackupService.attach_package_checksum(package)


    @staticmethod
    async def write_auto_backup_file(
        db: AsyncSession,
        backup_root: Path,
        storage_root: Path | None = None,
        public_storage_prefix: str = "/static/storage",
        max_media_bytes: int | None = None,
        keep_latest: int = 7,
    ) -> Path:
        backup_root.mkdir(parents=True, exist_ok=True)
        package = await BackupService.export_package(
            db,
            storage_root=storage_root,
            public_storage_prefix=public_storage_prefix,
            max_media_bytes=max_media_bytes,
        )
        package["manifest"]["backup_type"] = "auto"
        package["manifest"]["created_by"] = "auto_backup_scheduler"
        BackupService.attach_package_checksum(package)

        timestamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
        backup_path = backup_root / f"auto-backup-{timestamp}.json"
        counter = 1
        while backup_path.exists():
            backup_path = backup_root / f"auto-backup-{timestamp}-{counter}.json"
            counter += 1

        backup_path.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")

        if keep_latest > 0:
            backups = sorted(backup_root.glob("auto-backup-*.json"), key=lambda path: (path.stat().st_mtime, path.name))
            for old_path in backups[:-keep_latest]:
                with contextlib.suppress(FileNotFoundError):
                    old_path.unlink()

        return backup_path

    @staticmethod
    def next_run_from_cron(cron_expr: str, now: dt.datetime | None = None) -> dt.datetime:
        now = (now or utc_now()).replace(microsecond=0)
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
    def _local_media_file_path(local_path: str, storage_root: Path, public_storage_prefix: str) -> Path | None:
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
        try:
            content = base64.b64decode(media_file.get("content_base64") or "", validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"invalid embedded media content: {media_file.get('local_path')}") from exc

        expected_size = media_file.get("file_size")
        if expected_size is not None and len(content) != expected_size:
            raise ValueError(f"embedded media size mismatch: {media_file.get('local_path')}")

        checksum = media_file.get("file_checksum") or {}
        if checksum.get("algorithm") != "sha256":
            raise ValueError(f"unsupported embedded media checksum algorithm: {checksum.get('algorithm')!r}")
        actual = hashlib.sha256(content).hexdigest()
        if actual != checksum.get("value"):
            raise ValueError(f"embedded media checksum mismatch: {media_file.get('local_path')}")

        return content

    @staticmethod
    def _embedded_media_files_by_path(package: dict[str, Any], errors: list[str] | None = None) -> dict[str, dict[str, Any]]:
        embedded: dict[str, dict[str, Any]] = {}
        for media_file in package.get("media_files", []) or []:
            local_path = media_file.get("local_path")
            if not local_path:
                if errors is not None:
                    errors.append("embedded media file missing local_path")
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
    def _validate_required_package_fields(package: dict[str, Any], errors: list[str]) -> None:
        for section, required_fields in BackupService._REQUIRED_FIELDS.items():
            items = package.get(section, []) or []
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

        for asset in package.get("media_assets", []) or []:
            checksum = asset.get("file_checksum")
            if checksum is None:
                continue
            media_files["checked"] += 1
            if checksum.get("algorithm") != "sha256":
                media_files["mismatch"] += 1
                errors.append(f"unsupported media checksum algorithm for {asset.get('local_path')}: {checksum.get('algorithm')!r}")
                continue

            local_path = asset.get("local_path") or ""
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
    ) -> dict[str, Any]:
        manifest = package.get("manifest") or {}
        schema = manifest.get("schema")
        errors: list[str] = []
        checksum_valid: bool | None = None

        if schema != BACKUP_SCHEMA:
            errors.append(f"unsupported backup schema: {schema!r}")

        BackupService._validate_required_package_fields(package, errors)

        checksum = manifest.get("checksum")
        if checksum is not None:
            try:
                BackupService.validate_package_checksum(package)
                checksum_valid = True
            except ValueError as exc:
                checksum_valid = False
                errors.append(str(exc))

        counts = {
            "messages": len(package.get("messages", []) or []),
            "robot_messages": len(package.get("robot_messages", []) or []),
            "media_assets": len(package.get("media_assets", []) or []),
            "media_files": len(package.get("media_files", []) or []),
            "room_profiles": len(package.get("room_profiles", []) or []),
            "user_profiles": len(package.get("user_profiles", []) or []),
        }
        media_files = BackupService._validate_media_files(package, storage_root, public_storage_prefix, errors)

        return {
            "valid": not errors,
            "schema": schema,
            "checksum_valid": checksum_valid,
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
    ) -> dict[str, Any]:
        report = BackupService.validate_import_package(
            package,
            storage_root=storage_root,
            public_storage_prefix=public_storage_prefix,
        )
        message_diff = {"new": 0, "update": 0, "unchanged": 0}
        robot_message_diff = {"new": 0, "existing": 0}
        media_asset_diff = {"new": 0, "update": 0, "unchanged": 0}

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

        report["diff"] = {
            "messages": message_diff,
            "robot_messages": robot_message_diff,
            "media_assets": media_asset_diff,
        }
        return report

    @staticmethod
    def write_failure_log(backup_root: Path, event: str, error: str, context: dict[str, Any] | None = None) -> Path:
        backup_root.mkdir(parents=True, exist_ok=True)
        log_path = backup_root / "failures.log"
        record = {
            "created_at": utc_now().isoformat(timespec="seconds") + "Z",
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
    ) -> dict[str, int]:
        manifest = package.get("manifest") or {}
        schema = manifest.get("schema")
        if schema != BACKUP_SCHEMA:
            raise ValueError(f"unsupported backup schema: {schema!r}")
        report = BackupService.validate_import_package(
            package,
            storage_root=storage_root,
            public_storage_prefix=public_storage_prefix,
        )
        if not report["valid"]:
            raise ValueError("; ".join(report["errors"]))
        BackupService._restore_embedded_media_files(package, storage_root, public_storage_prefix)

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

        await db.commit()
        return {
            "messages": message_count,
            "robot_messages": robot_message_count,
            "media_assets": media_asset_count,
        }

async def _auto_backup_loop(settings, sessionmaker) -> None:
    while True:
        now = utc_now().replace(microsecond=0)
        next_run = BackupService.next_run_from_cron(settings.auto_backup_cron, now)
        await asyncio.sleep(max(0, (next_run - now).total_seconds()))
        try:
            async with sessionmaker() as session:
                await BackupService.write_auto_backup_file(
                    session,
                    backup_root=settings.backup_root,
                    storage_root=settings.storage_root,
                    public_storage_prefix=settings.public_storage_prefix,
                    max_media_bytes=getattr(settings, "media_max_bytes", None),
                    keep_latest=getattr(settings, "auto_backup_keep_latest", 7),
                )
        except Exception as exc:
            BackupService.write_failure_log(
                settings.backup_root,
                event="auto_backup",
                error=str(exc),
                context={"cron": settings.auto_backup_cron},
            )


def start_auto_backup_scheduler(*, settings, sessionmaker) -> asyncio.Task | None:
    cron_expr = (settings.auto_backup_cron or "").strip()
    if not cron_expr or cron_expr.lower() in {"off", "disabled", "none", "false", "0"}:
        return None
    return asyncio.create_task(_auto_backup_loop(settings, sessionmaker))
