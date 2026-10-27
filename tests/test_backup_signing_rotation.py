"""Synthetic content-pinned compatibility across backup entry points."""

import copy
import gzip
import hashlib
import io
import json
import tarfile

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.backup import validate_backup_archive as public_validate_archive
from app.backup.archive import ArchiveValidationError, BackupArchiveWriter, validate_backup_archive
from app.backup.legacy import LEGACY_SECTIONS, convert_legacy_backup_to_v4, fingerprint_legacy_backup
from app.backup.restore import restore_backup_archive
from app.backup.trust import (
    BACKUP_TRUST_SCHEMA,
    MAX_REGISTRY_BYTES,
    BackupVerificationPolicy,
    backup_content_fingerprint,
    load_backup_verification_policy,
)
from app.config import Settings, get_settings
from app.database import get_db_session
from app.models import Message
from app.services.backup_service import BackupService
from app.services.evidence_cursor import cursor_signing_key, decode_cursor, encode_cursor


OLD_KEY = "synthetic-pre-rotation-key"
NEW_KEY = "synthetic-post-rotation-key"


def _legacy_package(version=3):
    package = {section: [] for section in (*LEGACY_SECTIONS, "media_files")}
    package["manifest"] = {"schema": f"chat-audit-core.backup.v{version}", "created_at": "2020-01-01T00:00:00Z"}
    package["messages"] = [{
        "msg_hash": "synthetic-message",
        "platform": "qq",
        "room_id": "synthetic-room",
        "message_type": "group",
        "sender_id": "synthetic-user",
        "raw_message": "历史合成夹具",
        "local_message": "历史合成夹具",
        "timestamp": 1,
    }]
    return BackupService.attach_package_integrity(package, system_id="synthetic-instance", signing_key=OLD_KEY)


def _legacy_pin(package):
    return backup_content_fingerprint(
        "legacy-json", BackupService.calculate_package_checksum(package), package["manifest"]
    )


def _registry(tmp_path, pins):
    path = tmp_path / "trust.json"
    path.write_text(json.dumps({
        "schema": BACKUP_TRUST_SCHEMA,
        "historical_keys": {"previous": OLD_KEY},
        "trusted_backups": {pin: "previous" for pin in pins},
    }), encoding="utf-8")
    return path, load_backup_verification_policy(path)


def _write_v4(path, *, key=OLD_KEY, text="历史合成夹具", metadata=None, key_id="synthetic-instance"):
    package = _legacy_package()
    package["messages"][0]["raw_message"] = text
    package["messages"][0]["local_message"] = text
    with BackupArchiveWriter(
        final_path=path,
        metadata=metadata or {"created_at": "2020-01-01T00:00:00Z"},
        signing_key=key,
        key_id=key_id,
        min_free_bytes=0,
    ) as writer:
        for section in LEGACY_SECTIONS:
            with writer.section(section) as output:
                for item in package[section]:
                    output.write(item)
            writer.set_count(section, len(package[section]))
        writer.set_count("media_files", 0)
        writer.set_count("missing_media_files", 0)
        writer.finalize()
    return validate_backup_archive(path, signing_key=key)


