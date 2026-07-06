import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

import bcrypt
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminSession, AdminUser
from app.services.admin_token_service import AdminTokenService
from app.time_utils import utc_now

PASSWORD_HASH_ALGORITHM = "pbkdf2_sha256"
PASSWORD_HASH_ITERATIONS = 210_000
BCRYPT_ROUNDS = 12


@dataclass(frozen=True)
class MatchedAdminSession:
    role: str
    actor: str
    user_id: int
    session_id: int
    username: str


class AdminUserService:
    @staticmethod
    def normalize_username(username: str) -> str:
        return username.strip().lower()

    @staticmethod
    def hash_password(password: str) -> str:
        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("utf-8")

    @staticmethod
    def verify_password(password: str, password_hash: str) -> bool:
        if AdminUserService._is_bcrypt_hash(password_hash):
            try:
                return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
            except ValueError:
                return False
        if AdminUserService._verify_legacy_sha256_password(password, password_hash):
            return True
        return AdminUserService._verify_pbkdf2_password(password, password_hash)

    @staticmethod
    def password_hash_needs_upgrade(password_hash: str) -> bool:
        return not AdminUserService._is_bcrypt_hash(password_hash)

    @staticmethod
    def _is_bcrypt_hash(password_hash: str) -> bool:
        return password_hash.startswith(("$2a$", "$2b$", "$2y$"))

    @staticmethod
    def _verify_legacy_sha256_password(password: str, password_hash: str) -> bool:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", password_hash or ""):
            return False
        expected = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(expected, password_hash.lower())

    @staticmethod
    def _verify_pbkdf2_password(password: str, password_hash: str) -> bool:
        try:
            algorithm, iterations_raw, salt_raw, expected_raw = password_hash.split("$", 3)
            if algorithm != PASSWORD_HASH_ALGORITHM:
                return False
            iterations = int(iterations_raw)
            salt = base64.urlsafe_b64decode(salt_raw.encode("ascii"))
            expected = base64.urlsafe_b64decode(expected_raw.encode("ascii"))
        except (ValueError, TypeError):
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)

    @staticmethod
    def generate_session_token() -> str:
        return "cas_" + secrets.token_urlsafe(32)

    @staticmethod
    async def create_user(
        db: AsyncSession,
        *,
        username: str,
        password: str,
        role: str,
        display_name: str | None = None,
    ) -> AdminUser:
        normalized_username = AdminUserService.normalize_username(username)
        existing = await AdminUserService.get_user_by_username(db, normalized_username)
        if existing is not None:
            raise ValueError("admin user already exists")
        user = AdminUser(
            username=normalized_username,
            display_name=(display_name or "").strip() or None,
            role=AdminTokenService.normalize_role(role),
            password_hash=AdminUserService.hash_password(password),
            status="active",
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user

    @staticmethod
    async def get_user_by_username(db: AsyncSession, username: str) -> AdminUser | None:
        result = await db.execute(select(AdminUser).where(AdminUser.username == AdminUserService.normalize_username(username)))
        return result.scalar_one_or_none()

    @staticmethod
    async def list_users(db: AsyncSession) -> list[AdminUser]:
        result = await db.execute(select(AdminUser).order_by(desc(AdminUser.created_at), desc(AdminUser.id)))
        return list(result.scalars().all())

    @staticmethod
    async def list_sessions(db: AsyncSession) -> list[tuple[AdminSession, AdminUser]]:
        result = await db.execute(
            select(AdminSession, AdminUser)
            .join(AdminUser, AdminUser.id == AdminSession.user_id)
            .order_by(desc(AdminSession.created_at), desc(AdminSession.id))
        )
        return [(session, user) for session, user in result.all()]

    @staticmethod
    async def authenticate(db: AsyncSession, *, username: str, password: str) -> AdminUser | None:
        user = await AdminUserService.get_user_by_username(db, username)
        if user is None or user.status != "active":
            return None
        if not AdminUserService.verify_password(password, user.password_hash):
            return None
        if AdminUserService.password_hash_needs_upgrade(user.password_hash):
            user.password_hash = AdminUserService.hash_password(password)
        user.last_login_at = utc_now()
        await db.commit()
        await db.refresh(user)
        return user

    @staticmethod
    async def create_session(db: AsyncSession, user: AdminUser) -> tuple[AdminSession, str]:
        token = AdminUserService.generate_session_token()
        session = AdminSession(
            user_id=user.id,
            token_hash=AdminTokenService.hash_token(token),
            token_prefix=token[:12],
            status="active",
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        return session, token

    @staticmethod
    async def match_session(db: AsyncSession, token: str) -> MatchedAdminSession | None:
        token_hash = AdminTokenService.hash_token(token)
        result = await db.execute(
            select(AdminSession, AdminUser)
            .join(AdminUser, AdminUser.id == AdminSession.user_id)
            .where(AdminSession.token_hash == token_hash, AdminSession.status == "active", AdminUser.status == "active")
        )
        row = result.first()
        if row is None:
            return None
        session, user = row
        session.last_used_at = utc_now()
        await db.commit()
        return MatchedAdminSession(
            role=user.role,
            actor=f"db-user:{user.username}",
            user_id=user.id,
            session_id=session.id,
            username=user.username,
        )

    @staticmethod
    async def revoke_session(db: AsyncSession, token: str) -> AdminSession | None:
        token_hash = AdminTokenService.hash_token(token)
        result = await db.execute(select(AdminSession).where(AdminSession.token_hash == token_hash, AdminSession.status == "active"))
        session = result.scalar_one_or_none()
        if session is None:
            return None
        session.status = "revoked"
        session.revoked_at = utc_now()
        await db.commit()
        await db.refresh(session)
        return session

    @staticmethod
    async def revoke_session_by_id(db: AsyncSession, session_id: int) -> AdminSession | None:
        session = await db.get(AdminSession, session_id)
        if session is None:
            return None
        if session.status != "revoked":
            session.status = "revoked"
            session.revoked_at = utc_now()
            await db.commit()
            await db.refresh(session)
        return session

    @staticmethod
    async def revoke_user(db: AsyncSession, user_id: int) -> AdminUser | None:
        user = await db.get(AdminUser, user_id)
        if user is None:
            return None
        if user.status != "revoked":
            user.status = "revoked"
            user.revoked_at = utc_now()
            result = await db.execute(select(AdminSession).where(AdminSession.user_id == user_id, AdminSession.status == "active"))
            for session in result.scalars().all():
                session.status = "revoked"
                session.revoked_at = utc_now()
            await db.commit()
            await db.refresh(user)
        return user

    @staticmethod
    async def reset_password(db: AsyncSession, user_id: int, password: str) -> AdminUser | None:
        user = await db.get(AdminUser, user_id)
        if user is None:
            return None
        user.password_hash = AdminUserService.hash_password(password)
        result = await db.execute(select(AdminSession).where(AdminSession.user_id == user_id, AdminSession.status == "active"))
        for session in result.scalars().all():
            session.status = "revoked"
            session.revoked_at = utc_now()
        await db.commit()
        await db.refresh(user)
        return user
