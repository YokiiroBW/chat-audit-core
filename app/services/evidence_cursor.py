"""Opaque keyset positions bound to the currently authorized reader and query."""
import base64
import hashlib
import hmac
import json
import re


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _encode(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def query_binding(scope_digest, service_id, filters, limit):
    return hashlib.sha256(_canonical({"scope": scope_digest, "service": service_id,
                                     "filters": filters, "limit": limit})).hexdigest()


def cursor_signing_key(server_secret, token_sha256):
    # Reuse the application's existing server secret, not the caller's bearer
    # token as a forgeable signing secret. Rotation of either invalidates cursors.
    return hmac.digest(server_secret.encode(), b"chat-history/cursor/v1\0" + bytes.fromhex(token_sha256), "sha256")


def encode_cursor(key, binding, signing_key):
    raw = _canonical({"version": 1, "binding": binding, "before": list(key)})
    signature = hmac.digest(signing_key, raw, "sha256")
    return _encode(raw) + "." + _encode(signature)


def decode_cursor(cursor, binding, signing_key):
    try:
        if len(cursor) > 2048 or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", cursor):
            raise ValueError
        encoded, signed = cursor.split(".")
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        signature = base64.b64decode(signed + "=" * (-len(signed) % 4), altchars=b"-_", validate=True)
        if not hmac.compare_digest(signature, hmac.digest(signing_key, raw, "sha256")):
            raise ValueError
        value = json.loads(raw)
        key = value["before"]
        if (set(value) != {"version", "binding", "before"} or value["version"] != 1 or
                value["binding"] != binding or not isinstance(key, list) or len(key) != 3 or
                any(type(item) is not int for item in key[:2]) or
                not isinstance(key[2], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key[2])):
            raise ValueError
        return tuple(key)
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise ValueError("Invalid archive cursor") from exc
