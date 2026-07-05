import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


class AuditLogService:
    @staticmethod
    async def record(
        db: AsyncSession,
        *,
        action: str,
        status: str,
        actor: str | None = None,
        ip_address: str | None = None,
        target: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> AuditLog:
        log = AuditLog(
            action=action,
            status=status,
            actor=actor,
            ip_address=ip_address,
            target=target,
            detail_json=json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
        )
        db.add(log)
        await db.commit()
        await db.refresh(log)
        return log

    @staticmethod
    async def list_logs(
        db: AsyncSession,
        *,
        action: str | None = None,
        limit: int = 100,
    ) -> list[AuditLog]:
        stmt = select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit)
        if action:
            stmt = stmt.where(AuditLog.action == action)
        result = await db.execute(stmt)
        return list(result.scalars().all())
