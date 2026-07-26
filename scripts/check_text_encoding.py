"""Guard the repository against mojibake in tracked text files.

The repository once lost every CJK character in several files at once: one write
went through an ASCII codec with ``errors="replace"``, so each character became a
literal question mark. That damage is irreversible -- the bytes are gone -- so
the only real defence is refusing to let it in again.

Checks performed per tracked text file:

1. the bytes decode as UTF-8;
2. no U+FFFD replacement character;
3. no UTF-8-read-as-latin-1 signature, and no classic GBK garbage sequence;
4. no run of two or more consecutive question marks.

Rule 4 is the one that catches the historical damage. A single question mark is
deliberately *not* reported: SQL placeholders (``",".join("?" ...)``), URL query
separators and regex character classes all use one, so a single-character rule
drowns in false positives. The cost is that a lone mangled character (a one-CJK
string collapsing to one question mark) still has to be caught in review.

The JavaScript nullish-coalescing operator is exempted, but only in ``.js``
files. Markdown prose can contain the same character pair -- ``16``, two
question marks, ``ASCII`` appears in a real README -- and must stay reportable.

This file is deliberately pure ASCII: every non-ASCII marker is built with
``chr()`` so that the guard neither trips its own checks nor can be silently
damaged by the very bug it exists to catch.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# Runtime code that legitimately mentions the replacement character while
# validating decoded payloads, rather than containing damaged text itself.
REPLACEMENT_CHAR_ALLOWLIST = {"collector/parsers/protobuf.py"}

SKIPPED_DIRECTORIES = {".git", ".venv", ".tmp", "node_modules", "__pycache__", "dist", "build"}

REPLACEMENT_CHAR = chr(0xFFFD)
# UTF-8 bytes read back as latin-1 leave a lead byte in U+00C2-U+00C5 or
# U+00E0-U+00EF followed by a continuation byte in U+0080-U+00BF.
LATIN1_MOJIBAKE = re.compile("[%c-%c%c-%c][%c-%c]" % (0xC2, 0xC5, 0xE0, 0xEF, 0x80, 0xBF))
# The canonical GBK-through-the-wrong-codec sequence.
GBK_MOJIBAKE = "".join(map(chr, (0x951F, 0x65A4, 0x62F7)))
REPEATED_QUESTION_MARKS = re.compile("[?]{2,}")
# The nullish-coalescing operator, optionally with an assignment. The leading
# space is what separates it from damaged text, which never has a space before
# its first question mark.
JS_NULLISH = re.compile(" [?][?]=? ")


def _tracked_files() -> list[Path]:
    """Tracked files, or a filesystem walk when git is unavailable."""
    try:
        listing = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout.decode("utf-8")
    except (OSError, subprocess.CalledProcessError):
        return [
            path
            for path in sorted(ROOT.rglob("*"))
            if path.is_file() and not SKIPPED_DIRECTORIES.intersection(path.relative_to(ROOT).parts)
        ]
    return [ROOT / name for name in listing.split("\0") if name]


def _read_text(path: Path) -> tuple[str | None, str | None]:
    """Return (text, problem). Binary files yield (None, None) and are skipped."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"cannot be read ({exc})"
    if b"\0" in raw:
        return None, None
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError as exc:
        return None, f"is not valid UTF-8 ({exc.reason} at byte {exc.start})"


def _line_problems(relative: str, text: str) -> list[str]:
    problems: list[str] = []
    is_javascript = relative.endswith((".js", ".mjs", ".cjs"))
    allows_replacement_char = relative in REPLACEMENT_CHAR_ALLOWLIST
    for number, line in enumerate(text.splitlines(), start=1):
        location = f"{relative}:{number}"
        if REPLACEMENT_CHAR in line and not allows_replacement_char:
            problems.append(f"{location} contains a U+FFFD replacement character")
        if GBK_MOJIBAKE in line or LATIN1_MOJIBAKE.search(line):
            problems.append(f"{location} looks decoded with the wrong codec: {line.strip()!r}")
        candidate = JS_NULLISH.sub(" ", line) if is_javascript else line
        if REPEATED_QUESTION_MARKS.search(candidate):
            problems.append(f"{location} has consecutive question marks where text was lost: {line.strip()!r}")
    return problems


def check_text_encoding() -> list[str]:
    """Return a problem description per damaged line, empty when the tree is clean."""
    problems: list[str] = []
    for path in _tracked_files():
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT).as_posix()
        text, failure = _read_text(path)
        if failure is not None:
            problems.append(f"{relative} {failure}")
            continue
        if text is None:
            continue
        problems.extend(_line_problems(relative, text))
    return problems


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify tracked text files are free of mojibake.")
    parser.parse_args()
    issues = check_text_encoding()
    for issue in issues:
        print(f"damaged text: {issue}", file=sys.stderr)
    raise SystemExit(1 if issues else 0)
