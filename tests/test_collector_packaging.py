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
