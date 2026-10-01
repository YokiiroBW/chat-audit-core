"""Single source of truth for public media path prefixes.

Archived media is reachable under two routes (see ``app/api.py``): the
configured ``public_storage_prefix`` (default ``/media``) and the legacy
``/static/storage/`` prefix, kept so archives written before the media routes
were split keep resolving.

Anything that needs to answer "is this path already stored locally?" must ask
here instead of hard-coding one prefix. Hard-coding ``/static/storage/`` while
the deployment actually serves ``/media`` silently breaks avatar caching (every
list request re-downloads avatars that are already local) and makes imported
profiles keep an ``avatar_status`` of ``pending`` forever.
"""

from __future__ import annotations

import re
from functools import lru_cache

from app.config import get_settings

LEGACY_STORAGE_PREFIX = "/static/storage/"
DEFAULT_STORAGE_PREFIX = "/media/"


def normalize_storage_prefix(value: str | None) -> str:
    """Return ``value`` as a leading-and-trailing-slash prefix."""
    text = str(value or "").strip()
    if not text:
        return DEFAULT_STORAGE_PREFIX
    if not text.startswith("/"):
        text = "/" + text
    return text.rstrip("/") + "/"


def known_storage_prefixes(public_prefix: str | None = None) -> tuple[str, ...]:
    """Every prefix under which locally archived media may be addressed.

    ``public_prefix`` overrides the configured value; callers that already hold
    a ``Settings`` instance should pass it so the result cannot drift from the
    request's own configuration.
    """
    configured = normalize_storage_prefix(
        public_prefix if public_prefix is not None else get_settings().public_storage_prefix
    )
    return tuple(dict.fromkeys((configured, DEFAULT_STORAGE_PREFIX, LEGACY_STORAGE_PREFIX)))


def is_local_storage_path(value: str | None, public_prefix: str | None = None) -> bool:
    """True when ``value`` points at media this deployment already serves."""
    text = str(value or "").strip()
    return bool(text) and text.startswith(known_storage_prefixes(public_prefix))


def count_local_storage_paths(value: str | None, public_prefix: str | None = None) -> int:
    """How many locally archived media paths appear in ``value``."""
    text = value or ""
    return sum(text.count(prefix) for prefix in known_storage_prefixes(public_prefix))


@lru_cache(maxsize=8)
def _stored_media_pattern(prefixes: tuple[str, ...]) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(prefix) for prefix in prefixes)
    return re.compile(rf"(?:{alternatives})[a-f0-9]{{32}}\.[a-z0-9]+", re.IGNORECASE)


def stored_media_pattern(public_prefix: str | None = None) -> re.Pattern[str]:
    """Regex matching a hashed media file under any known storage prefix."""
    return _stored_media_pattern(known_storage_prefixes(public_prefix))
