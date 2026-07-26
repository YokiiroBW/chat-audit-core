from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "app" / "static" / "assets"


def minify_js(source: str) -> str:
    lines = []
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        lines.append(stripped)
    return "\n".join(lines) + "\n"


def minify_css(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    source = re.sub(r"\s+", " ", source)
    source = re.sub(r"\s*([{}:;,>+~])\s*", r"\1", source)
    source = source.replace(";}", "}")
    return source.strip() + "\n"


def _expected_assets() -> dict[Path, str]:
    js_source = (ASSET_DIR / "app.js").read_text(encoding="utf-8")
    css_source = (ASSET_DIR / "app.css").read_text(encoding="utf-8")
    return {
        ASSET_DIR / "app.min.js": minify_js(js_source),
        ASSET_DIR / "app.min.css": minify_css(css_source),
    }


def write_minified_assets() -> None:
    for path, content in _expected_assets().items():
        path.write_text(content, encoding="utf-8")


def check_minified_assets() -> list[str]:
    """Return a problem description per minified asset that is missing or stale.

    Comparison goes through text mode, so a checkout using CRLF and a build host
    using LF still agree; only real content differences are reported.
    """
    problems: list[str] = []
    for path, expected in _expected_assets().items():
        if not path.exists():
            problems.append(f"{path.relative_to(ROOT)} is missing")
            continue
        if path.read_text(encoding="utf-8") != expected:
            problems.append(f"{path.relative_to(ROOT)} is stale; regenerate it")
    return problems


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate the minified web console assets.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the committed assets match their sources instead of rewriting them.",
    )
    args = parser.parse_args()
    if args.check:
        issues = check_minified_assets()
        for issue in issues:
            print(f"minified asset out of date: {issue}", file=sys.stderr)
        raise SystemExit(1 if issues else 0)
    write_minified_assets()
