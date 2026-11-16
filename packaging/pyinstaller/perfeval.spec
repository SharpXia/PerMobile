# -*- mode: python ; coding: utf-8 -*-
from __future__ import annotations

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


ROOT = Path(SPECPATH).resolve().parents[1]
ENTRY_ROOT = ROOT / "packaging" / "pyinstaller" / "entries"
ICON_PATH = os.environ.get("PERFEVAL_ICON_PATH")
VERSION_METADATA = Path(os.environ["PERFEVAL_VERSION_METADATA"])
ENTITLEMENTS = ROOT / "packaging" / "macos" / "entitlements.plist"
WINDOWS_VERSION = ROOT / "packaging" / "windows" / "version_info.txt"
WINDOWS_MANIFEST = ROOT / "packaging" / "windows" / "app.manifest"

datas = collect_data_files(
    "perfeval",
    includes=[
        "fixtures/android/golden/*.txt",
        "fixtures/synthetic/golden/*.json",
        "core/report/vendor/echarts.min.js",
        "gui/resources/presets/*.json",
        "gui/resources/presets/*.yaml",
    ],
)
resource_destination = "." if sys.platform == "darwin" else "resources"
datas += [(str(VERSION_METADATA), resource_destination)]
datas += [(str(ROOT / "packaging" / "licenses" / "THIRD_PARTY_NOTICES.md"), "licenses")]
datas += [(str(ROOT / "packaging" / "licenses" / "LGPL-3.0.txt"), "licenses")]
datas += [(str(ROOT / "packaging" / "licenses" / "GPL-3.0.txt"), "licenses")]
datas += [(str(ROOT / "packaging" / "licenses" / "Apache-2.0.txt"), "licenses")]
extra_license_root = os.environ.get("PERFEVAL_EXTRA_LICENSE_DIR")
if extra_license_root:
    for license_file in Path(extra_license_root).rglob("*"):
        if license_file.is_file():
            relative_parent = license_file.relative_to(extra_license_root).parent
            datas.append((str(license_file), str(Path("licenses") / relative_parent)))

common_excludes = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickWidgets",
]

gui_analysis = Analysis(
    [str(ENTRY_ROOT / "gui_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=collect_submodules("keyring.backends"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=common_excludes,
    noarchive=False,
    optimize=1,
)


def prune_unused_qt(entries):
    blocked = (
        "qtqml",
        "qtquick",
        "qtvirtualkeyboard",
        "platforminputcontexts/libqtvirtualkeyboardplugin",
    )
    return [
        entry
        for entry in entries
        if not any(token in (str(entry[0]) + " " + str(entry[1])).casefold() for token in blocked)
    ]


gui_analysis.binaries = prune_unused_qt(gui_analysis.binaries)
gui_analysis.datas = prune_unused_qt(gui_analysis.datas)
cli_analysis = Analysis(
    [str(ENTRY_ROOT / "cli_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[*common_excludes, "PySide6"],
    noarchive=False,
    optimize=1,
)

gui_pyz = PYZ(gui_analysis.pure)
cli_pyz = PYZ(cli_analysis.pure)

gui_exe = EXE(
    gui_pyz,
    gui_analysis.scripts,
    gui_analysis.dependencies,
    [],
    name="PerfEval-Lite",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=str(ENTITLEMENTS) if sys.platform == "darwin" else None,
    icon=ICON_PATH,
    version=str(WINDOWS_VERSION) if sys.platform.startswith("win") else None,
    manifest=str(WINDOWS_MANIFEST) if sys.platform.startswith("win") else None,
    exclude_binaries=True,
    contents_directory=".",
)
cli_exe = EXE(
    cli_pyz,
    cli_analysis.scripts,
    cli_analysis.dependencies,
    [],
    name="perfeval-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=str(ENTITLEMENTS) if sys.platform == "darwin" else None,
    icon=ICON_PATH,
    version=str(WINDOWS_VERSION) if sys.platform.startswith("win") else None,
    manifest=str(WINDOWS_MANIFEST) if sys.platform.startswith("win") else None,
    exclude_binaries=True,
    contents_directory=".",
)

collection = COLLECT(
    gui_exe,
    cli_exe,
    gui_analysis.binaries,
    gui_analysis.datas,
    cli_analysis.binaries,
    cli_analysis.datas,
    strip=False,
    upx=False,
    name="PerfEval-Lite",
)

if sys.platform == "darwin":
    app = BUNDLE(
        collection,
        name="PerfEval Lite.app",
        icon=ICON_PATH,
        bundle_identifier="local.perfeval.lite",
        version=os.environ.get("PERFEVAL_VERSION", "0.0.0"),
        info_plist={
            "CFBundleDisplayName": "PerfEval Lite",
            "CFBundleName": "PerfEval Lite",
            "CFBundleVersion": os.environ.get("PERFEVAL_BUILD_VERSION", "0.0.0"),
            "LSMinimumSystemVersion": "13.0",
            "LSBackgroundOnly": False,
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
        },
    )