@pytest.mark.parametrize("version", [1, 2, 3])
@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.asyncio
async def test_registered_legacy_core_import_conversion_and_restore(tmp_path, db_session, version, compressed):
    package = _legacy_package(version)
    path, policy = _registry(tmp_path, [_legacy_pin(package)])
    # Object key order/whitespace can change during browser JSON.parse/stringify.
    reordered = json.loads(json.dumps(package, ensure_ascii=False, sort_keys=True, indent=2))
    raw = json.dumps(reordered, ensure_ascii=False, indent=4).encode()
    source = tmp_path / ("historical.json.gz" if compressed else "historical.json")
    source.write_bytes(gzip.compress(raw) if compressed else raw)
    assert fingerprint_legacy_backup(source, signing_key=NEW_KEY, verification_policy=policy) == _legacy_pin(package)
    assert not BackupService.validate_import_package(reordered, signing_key=NEW_KEY)["valid"]
    report = await BackupService.preview_import_package(
        db_session, reordered, signing_key=NEW_KEY, require_signature=True, verification_policy=policy
    )
    assert report["valid"] and report["signature_valid"] is True
    await BackupService.import_package(
        db_session, reordered, signing_key=NEW_KEY, require_signature=True, verification_policy=policy
    )
    assert (await db_session.execute(select(Message))).scalar_one().raw_message == "历史合成夹具"
    converted = convert_legacy_backup_to_v4(
        source, backup_root=tmp_path / "converted", signing_key=NEW_KEY, system_id="new-instance",
        require_legacy_signature=True, verification_policy=policy,
    )
    assert validate_backup_archive(converted.path, signing_key=NEW_KEY).signature_valid
    with pytest.raises(ArchiveValidationError, match="signature"):
        validate_backup_archive(converted.path, signing_key=OLD_KEY, verification_policy=policy)
    restored = await restore_backup_archive(
        converted.path, database_url=f"sqlite+aiosqlite:///{tmp_path / 'restore.sqlite3'}",
        storage_root=tmp_path / "restore-media", public_storage_prefix="/media", signing_key=NEW_KEY,
    )
    assert restored.counts["messages"] == 1
    assert path.exists() and source.exists()


@pytest.mark.parametrize("change", ["payload", "created_at", "key_id", "signature_metadata", "checksum_metadata"])
def test_unregistered_legacy_resigned_content_is_rejected(tmp_path, change):
    original = _legacy_package()
    _path, policy = _registry(tmp_path, [_legacy_pin(original)])
    forged = copy.deepcopy(original)
    if change == "payload":
        forged["messages"][0]["raw_message"] = "forged"
    elif change == "created_at":
        forged["manifest"]["created_at"] = "1990-01-01T00:00:00Z"
    elif change == "key_id":
        forged["manifest"]["signature"]["key_id"] = "previous"
    else:
        forged["manifest"]["signature" if change == "signature_metadata" else "checksum"]["extra"] = "forged"
    # The attacker knows the old key and recomputes both unkeyed/keyed integrity.
    forged["manifest"]["checksum"]["value"] = BackupService.calculate_package_checksum(forged)
    forged["manifest"]["signature"]["value"] = BackupService.calculate_package_signature(forged, OLD_KEY)
    report = BackupService.validate_import_package(forged, signing_key=NEW_KEY, verification_policy=policy)
    assert report["valid"] is False and report["signature_valid"] is False
    source = tmp_path / "forged.json"
    source.write_text(json.dumps(forged), encoding="utf-8")
    with pytest.raises(ValueError, match="signature mismatch"):
        convert_legacy_backup_to_v4(
            source, backup_root=tmp_path / "converted", signing_key=NEW_KEY,
            system_id="new-instance", verification_policy=policy,
        )
    assert not list(tmp_path.glob("converted/*.cacb"))


