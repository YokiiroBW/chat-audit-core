from __future__ import annotations

import re
from pathlib import Path

from collector import __version__


ROOT = Path(__file__).resolve().parents[1]


def test_windows_packaging_files_define_upgrade_startup_and_data_retention():
    spec = (ROOT / "collector" / "packaging" / "chat_audit_qq_collector.spec").read_text(encoding="utf-8")
    gui_spec = (ROOT / "collector" / "packaging" / "chat_audit_qq_collector_gui.spec").read_text(
        encoding="utf-8"
    )
    installer = (ROOT / "collector" / "packaging" / "installer.iss").read_text(encoding="utf-8")
    build_script = (ROOT / "scripts" / "build_collector.ps1").read_text(encoding="utf-8")
    instructions = (ROOT / "collector" / "WINDOWS_INSTALL.md").read_text(encoding="utf-8")

    assert 'name="chat-audit-qq-collector"' in spec
    assert 'name="chat-audit-qq-collector-gui"' in gui_spec
    assert "console=False" in gui_spec
    assert 'collect_submodules("pystray")' in gui_spec
    assert "AppId={{" in installer
    assert f'#define AppVersion "{__version__}"' in installer
    assert "{userstartup}" in installer
    assert "chat-audit-qq-collector-gui.exe" in installer
    assert "--start-hidden" in installer
    assert "UninstallSilent" in installer
    assert "PyInstaller" in build_script
    assert "ISCC.exe" in build_script
    assert "chat-audit-collector-tcl" in build_script
    assert "%LOCALAPPDATA%\\ChatAuditQQCollector" in instructions
    assert re.search(r"日志按 10 MiB\s*轮转", instructions)


def test_packaging_icon_referenced_by_both_specs_exists_and_is_a_real_ico():
    # Both PyInstaller specs name this file; a missing or malformed one fails
    # the build only at package time, which is the worst place to find out.
    from pathlib import Path

    icon = Path("collector/packaging/chat-audit-collector.ico")
    assert icon.is_file()

    raw = icon.read_bytes()
    # ICONDIR: reserved 0, type 1 (icon), then the image count.
    assert raw[:4] == b"\x00\x00\x01\x00"
    image_count = int.from_bytes(raw[4:6], "little")
    assert image_count > 0
    assert len(raw) > 6 + image_count * 16

    for spec_name in ("chat_audit_qq_collector.spec", "chat_audit_qq_collector_gui.spec"):
        spec = (Path("collector/packaging") / spec_name).read_text(encoding="utf-8")
        assert "chat-audit-collector.ico" in spec
