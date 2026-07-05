from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UserProfile
from app.services.media_service import MediaService


class UserProfileService:
    @staticmethod
    async def upsert_user_profile(
        db: AsyncSession,
        *,
        user_id: str,
        platform: str,
        display_name: str | None = None,
        avatar_path: str | None = None,
    ) -> UserProfile:
        profile = await db.get(UserProfile, user_id)
        if profile is None:
            profile = UserProfile(user_id=user_id, platform=platform)
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
    async def cache_qq_user_profile(
        db: AsyncSession,
        *,
        user_id: str,
        platform: str,
        display_name: str | None = None,
        http_client: Any | None = None,
        storage_root: str | Path | None = None,
        public_prefix: str | None = None,
        max_bytes: int | None = None,
    ) -> UserProfile:
        avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"
        avatar_path = await MediaService.download_url_to_local_path(
            db,
            avatar_url,
            media_type="image",
            file_name=f"{user_id}.jpg",
            http_client=http_client,
            storage_root=storage_root,
            public_prefix=public_prefix,
            max_bytes=max_bytes,
        )
        return await UserProfileService.upsert_user_profile(
            db,
            user_id=user_id,
            platform=platform,
            display_name=display_name,
            avatar_path=avatar_path,
        )