@pytest.mark.asyncio
async def test_registered_v4_alias_and_restore_and_unregistered_old_key_rejected(tmp_path):
    original = tmp_path / "original.cacb"
    report = _write_v4(original)
    _path, policy = _registry(tmp_path, [report.content_fingerprint])
    assert public_validate_archive(original, signing_key=NEW_KEY, verification_policy=policy).signature_valid
    wrong_key_policy = BackupVerificationPolicy({"wrong": NEW_KEY}, {report.content_fingerprint: "wrong"})
    with pytest.raises(ArchiveValidationError, match="signature"):
        validate_backup_archive(original, signing_key=NEW_KEY, verification_policy=wrong_key_policy)
    restored = await restore_backup_archive(
        original, database_url=f"sqlite+aiosqlite:///{tmp_path / 'restore.sqlite3'}",
        storage_root=tmp_path / "media", public_storage_prefix="/media", signing_key=NEW_KEY,
        verification_policy=policy,
    )
    assert restored.counts["messages"] == 1
    for name, changes in (
        ("payload", {"text": "old-key-forgery"}),
        ("time", {"metadata": {"created_at": "1990-01-01T00:00:00Z"}}),
        ("kid", {"key_id": "previous"}),
    ):
        forged = tmp_path / f"{name}.cacb"
        _write_v4(forged, **changes)
        with pytest.raises(ArchiveValidationError, match="signature"):
            validate_backup_archive(forged, signing_key=NEW_KEY, verification_policy=policy)
    # The outer signature's metadata is excluded from its HMAC envelope, so
    # only the full-manifest content pin detects this otherwise valid change.
    metadata_forgery = tmp_path / "signature-metadata.cacb"
    with tarfile.open(original, "r:gz") as source, tarfile.open(metadata_forgery, "w:gz") as destination:
        for member in source:
            content = source.extractfile(member).read()
            if member.name == "manifest.json":
                manifest = json.loads(content)
                manifest["integrity"]["signature"]["key_id"] = "previous"
                content = json.dumps(manifest).encode()
            member.size = len(content)
            destination.addfile(member, io.BytesIO(content))
    assert validate_backup_archive(metadata_forgery, signing_key=OLD_KEY).signature_valid
    with pytest.raises(ArchiveValidationError, match="signature"):
        validate_backup_archive(metadata_forgery, signing_key=NEW_KEY, verification_policy=policy)
    current = tmp_path / "current.cacb"
    _write_v4(current, key=NEW_KEY)
    assert validate_backup_archive(current, signing_key=NEW_KEY, verification_policy=policy).signature_valid


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.asyncio
async def test_http_historical_import_and_current_only_export(tmp_path, db_session, compressed):
    from app.main import create_app

    package = _legacy_package()
    registry, _policy = _registry(tmp_path, [_legacy_pin(package)])
    settings = Settings(
        app_secret_key=NEW_KEY, backup_trust_registry=str(registry),
        storage_root=tmp_path / "storage", backup_root=tmp_path / "backups",
    )
    application = create_app(settings=settings)

    async def sessions():
        yield db_session

    application.dependency_overrides[get_db_session] = sessions
    application.dependency_overrides[get_settings] = lambda: settings
    raw = json.dumps(package, sort_keys=True, ensure_ascii=False).encode()
    content = gzip.compress(raw) if compressed else raw
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
        preview = await client.post("/api/import/validate", content=content)
        assert preview.status_code == 200 and preview.json()["signature_valid"] is True
        imported = await client.post("/api/import", content=content)
        assert imported.status_code == 200
        forged = _legacy_package()
        forged["manifest"]["signature"]["key_id"] = "previous"
        rejected = await client.post("/api/import", json=forged)
        assert rejected.status_code == 400 and "signature mismatch" in rejected.json()["detail"]
        exported = await client.get("/api/export")
        assert exported.status_code == 200
        BackupService.validate_package_signature(exported.json(), NEW_KEY)
        with pytest.raises(ValueError, match="signature mismatch"):
            BackupService.validate_package_signature(exported.json(), OLD_KEY, verification_policy=_policy)


@pytest.mark.parametrize("invalid", ["missing", "schema", "duplicate", "dangling", "oversized"])
def test_invalid_registry_preserves_current_verification_and_redacts_diagnostic(tmp_path, caplog, invalid):
    path = tmp_path / "invalid.json"
    if invalid == "schema":
        path.write_text(json.dumps({"schema": OLD_KEY}), encoding="utf-8")
    elif invalid == "duplicate":
        path.write_text('{"schema":"' + OLD_KEY + '","schema":"duplicate"}', encoding="utf-8")
    elif invalid == "dangling":
        path.write_text(json.dumps({"schema": BACKUP_TRUST_SCHEMA, "historical_keys": {},
                                    "trusted_backups": {"0" * 64: OLD_KEY}}), encoding="utf-8")
    elif invalid == "oversized":
        path.write_bytes(b" " * (MAX_REGISTRY_BYTES + 1))
    policy = load_backup_verification_policy(path)
    assert not policy.historical_keys and not policy.trusted_backups
    assert "historical verification disabled" in caplog.text and OLD_KEY not in caplog.text
    package = _legacy_package()
    assert not BackupService.validate_import_package(package, signing_key=NEW_KEY, verification_policy=policy)["valid"]
    BackupService.attach_package_integrity(package, system_id="new-instance", signing_key=NEW_KEY)
    assert BackupService.validate_import_package(package, signing_key=NEW_KEY, verification_policy=policy)["signature_valid"]


