"""Bind the local history wire implementation to the coordinator's frozen pack."""
import hashlib
import json
from pathlib import Path

MANIFEST_SHA256 = "4395f4207ef4bcaacb45522213aa622c5603d79006c29b48b5ff350525082ef4"


def history_contract_ready():
    root = Path(__file__).with_name("chat_history_contract")
    try:
        manifest_raw = (root / "manifest.json").read_bytes().replace(b"\r\n", b"\n")
        if hashlib.sha256(manifest_raw).hexdigest() != MANIFEST_SHA256:
            return False
        manifest = json.loads(manifest_raw)
        if set(manifest["sha256"]) != {"schema.json", "examples.json", "README.md"}:
            return False
        for name, digest in manifest["sha256"].items():
            raw = (root / name).read_bytes().replace(b"\r\n", b"\n")
            if hashlib.sha256(raw).hexdigest() != digest:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False
