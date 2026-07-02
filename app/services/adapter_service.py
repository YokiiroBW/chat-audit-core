import datetime as dt

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Adapter


class AdapterService:
    @staticmethod
    async def create_adapter(
        db: AsyncSession,
        adapter_id: str,
        platform: str,
        config_json: str | None = None,
        status: str = "gray",
    ) -> Adapter:
        existing = await db.get(Adapter, adapter_id)
        if existing is not None:
            raise ValueError("adapter already exists")

        adapter = Adapter(id=adapter_id, platform=platform, config_json=config_json, status=status)
        db.add(adapter)
        await db.commit()
        await db.refresh(adapter)
        return adapter

    @staticmethod
    async def update_adapter(
        db: AsyncSession,
        adapter_id: str,
        platform: str | None = None,
        config_json: str | None = None,
        status: str | None = None,
        config_json_provided: bool = False,
    ) -> Adapter | None:
        adapter = await db.get(Adapter, adapter_id)
        if adapter is None:
            return None

        if platform is not None:
            adapter.platform = platform
        if config_json_provided:
            adapter.config_json = config_json
        if status is not None:
            adapter.status = status
        adapter.updated_at = dt.datetime.utcnow()
        await db.commit()
        await db.refresh(adapter)
        return adapter

    @staticmethod
    async def delete_adapter(db: AsyncSession, adapter_id: str) -> bool:
        adapter = await db.get(Adapter, adapter_id)
        if adapter is None:
            return False
        await db.delete(adapter)
        await db.commit()
        return True