def test_unsigned_semantics_and_cursor_rotation_remain_independent(tmp_path):
    signed = _legacy_package()
    _path, policy = _registry(tmp_path, [_legacy_pin(signed)])
    wrong_key_policy = BackupVerificationPolicy({"wrong": NEW_KEY}, {_legacy_pin(signed): "wrong"})
    assert BackupService.validate_import_package(
        signed, signing_key=NEW_KEY, verification_policy=wrong_key_policy
    )["signature_valid"] is False
    unsigned = copy.deepcopy(signed)
    unsigned["manifest"].pop("signature")
    unsigned["manifest"].pop("checksum")
    report = BackupService.validate_import_package(unsigned, signing_key=NEW_KEY, verification_policy=policy)
    assert report["valid"] and report["signature_valid"] is None
    assert not BackupService.validate_import_package(
        unsigned, signing_key=NEW_KEY, require_signature=True, verification_policy=policy
    )["valid"]
    source = tmp_path / "unsigned.json"
    source.write_text(json.dumps(unsigned), encoding="utf-8")
    with pytest.raises(ValueError, match="checksum|signature"):
        fingerprint_legacy_backup(source, signing_key=NEW_KEY, verification_policy=policy)
    token_hash = hashlib.sha256(b"synthetic-reader").hexdigest()
    old_cursor = encode_cursor((1, 2, "message"), "binding", cursor_signing_key(OLD_KEY, token_hash))
    with pytest.raises(ValueError, match="Invalid archive cursor"):
        decode_cursor(old_cursor, "binding", cursor_signing_key(NEW_KEY, token_hash))
    new_cursor = encode_cursor((1, 2, "message"), "binding", cursor_signing_key(NEW_KEY, token_hash))
    assert decode_cursor(new_cursor, "binding", cursor_signing_key(NEW_KEY, token_hash)) == (1, 2, "message")


def test_cli_pins_and_history_input_are_separate_from_conversion_output(tmp_path, monkeypatch, capsys):
    from app.backup import cli

    package = _legacy_package()
    legacy = tmp_path / "historical.json"
    legacy.write_text(json.dumps(package), encoding="utf-8")
    original = tmp_path / "original.cacb"
    report = _write_v4(original)
    registry, policy = _registry(tmp_path, [_legacy_pin(package), report.content_fingerprint])
    settings = Settings(
        app_secret_key=NEW_KEY, backup_trust_registry=str(registry), backup_root=tmp_path / "output"
    )
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    # Do not irreversibly lower the test runner's address space; dispatch and
    # validators/converter are real. The existing worker tests own RLIMIT_AS.
    monkeypatch.setattr(cli, "_install_process_memory_limit", lambda _settings: None)
    assert cli.main(["fingerprint", str(legacy), "--format", "legacy"]) == 0
    assert json.loads(capsys.readouterr().out)["content_fingerprint"] == _legacy_pin(package)
    assert cli.main(["fingerprint", str(original), "--format", "v4"]) == 0
    assert json.loads(capsys.readouterr().out)["content_fingerprint"] == report.content_fingerprint
    assert cli.main(["validate", str(original)]) == 0
    assert json.loads(capsys.readouterr().out)["signature_valid"] is True
    assert cli.main(["convert-legacy", str(legacy), "--require-legacy-signature", "--filename", "new.cacb"]) == 0
    converted = tmp_path / "output" / "new.cacb"
    assert json.loads(capsys.readouterr().out)["legacy"]["signature_valid"] is True
    assert validate_backup_archive(converted, signing_key=NEW_KEY).signature_valid
    with pytest.raises(ArchiveValidationError, match="signature"):
        validate_backup_archive(converted, signing_key=OLD_KEY, verification_policy=policy)
    assert cli.main([
        "restore", str(original), "--database-url", f"sqlite+aiosqlite:///{tmp_path / 'cli-restore.sqlite3'}",
        "--storage-root", str(tmp_path / "cli-media"),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["signature_valid"] is True
    # The utility has no registration operation and leaves trusted config intact.
    assert load_backup_verification_policy(registry).trusted_backups == policy.trusted_backups
