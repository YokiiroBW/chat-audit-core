import hashlib
import html
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MediaAsset, Message, RoomProfile, UserProfile
from app.services.backup_service import BackupService
from app.services.message_service import MessageService


@dataclass
class OfflineRepairReport:
    scanned_messages: int = 0
    repaired_media_assets: int = 0
    repaired_media_files: int = 0
    repaired_file_sizes: int = 0
    repaired_profile_avatars: int = 0
    repaired_paths: list[str] = field(default_factory=list)


_ONE_PIXEL_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!"
    b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00"
    b"\x00\x02\x02D\x01\x00;"
)


class OfflineRepairService:
    @staticmethod
    async def repair_local_media_integrity(
        db: AsyncSession,
        *,
        storage_root: str | Path,
        public_storage_prefix: str = "/static/storage",
        limit: int = 50000,
    ) -> OfflineRepairReport:
        report = OfflineRepairReport()
        root = Path(storage_root)
        result = await db.execute(select(Message).order_by(Message.timestamp.asc(), Message.msg_hash.asc()).limit(limit))
        messages = list(result.scalars().all())
        report.scanned_messages = len(messages)

        local_paths: set[str] = set()
        group_platforms: dict[str, str] = {}
        user_profiles: dict[str, tuple[str, str | None]] = {}
        for message in messages:
            local_paths.update(BackupService._extract_local_media_paths(message.local_message, public_storage_prefix))
            if message.message_type == "group":
                group_platforms.setdefault(message.room_id, message.platform)
            elif message.message_type == "private":
                user_profiles.setdefault(message.room_id, (message.platform, None))
            user_profiles.setdefault(message.sender_id, (message.platform, message.nickname))

        profile_paths = await OfflineRepairService._repair_profile_avatars(
            db,
            group_platforms=group_platforms,
            user_profiles=user_profiles,
            storage_root=root,
            public_storage_prefix=public_storage_prefix,
        )
        report.repaired_profile_avatars = len(profile_paths)
        local_paths.update(profile_paths)
        report.repaired_paths.extend(path for path in profile_paths if path not in report.repaired_paths)

        asset_result = await db.execute(select(MediaAsset))
        assets = list(asset_result.scalars().all())
        asset_by_path = {asset.local_path: asset for asset in assets}
        used_hashes = {asset.file_hash for asset in assets}

        for local_path in sorted(local_paths):
            file_path = BackupService._local_media_file_path(local_path, root, public_storage_prefix)
            if file_path is None:
                continue
            if not file_path.exists():
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_bytes(OfflineRepairService._placeholder_bytes_for(local_path))
                report.repaired_media_files += 1
                report.repaired_paths.append(local_path)

            content = file_path.read_bytes()
            asset = asset_by_path.get(local_path)
            if asset is None:
                file_hash = OfflineRepairService._file_hash_for(local_path, content, used_hashes)
                used_hashes.add(file_hash)
                asset = MediaAsset(
                    file_hash=file_hash,
                    file_type=OfflineRepairService._file_type_for(local_path),
                    file_size=len(content),
                    local_path=local_path,
                )
                db.add(asset)
                asset_by_path[local_path] = asset
                report.repaired_media_assets += 1
                if local_path not in report.repaired_paths:
                    report.repaired_paths.append(local_path)
            elif asset.file_size != len(content):
                asset.file_size = len(content)
                report.repaired_file_sizes += 1
                if local_path not in report.repaired_paths:
                    report.repaired_paths.append(local_path)

        for asset in assets:
            file_path = BackupService._local_media_file_path(asset.local_path, root, public_storage_prefix)
            if file_path is None:
                continue
            if not file_path.exists():
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_bytes(OfflineRepairService._placeholder_bytes_for(asset.local_path))
                report.repaired_media_files += 1
                if asset.local_path not in report.repaired_paths:
                    report.repaired_paths.append(asset.local_path)
            file_size = file_path.stat().st_size
            if asset.file_size != file_size:
                asset.file_size = file_size
                report.repaired_file_sizes += 1
                if asset.local_path not in report.repaired_paths:
                    report.repaired_paths.append(asset.local_path)

        await db.commit()
        return report

    @staticmethod
    async def _repair_profile_avatars(
        db: AsyncSession,
        *,
        group_platforms: dict[str, str],
        user_profiles: dict[str, tuple[str, str | None]],
        storage_root: Path,
        public_storage_prefix: str,
    ) -> list[str]:
        repaired_paths: list[str] = []
        room_profiles: dict[str, RoomProfile] = {}
        existing_user_profiles: dict[str, UserProfile] = {}

        if group_platforms:
            room_result = await db.execute(select(RoomProfile).where(RoomProfile.room_id.in_(group_platforms)))
            room_profiles = {profile.room_id: profile for profile in room_result.scalars().all()}
        if user_profiles:
            user_result = await db.execute(select(UserProfile).where(UserProfile.user_id.in_(user_profiles)))
            existing_user_profiles = {profile.user_id: profile for profile in user_result.scalars().all()}

        for room_id, platform in sorted(group_platforms.items()):
            profile = room_profiles.get(room_id)
            if profile is not None and OfflineRepairService._is_local_profile_avatar(profile.avatar_path, public_storage_prefix):
                continue
            local_path = await OfflineRepairService._save_profile_placeholder(
                db,
                profile_type="room",
                profile_id=room_id,
                storage_root=storage_root,
                public_storage_prefix=public_storage_prefix,
            )
            if profile is None:
                profile = RoomProfile(room_id=room_id, platform=platform)
                db.add(profile)
            profile.platform = platform
            profile.avatar_path = local_path
            repaired_paths.append(local_path)

        for user_id, (platform, display_name) in sorted(user_profiles.items()):
            profile = existing_user_profiles.get(user_id)
            if profile is not None and OfflineRepairService._is_local_profile_avatar(profile.avatar_path, public_storage_prefix):
                continue
            local_path = await OfflineRepairService._save_profile_placeholder(
                db,
                profile_type="user",
                profile_id=user_id,
                storage_root=storage_root,
                public_storage_prefix=public_storage_prefix,
            )
            if profile is None:
                profile = UserProfile(user_id=user_id, platform=platform)
                db.add(profile)
            profile.platform = platform
            if display_name and not profile.display_name:
                profile.display_name = display_name
            profile.avatar_path = local_path
            repaired_paths.append(local_path)

        return repaired_paths

    @staticmethod
    def _is_local_profile_avatar(avatar_path: str | None, public_storage_prefix: str) -> bool:
        return bool(avatar_path and avatar_path.startswith(public_storage_prefix.rstrip("/") + "/"))

    @staticmethod
    async def _save_profile_placeholder(
        db: AsyncSession,
        *,
        profile_type: str,
        profile_id: str,
        storage_root: Path,
        public_storage_prefix: str,
    ) -> str:
        content = OfflineRepairService._profile_placeholder_svg(profile_type, profile_id)
        return await MessageService.save_media_asset(
            db,
            file_content=content,
            file_type="image",
            ext="svg",
            storage_root=storage_root,
            public_prefix=public_storage_prefix,
        )

    @staticmethod
    def _profile_placeholder_svg(profile_type: str, profile_id: str) -> bytes:
        label = (profile_id or "--")[-2:]
        title = html.escape(f"{profile_type}:{profile_id}", quote=True)
        label_text = html.escape(label, quote=True)
        color = "#2563eb" if profile_type == "room" else "#0f766e"
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96">'
            f"<title>{title}</title>"
            f'<rect width="96" height="96" rx="18" fill="{color}"/>'
            '<circle cx="48" cy="38" r="16" fill="#ffffff" opacity=".9"/>'
            '<path d="M20 82c4-16 15-26 28-26s24 10 28 26" fill="#ffffff" opacity=".9"/>'
            f'<text x="48" y="91" text-anchor="middle" font-family="Arial, sans-serif" font-size="13" fill="#ffffff">{label_text}</text>'
            "</svg>"
        )
        return svg.encode("utf-8")

    @staticmethod
    def _file_hash_for(local_path: str, content: bytes, used_hashes: set[str]) -> str:
        stem = Path(local_path).stem.lower()
        if len(stem) == 32 and all(char in "0123456789abcdef" for char in stem) and stem not in used_hashes:
            return stem
        candidate = hashlib.md5(content).hexdigest()
        if candidate not in used_hashes:
            return candidate
        return hashlib.md5(f"{local_path}\n".encode("utf-8") + content).hexdigest()

    @staticmethod
    def _file_type_for(local_path: str) -> str:
        suffix = Path(local_path).suffix.lstrip(".").lower()
        if suffix in {"png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"}:
            return "image"
        if suffix in {"mp4", "webm", "mov", "mkv", "avi"}:
            return "video"
        if suffix in {"mp3", "wav", "ogg", "silk", "amr", "m4a", "flac"}:
            return "voice"
        if suffix == "html":
            return "card_page"
        if suffix == "json":
            return "forward"
        return "file"

    @staticmethod
    def _placeholder_bytes_for(local_path: str) -> bytes:
        suffix = Path(local_path).suffix.lstrip(".").lower()
        if suffix == "html":
            return b'<!doctype html><meta charset="utf-8"><title>missing local asset</title><p>missing local asset placeholder</p>'
        if suffix == "svg":
            return b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"><rect width="1" height="1" fill="#e2e8f0"/></svg>'
        if suffix in {"png", "jpg", "jpeg", "gif", "webp", "bmp"}:
            return _ONE_PIXEL_GIF
        return b"missing local asset placeholder\n"
