from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RoomProfile
from app.services.media_service import MediaService


class RoomProfileService:
    @staticmethod
    async def upsert_room_profile(
        db: AsyncSession,
        *,
        room_id: str,
        platform: str,
        display_name: str | None = None,
        avatar_path: str | None = None,
    ) -> RoomProfile:
        profile = await db.get(RoomProfile, room_id)
        if profile is None:
            profile = RoomProfile(room_id=room_id, platform=platform)
            db.add(profile)
        profile.platform = platform
        if display_name:
            profile.display_name = display_name
        if avatar_path:
            profile.avatar_path = avatar_path
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
        return await RoomProfileService.upsert_room_profile(
            db,
            room_id=room_id,
            platform=platform,
            display_name=str(display_name) if display_name else None,
            avatar_path=avatar_path,
        )
