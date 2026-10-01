import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SystemSetting
from app.services.backup_service import BackupService

BACKUP_CRON_KEY = "backup.auto_backup_cron"
BACKUP_KEEP_LATEST_KEY = "backup.auto_backup_keep_latest"
DISABLED_CRON_VALUES = {"", "off", "disabled", "none", "false", "0"}


@dataclass(frozen=True)
class EffectiveBackupConfig:
    cron: str
    keep_latest: int
    cron_source: str
    keep_latest_source: str

    @property
    def enabled(self) -> bool:
        return self.cron.strip().lower() not in DISABLED_CRON_VALUES

    @property
    def config_source(self) -> str:
        return "database" if "database" in {self.cron_source, self.keep_latest_source} else "env"


class BackupConfigService:
    @staticmethod
    async def get_effective_config(db: AsyncSession, settings: Any) -> EffectiveBackupConfig:
        result = await db.execute(select(SystemSetting).where(SystemSetting.key.in_([BACKUP_CRON_KEY, BACKUP_KEEP_LATEST_KEY])))
        records = {record.key: record for record in result.scalars().all()}
        cron = str(getattr(settings, "auto_backup_cron", "") or "")
        keep_latest = int(getattr(settings, "auto_backup_keep_latest", 7))
        cron_source = "env"
        keep_latest_source = "env"

        if BACKUP_CRON_KEY in records:
            cron = str(json.loads(records[BACKUP_CRON_KEY].value_json))
            cron_source = "database"
        if BACKUP_KEEP_LATEST_KEY in records:
            keep_latest = int(json.loads(records[BACKUP_KEEP_LATEST_KEY].value_json))
            keep_latest_source = "database"

        return EffectiveBackupConfig(
            cron=cron,
            keep_latest=keep_latest,
            cron_source=cron_source,
            keep_latest_source=keep_latest_source,
        )

    @staticmethod
    def validate_cron(cron: str) -> str:
        normalized = cron.strip()
        if normalized.lower() in DISABLED_CRON_VALUES:
            return normalized
        BackupService.next_run_from_cron(normalized)
        return normalized

    @staticmethod
    async def update_config(
        db: AsyncSession,
        settings: Any,
        *,
        cron: str | None = None,
        keep_latest: int | None = None,
        reset_to_env: bool = False,
    ) -> EffectiveBackupConfig:
        if reset_to_env:
            await db.execute(delete(SystemSetting).where(SystemSetting.key.in_([BACKUP_CRON_KEY, BACKUP_KEEP_LATEST_KEY])))
            await db.commit()
            return await BackupConfigService.get_effective_config(db, settings)

        changed = False
        if cron is not None:
            await BackupConfigService._upsert_setting(db, BACKUP_CRON_KEY, BackupConfigService.validate_cron(cron))
            changed = True
        if keep_latest is not None:
            await BackupConfigService._upsert_setting(db, BACKUP_KEEP_LATEST_KEY, int(keep_latest))
            changed = True
        if changed:
            await db.commit()
        return await BackupConfigService.get_effective_config(db, settings)

    @staticmethod
    async def _upsert_setting(db: AsyncSession, key: str, value: Any) -> None:
        record = await db.get(SystemSetting, key)
        value_json = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if record is None:
            db.add(SystemSetting(key=key, value_json=value_json))
            return
        record.value_json = value_json
