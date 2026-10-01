"""Containment checks for paths that came out of the QQ database.

Media paths in a QQNT row are data, not configuration: whatever string sits in
``local_path`` is what the collector would open and upload. Without a
containment check a row pointing anywhere on the disk -- a key file, a browser
profile -- reads as a perfectly valid media reference.

The server has the same rule for its own storage root in ``app/api.py``; the
collector ships as a separate PyInstaller bundle that cannot import ``app``, so
this is a deliberate second copy. Change one, change the other.
"""

from __future__ import annotations

from pathlib import Path


def path_within(root: Path, candidate: Path) -> bool:
    """True when ``candidate`` is ``root`` itself or sits underneath it.

    Both sides are resolved first, so a symlink pointing out of the tree cannot
    be used to step outside it.
    """
    try:
        resolved_root = root.expanduser().resolve()
        resolved_candidate = candidate.expanduser().resolve()
    except OSError:
        return False
    return resolved_candidate == resolved_root or resolved_root in resolved_candidate.parents


def within_any(roots: tuple[Path, ...], candidate: Path) -> bool:
    """True when ``candidate`` is inside at least one of ``roots``.

    An empty ``roots`` means no root is known, and that answers False: refusing
    to read is the safe outcome when there is nothing to check against.
    """
    return any(path_within(root, candidate) for root in roots)
