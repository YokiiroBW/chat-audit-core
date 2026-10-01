import hashlib
import html
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.message_service import MessageService


class ProfilePlaceholderService:
    @staticmethod
    def _placeholder_svg(profile_type: str, profile_id: str, display_name: str | None = None) -> bytes:
        label_source = display_name or profile_id or "--"
        label = html.escape(label_source[-2:], quote=True)
        title = html.escape(f"{profile_type}:{profile_id}", quote=True)
        digest = hashlib.md5(f"{profile_type}:{profile_id}".encode("utf-8")).hexdigest()
        color = f"#{digest[:6]}"
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96">'
            f"<title>{title}</title>"
            f'<rect width="96" height="96" rx="20" fill="{color}"/>'
            '<circle cx="48" cy="35" r="16" fill="#ffffff" opacity=".9"/>'
            '<path d="M20 82c4-16 15-26 28-26s24 10 28 26" fill="#ffffff" opacity=".9"/>'
            f'<text x="48" y="91" text-anchor="middle" fill="#ffffff" font-size="12" font-family="Arial, sans-serif">{label}</text>'
            "</svg>"
        ).encode("utf-8")

    @staticmethod
    async def save_placeholder_avatar(
        db: AsyncSession,
        *,
        profile_type: str,
        profile_id: str,
        display_name: str | None = None,
        storage_root: str | Path | None = None,
        public_prefix: str | None = None,
    ) -> str:
        return await MessageService.save_media_asset(
            db,
            file_content=ProfilePlaceholderService._placeholder_svg(profile_type, profile_id, display_name),
            file_type="image",
            ext="svg",
            storage_root=storage_root,
            public_prefix=public_prefix,
        )
