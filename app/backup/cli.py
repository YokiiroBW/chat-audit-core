from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.backup.archive import validate_backup_archive
from app.backup.jobs import enqueue_backup_job, get_backup_job
from app.backup.legacy import convert_legacy_backup_to_v4
from app.backup.restore import restore_backup_archive
from app.backup.worker import _install_process_memory_limit
from app.config import get_settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="chat-audit-core bounded backup utility")
    subparsers = parser.add_subparsers(dest="command", required=True)

    enqueue = subparsers.add_parser("enqueue", help="queue a full backup for the isolated worker")
    enqueue.add_argument("--type", choices=("manual", "auto"), default="manual")
    enqueue.add_argument("--created-by", default="backup_cli")
    enqueue.add_argument("--keep-latest", type=int)

    status = subparsers.add_parser("status", help="read one persisted backup job status")
    status.add_argument("job_id")

    validate = subparsers.add_parser("validate", help="stream-validate a v4 archive")
    validate.add_argument("archive", type=Path)

    restore = subparsers.add_parser("restore", help="restore a v4 archive into an empty target")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--database-url", required=True)
    restore.add_argument("--storage-root", type=Path, required=True)
    restore.add_argument("--work-root", type=Path)

    convert = subparsers.add_parser("convert-legacy", help="stream-convert a v1-v3 JSON/gzip package to v4")
    convert.add_argument("archive", type=Path)
    convert.add_argument("--require-legacy-signature", action="store_true")
    convert.add_argument("--filename")
    return parser


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    if args.command == "enqueue":
        status = enqueue_backup_job(
            settings.backup_root,
            backup_type=args.type,
            created_by=args.created_by,
            keep_latest=settings.auto_backup_keep_latest if args.keep_latest is None else args.keep_latest,
        )
        _print(status)
        return 0
    if args.command == "status":
        status = get_backup_job(settings.backup_root, args.job_id)
        if status is None:
            _print({"error": "backup job not found", "job_id": args.job_id})
            return 1
        _print(status)
        return 0

    _install_process_memory_limit(settings)
    if args.command == "validate":
        report = validate_backup_archive(
            args.archive,
            signing_key=settings.app_secret_key,
            require_signature=True,
        )
        _print(
            {
                "valid": report.valid,
                "schema": report.manifest.get("schema"),
                "counts": report.counts,
                "members": report.members,
                "uncompressed_bytes": report.uncompressed_bytes,
                "checksum_valid": report.checksum_valid,
                "signature_valid": report.signature_valid,
            }
        )
        return 0
    if args.command == "convert-legacy":
        result = convert_legacy_backup_to_v4(
            args.archive,
            backup_root=settings.backup_root,
            signing_key=settings.app_secret_key,
            system_id=settings.system_instance_id,
            require_legacy_signature=args.require_legacy_signature,
            chunk_bytes=settings.backup_chunk_bytes,
            filename=args.filename,
        )
        _print(
            {
                "path": str(result.path),
                "filename": result.path.name,
                "size_bytes": result.path.stat().st_size,
                "counts": result.manifest.get("counts", {}),
                "legacy": result.manifest.get("legacy", {}),
            }
        )
        return 0
    if args.command == "restore":
        result = asyncio.run(
            restore_backup_archive(
                args.archive,
                database_url=args.database_url,
                storage_root=args.storage_root,
                public_storage_prefix=settings.public_storage_prefix,
                signing_key=settings.app_secret_key,
                work_root=args.work_root,
            )
        )
        _print(
            {
                "counts": result.counts,
                "media_files_created": result.media_files_created,
                "media_files_reused": result.media_files_reused,
                "checksum_valid": result.validation.checksum_valid,
                "signature_valid": result.validation.signature_valid,
            }
        )
        return 0
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        _print({"error": f"{type(exc).__name__}: {exc}"})
        sys.exit(1)
