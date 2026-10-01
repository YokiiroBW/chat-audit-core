from dataclasses import dataclass

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CaptureTargetPolicy, Message, RobotMessage, RoomProfile, UserProfile
from app.services.media_service import _CQ_PATTERN


DEFAULT_ALLOWED_MEDIA_TYPES = {"image", "voice", "video"}
VALID_LIST_MODES = {"none", "blacklist", "whitelist"}
VALID_TARGET_TYPES = {"group", "private"}
CAPTURE_FIELDS = ("capture_text", "capture_image", "capture_voice", "capture_video", "capture_file")


@dataclass(frozen=True)
class CaptureDecision:
    should_capture: bool
    reason: str = "allowed"
    allowed_media_types: set[str] | None = None
    categories: set[str] | None = None


def _normalize_target_type(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in VALID_TARGET_TYPES:
        raise ValueError("target_type must be group or private")
    return normalized


def _normalize_list_mode(value: str | None) -> str:
    normalized = str(value or "none").strip().lower()
    if normalized not in VALID_LIST_MODES:
        raise ValueError("list_mode must be none, blacklist, or whitelist")
    return normalized


def _target_type_for_message(message_type: str) -> str:
    return "group" if str(message_type).lower() == "group" else "private"


def _message_categories(raw_message: str) -> set[str]:
    categories: set[str] = set()
    cursor = 0
    for match in _CQ_PATTERN.finditer(raw_message or ""):
        if (raw_message or "")[cursor:match.start()].strip():
            categories.add("text")
        kind = match.group("kind")
        if kind == "image":
            categories.add("image")
        elif kind == "record":
            categories.add("voice")
        elif kind == "video":
            categories.add("video")
        elif kind == "file":
            categories.add("file")
        else:
            categories.add("text")
        cursor = match.end()
    if (raw_message or "")[cursor:].strip():
        categories.add("text")
    return categories or {"text"}


def _allowed_categories(policy: CaptureTargetPolicy | None) -> set[str]:
    if policy is None:
        return {"text", "image", "voice", "video", "file"}
    allowed = set()
    if policy.capture_text:
        allowed.add("text")
    if policy.capture_image:
        allowed.add("image")
    if policy.capture_voice:
        allowed.add("voice")
    if policy.capture_video:
        allowed.add("video")
    if policy.capture_file:
        allowed.add("file")
    return allowed


def _media_types_from_categories(categories: set[str]) -> set[str]:
    return {category for category in categories if category in {"image", "voice", "video", "file"}}


class CapturePolicyService:
    @staticmethod
    async def list_policies(db: AsyncSession, robot_id: str) -> list[dict]:
        stats = CapturePolicyService._target_stats_subquery(robot_id)
        result = await db.execute(
            select(
                CaptureTargetPolicy,
                RoomProfile.display_name.label("room_display_name"),
                RoomProfile.avatar_path.label("room_avatar_path"),
                UserProfile.display_name.label("user_display_name"),
                UserProfile.avatar_path.label("user_avatar_path"),
                stats.c.last_timestamp.label("last_timestamp"),
            )
            .select_from(CaptureTargetPolicy)
            .outerjoin(
                RoomProfile,
                (RoomProfile.room_id == CaptureTargetPolicy.target_id)
                & (CaptureTargetPolicy.target_type == "group"),
            )
            .outerjoin(
                UserProfile,
                (UserProfile.user_id == CaptureTargetPolicy.target_id)
                & (CaptureTargetPolicy.target_type == "private"),
            )
            .outerjoin(stats, stats.c.room_id == CaptureTargetPolicy.target_id)
            .where(CaptureTargetPolicy.robot_id == robot_id)
            .order_by(CaptureTargetPolicy.target_type.asc(), CaptureTargetPolicy.target_id.asc())
        )
        return [CapturePolicyService._row_to_dict(row) for row in result.all()]

    @staticmethod
    def _target_stats_subquery(robot_id: str):
        return (
            select(
                Message.room_id.label("room_id"),
                func.max(Message.timestamp).label("last_timestamp"),
            )
            .join(RobotMessage, RobotMessage.msg_hash == Message.msg_hash)
            .where(RobotMessage.robot_id == robot_id)
            .group_by(Message.room_id)
            .subquery()
        )

    @staticmethod
    async def list_known_targets(db: AsyncSession, robot_id: str) -> list[dict]:
        private_sender_display_name = case(
            ((Message.message_type == "private") & (Message.sender_id == Message.room_id), Message.nickname),
            else_=None,
        )
        result = await db.execute(
            select(
                Message.room_id.label("target_id"),
                Message.message_type.label("target_type"),
                func.coalesce(RoomProfile.display_name, private_sender_display_name).label("room_display_name"),
                RoomProfile.avatar_path.label("room_avatar_path"),
                UserProfile.display_name.label("user_display_name"),
                UserProfile.avatar_path.label("user_avatar_path"),
                Message.timestamp.label("timestamp"),
            )
            .join(RobotMessage, RobotMessage.msg_hash == Message.msg_hash)
            .outerjoin(RoomProfile, RoomProfile.room_id == Message.room_id)
            .outerjoin(UserProfile, UserProfile.user_id == Message.room_id)
            .where(RobotMessage.robot_id == robot_id)
            .order_by(Message.timestamp.desc(), Message.room_id.asc())
        )
        seen: set[tuple[str, str]] = set()
        targets = []
        for row in result.all():
            target_type = _target_type_for_message(row.target_type)
            key = (target_type, str(row.target_id))
            if key in seen:
                continue
            seen.add(key)
            targets.append(
                {
                    "robot_id": robot_id,
                    "target_type": target_type,
                    "target_id": str(row.target_id),
                    "display_name": row.room_display_name or row.user_display_name,
                    "avatar_path": row.room_avatar_path or row.user_avatar_path,
                    "last_timestamp": row.timestamp,
                    "policy": None,
                }
            )
        return targets

    @staticmethod
    async def list_target_settings(db: AsyncSession, robot_id: str) -> list[dict]:
        policies = {
            (item["target_type"], item["target_id"]): item
            for item in await CapturePolicyService.list_policies(db, robot_id)
        }
        targets = await CapturePolicyService.list_known_targets(db, robot_id)
        for target in targets:
            target["policy"] = policies.pop((target["target_type"], target["target_id"]), None)
        for policy in policies.values():
            targets.append(
                {
                    "robot_id": robot_id,
                    "target_type": policy["target_type"],
                    "target_id": policy["target_id"],
                    "display_name": policy["display_name"],
                    "avatar_path": policy["avatar_path"],
                    "last_timestamp": policy["last_timestamp"],
                    "policy": policy,
                }
            )
        return targets

    @staticmethod
    async def upsert_policy(
        db: AsyncSession,
        *,
        robot_id: str,
        target_type: str,
        target_id: str,
        list_mode: str = "none",
        capture_text: bool = True,
        capture_image: bool = True,
        capture_voice: bool = True,
        capture_video: bool = True,
        capture_file: bool = False,
    ) -> CaptureTargetPolicy:
        normalized_type = _normalize_target_type(target_type)
        normalized_mode = _normalize_list_mode(list_mode)
        clean_target_id = str(target_id).strip()
        if not clean_target_id:
            raise ValueError("target_id is required")

        result = await db.execute(
            select(CaptureTargetPolicy).where(
                CaptureTargetPolicy.robot_id == robot_id,
                CaptureTargetPolicy.target_type == normalized_type,
                CaptureTargetPolicy.target_id == clean_target_id,
            )
        )
        policy = result.scalar_one_or_none()
        if policy is None:
            policy = CaptureTargetPolicy(robot_id=robot_id, target_type=normalized_type, target_id=clean_target_id)
            db.add(policy)
        policy.list_mode = normalized_mode
        policy.capture_text = bool(capture_text)
        policy.capture_image = bool(capture_image)
        policy.capture_voice = bool(capture_voice)
        policy.capture_video = bool(capture_video)
        policy.capture_file = bool(capture_file)
        await db.commit()
        await db.refresh(policy)
        return policy

    @staticmethod
    async def delete_policy(db: AsyncSession, *, robot_id: str, target_type: str, target_id: str) -> bool:
        normalized_type = _normalize_target_type(target_type)
        result = await db.execute(
            select(CaptureTargetPolicy).where(
                CaptureTargetPolicy.robot_id == robot_id,
                CaptureTargetPolicy.target_type == normalized_type,
                CaptureTargetPolicy.target_id == str(target_id),
            )
        )
        policy = result.scalar_one_or_none()
        if policy is None:
            return False
        await db.delete(policy)
        await db.commit()
        return True

    @staticmethod
    async def should_capture(db: AsyncSession, *, robot_id: str, msg_data: dict) -> CaptureDecision:
        target_type = _target_type_for_message(str(msg_data.get("message_type") or "private"))
        target_id = str(msg_data.get("room_id") or "")
        result = await db.execute(select(CaptureTargetPolicy).where(CaptureTargetPolicy.robot_id == robot_id))
        policies = list(result.scalars().all())
        policy_by_target = {
            (policy.target_type, policy.target_id): policy
            for policy in policies
        }
        policy = policy_by_target.get((target_type, target_id))

        has_whitelist = any(item.list_mode == "whitelist" for item in policies)
        if has_whitelist and (policy is None or policy.list_mode != "whitelist"):
            return CaptureDecision(False, "target_not_in_whitelist", set())
        if policy is not None and policy.list_mode == "blacklist":
            return CaptureDecision(False, "target_blacklisted", set())

        categories = _message_categories(str(msg_data.get("raw_message") or ""))
        allowed_categories = _allowed_categories(policy)
        if not categories <= allowed_categories:
            blocked = ",".join(sorted(categories - allowed_categories))
            return CaptureDecision(False, f"content_type_disabled:{blocked}", set(), categories)
        if policy is None:
            return CaptureDecision(True, allowed_media_types=set(DEFAULT_ALLOWED_MEDIA_TYPES), categories=categories)
        return CaptureDecision(True, allowed_media_types=_media_types_from_categories(allowed_categories), categories=categories)

    @staticmethod
    def policy_to_dict(policy: CaptureTargetPolicy) -> dict:
        return {
            "id": policy.id,
            "robot_id": policy.robot_id,
            "target_type": policy.target_type,
            "target_id": policy.target_id,
            "list_mode": policy.list_mode,
            "capture_text": policy.capture_text,
            "capture_image": policy.capture_image,
            "capture_voice": policy.capture_voice,
            "capture_video": policy.capture_video,
            "capture_file": policy.capture_file,
            "updated_at": policy.updated_at,
            "display_name": None,
            "avatar_path": None,
            "last_timestamp": None,
        }

    @staticmethod
    def _row_to_dict(row) -> dict:
        policy = row[0]
        item = CapturePolicyService.policy_to_dict(policy)
        item["display_name"] = row.room_display_name or row.user_display_name
        item["avatar_path"] = row.room_avatar_path or row.user_avatar_path
        item["last_timestamp"] = row.last_timestamp
        return item
