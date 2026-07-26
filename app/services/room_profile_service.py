from pathlib import Path
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MediaAsset, ProfileChangeRecord, RoomProfile
from app.services.media_service import MediaService


logger = logging.getLogger(__name__)


class RoomProfileService:
    @staticmethod
    async def upsert_room_profile(
        db: AsyncSession,
        *,
        room_id: str,
        platform: str,
        display_name: str | None = None,
        avatar_path: str | None = None,
        avatar_source_url: str | None = None,
        source_id: str | None = None,
    ) -> RoomProfile:
        profile = await db.get(RoomProfile, room_id)
        if profile is None:
            profile = RoomProfile(room_id=room_id, platform=platform)
            db.add(profile)
        profile.platform = platform
        asset_result = await db.execute(select(MediaAsset).where(MediaAsset.local_path == avatar_path)) if avatar_path else None
        asset = asset_result.scalar_one_or_none() if asset_result is not None else None
        old_name = profile.display_name
        old_avatar_hash = profile.avatar_file_hash
        new_name = display_name or profile.display_name
        new_avatar_hash = asset.content_sha256 if asset is not None else profile.avatar_file_hash
        if display_name:
            profile.display_name = display_name
        if avatar_path:
            profile.avatar_path = avatar_path
            profile.avatar_file_hash = new_avatar_hash
            profile.avatar_status = "local"
        if avatar_source_url:
            profile.avatar_source_url = avatar_source_url
        if (old_name != profile.display_name or old_avatar_hash != profile.avatar_file_hash) and (old_name or old_avatar_hash):
            db.add(ProfileChangeRecord(
                identity_type="conversation",
                identity_id=room_id,
                old_display_name=old_name,
                new_display_name=profile.display_name,
                old_avatar_file_hash=old_avatar_hash,
                new_avatar_file_hash=profile.avatar_file_hash,
                source_id=source_id,
            ))
        await db.commit()
        await db.refresh(profile)
        return profile

    @staticmethod
    async def cache_qq_group_profile(
        db: AsyncSession,
        *,
        room_id: str,
        platform: str,
        group_info: dict[str, Any] | None = None,
        http_client: Any | None = None,
        storage_root: str | Path | None = None,
        public_prefix: str | None = None,
        max_bytes: int | None = None,
    ) -> RoomProfile:
        data = group_info or {}
        display_name = data.get("group_name") or data.get("group_memo") or data.get("name")
        avatar_url = f"https://p.qlogo.cn/gh/{room_id}/{room_id}/100"
        try:
            avatar_path = await MediaService.download_url_to_local_path(
                db,
                avatar_url,
                media_type="image",
                file_name=f"{room_id}.jpg",
                http_client=http_client,
                storage_root=storage_root,
                public_prefix=public_prefix,
                max_bytes=max_bytes,
            )
        except Exception:
            await db.rollback()
            avatar_path = None
            logger.exception("QQ group avatar hydration failed", extra={"room_id": room_id})
        return await RoomProfileService.upsert_room_profile(
            db,
            room_id=room_id,
            platform=platform,
            display_name=str(display_name) if display_name else None,
            avatar_path=avatar_path,
        )
