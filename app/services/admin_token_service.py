import hashlib
import secrets
from dataclasses import dataclass

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminToken
from app.time_utils import utc_now


VALID_ADMIN_ROLES = {"viewer", "operator", "admin"}


@dataclass(frozen=True)
class MatchedAdminToken:
    role: str
    actor: str
    token_id: int


class AdminTokenService:
    @staticmethod
    def normalize_role(role: str | None) -> str:
        normalized = str(role or "viewer").strip().lower()
        return normalized if normalized in VALID_ADMIN_ROLES else "viewer"

    @staticmethod
    def generate_token() -> str:
        return "cat_" + secrets.token_urlsafe(32)

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    async def create_token(db: AsyncSession, *, name: str, role: str) -> tuple[AdminToken, str]:
        token = AdminTokenService.generate_token()
        record = AdminToken(
            name=name.strip() or "admin-token",
            role=AdminTokenService.normalize_role(role),
            token_hash=AdminTokenService.hash_token(token),
            token_prefix=token[:12],
            status="active",
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)
        return record, token

    @staticmethod
    async def list_tokens(db: AsyncSession) -> list[AdminToken]:
        result = await db.execute(select(AdminToken).order_by(desc(AdminToken.created_at), desc(AdminToken.id)))
        return list(result.scalars().all())

    @staticmethod
    async def match_token(db: AsyncSession, token: str) -> MatchedAdminToken | None:
        token_hash = AdminTokenService.hash_token(token)
        result = await db.execute(select(AdminToken).where(AdminToken.token_hash == token_hash, AdminToken.status == "active"))
        record = result.scalar_one_or_none()
        if record is None:
            return None
        record.last_used_at = utc_now()
        await db.commit()
        return MatchedAdminToken(role=record.role, actor=f"db-token:{record.name}", token_id=record.id)

    @staticmethod
    async def revoke_token(db: AsyncSession, token_id: int) -> AdminToken | None:
        record = await db.get(AdminToken, token_id)
        if record is None:
            return None
        if record.status != "revoked":
            record.status = "revoked"
            record.revoked_at = utc_now()
            await db.commit()
            await db.refresh(record)
        return record

    @staticmethod
    async def rotate_token(db: AsyncSession, token_id: int) -> tuple[AdminToken, str] | None:
        record = await db.get(AdminToken, token_id)
        if record is None:
            return None
        token = AdminTokenService.generate_token()
        record.token_hash = AdminTokenService.hash_token(token)
        record.token_prefix = token[:12]
        record.status = "active"
        record.revoked_at = None
        await db.commit()
        await db.refresh(record)
        return record, token
