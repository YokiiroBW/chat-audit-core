from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any


QQNT_HEADER_SIZE = 1024
QQNT_DATABASE_KEY_NAME = "qqnt_database_key"
SQLITE_PLAINTEXT_MAGIC = b"SQLite format 3\x00"


def body_looks_encrypted(source: str | Path, header_size: int = QQNT_HEADER_SIZE) -> bool:
    """Whether the bytes behind the QQNT custom header can plausibly be SQLCipher ciphertext.

    A SQLCipher body opens with a random 16-byte salt, while a plaintext SQLite body opens
    with the well-known magic string. This is deliberately cheap — no copy, no decryption and
    no key — because it runs on every compatibility check. It cannot prove the key is right;
    only actually opening a stripped copy can do that.
    """
    path = Path(source).expanduser().resolve()
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size <= header_size:
        return False
    with path.open("rb") as handle:
        handle.seek(header_size)
        prefix = handle.read(len(SQLITE_PLAINTEXT_MAGIC))
    return len(prefix) == len(SQLITE_PLAINTEXT_MAGIC) and prefix != SQLITE_PLAINTEXT_MAGIC


def load_sqlcipher() -> Any:
    try:
        import sqlcipher3.dbapi2 as sqlcipher
    except ImportError as exc:
        raise RuntimeError("sqlcipher3 is required for QQNT encrypted databases") from exc
    return sqlcipher


def sqlcipher_available() -> bool:
    try:
        load_sqlcipher()
    except RuntimeError:
        return False
    return True


def materialize_clear_database(source: str | Path, destination: str | Path) -> Path:
    source_path = Path(source).expanduser().resolve()
    destination_path = Path(destination).expanduser().resolve()
    if not source_path.is_file():
        raise RuntimeError("QQNT database snapshot does not exist")
    if source_path.stat().st_size <= QQNT_HEADER_SIZE:
        raise RuntimeError("QQNT database is smaller than its custom header")
    expected_size = source_path.stat().st_size - QQNT_HEADER_SIZE
    if destination_path.is_file() and destination_path.stat().st_size == expected_size:
        return destination_path

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination_path.with_suffix(destination_path.suffix + ".tmp")
    try:
        with source_path.open("rb") as source_file, temporary.open("wb") as output_file:
            source_file.seek(QQNT_HEADER_SIZE)
            shutil.copyfileobj(source_file, output_file, length=64 * 1024 * 1024)
        if temporary.stat().st_size != expected_size:
            raise RuntimeError("QQNT database header stripping produced an invalid size")
        os.replace(temporary, destination_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination_path


def configure_connection(connection: Any, key: str) -> None:
    if len(key.encode("utf-8")) != 16:
        raise ValueError("QQNT database key must be exactly 16 UTF-8 bytes")
    safe_key = key.replace("'", "''")
    connection.execute("PRAGMA cipher_page_size = 4096")
    connection.execute(f"PRAGMA key = '{safe_key}'")
    connection.execute("PRAGMA kdf_iter = 4000")
    connection.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA1")
    connection.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512")
