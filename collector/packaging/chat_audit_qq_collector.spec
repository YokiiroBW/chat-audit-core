from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


project_root = Path(SPECPATH).parents[1]
hidden_imports = collect_submodules("httpx") + collect_submodules("sqlcipher3")

a = Analysis(
    [str(project_root / "collector" / "__main__.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[
        (
            str(project_root / "collector" / "collector.example.toml"),
            "collector",
        )
    ],
    hiddenimports=hidden_imports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["app", "tests"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    icon=str(project_root / "collector" / "packaging" / "chat-audit-collector.ico"),
    name="chat-audit-qq-collector",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)
