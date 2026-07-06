from __future__ import annotations

import re
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


def write_minified_assets() -> None:
    js_source = (ASSET_DIR / "app.js").read_text(encoding="utf-8")
    css_source = (ASSET_DIR / "app.css").read_text(encoding="utf-8")

    (ASSET_DIR / "app.min.js").write_text(minify_js(js_source), encoding="utf-8")
    (ASSET_DIR / "app.min.css").write_text(minify_css(css_source), encoding="utf-8")


if __name__ == "__main__":
    write_minified_assets()
