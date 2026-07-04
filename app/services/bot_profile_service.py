from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Adapter, BotProfile
from app.time_utils import utc_now


class BotProfileService:
    @staticmethod
    async def upsert_bot_profile(
        db: AsyncSession,
        *,
        robot_id: str,
        platform: str,
        display_name: str | None = None,
        adapter_id: str | None = None,
    ) -> BotProfile:
        now = utc_now()
        profile = await db.get(BotProfile, robot_id)
        if profile is None:
            profile = BotProfile(
                id=robot_id,
                platform=platform,
                display_name=display_name,
                source_adapter_id=adapter_id,
                first_seen_at=now,
                last_seen_at=now,
            )
            db.add(profile)
        else:
            profile.platform = platform
            if display_name:
                profile.display_name = display_name
            if adapter_id:
                profile.source_adapter_id = adapter_id
            profile.last_seen_at = now

        if adapter_id:
            adapter = await db.get(Adapter, adapter_id)
            if adapter is None:
                adapter = Adapter(
                    id=adapter_id,
                    platform=platform,
                    status="gray",
                    current_robot_id=robot_id,
                )
                db.add(adapter)
            else:
                adapter.platform = platform
                adapter.current_robot_id = robot_id
                adapter.updated_at = now

        await db.commit()
        await db.refresh(profile)
        return profile
