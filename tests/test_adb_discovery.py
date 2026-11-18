"""AdbClient adb 发现兜底单测（GUI 直启环境 PATH 受限，2026-11-16 根治）。

背景：Dock/Finder/launchd 启动的 app 只有精简 PATH，shutil.which("adb") 落空
→ 后端 auto 探测误落 synthetic 且启动时一次性锁死。兜底策略=已知位置探测
（ANDROID_HOME → 平台 SDK → homebrew），显式 adb_path 参数恒胜出。
"""

from __future__ import annotations

import os
from pathlib import Path

from perfeval.collectors.android import adb as adb_mod
from perfeval.collectors.android.adb import AdbClient


def test_explicit_adb_path_always_wins(monkeypatch) -> None:
    """显式传参不受 which/兜底影响（测试桩注入依赖此语义）。"""
    monkeypatch.setattr("shutil.which", lambda _: None)
    monkeypatch.setenv("PERFEVAL_ADB_PATH", "/bundled/adb.exe")
    assert AdbClient("/fake-adb").adb_path == "/fake-adb"


def test_bundled_adb_has_priority_over_path(monkeypatch, tmp_path: Path) -> None:
    executable = tmp_path / "应用目录 with spaces" / "adb.exe"
    executable.parent.mkdir()
    executable.touch()
    monkeypatch.setenv("PERFEVAL_ADB_PATH", str(executable))
    monkeypatch.setattr("shutil.which", lambda name: "/path/adb" if name == "adb" else None)

    client = AdbClient()

    assert client.available()
    assert client.command("devices", "-l") == [str(executable), "devices", "-l"]


def test_path_hit_short_circuits_fallback(monkeypatch, tmp_path: Path) -> None:
    """PATH 命中时不做已知位置探测（既有行为不变）。"""
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/local/bin/adb" if name == "adb" else None
    )
    monkeypatch.setenv("ANDROID_HOME", str(tmp_path))
    assert AdbClient().adb_path == "/usr/local/bin/adb"


def test_fallback_android_home(monkeypatch, tmp_path: Path) -> None:
    """ANDROID_HOME/platform-tools 兜底命中。"""
    sdk = tmp_path / "sdk"
    (sdk / "platform-tools").mkdir(parents=True)
    executable = sdk / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
    executable.touch()
    monkeypatch.setattr("shutil.which", lambda _: None)
    monkeypatch.setenv("ANDROID_HOME", str(sdk))
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    client = AdbClient()
    assert client.adb_path == str(executable)
    assert client.available() is True


def test_fallback_platform_sdk_location(monkeypatch, tmp_path: Path) -> None:
    """无 SDK 环境变量时按平台已知位置兜底（macOS Studio SDK 标准布局）。"""
    sdk = tmp_path / "Library" / "Android" / "sdk"
    (sdk / "platform-tools").mkdir(parents=True)
    executable = sdk / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
    executable.touch()
    monkeypatch.setattr("shutil.which", lambda _: None)
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert AdbClient().adb_path == str(executable)


def test_fallback_homebrew_prefix(monkeypatch, tmp_path: Path) -> None:
    """SDK 全缺席时落到 homebrew 前缀（绝对路径常量可注入）。"""
    brew = tmp_path / "brew" / "bin"
    brew.mkdir(parents=True)
    (brew / "adb").write_text("#!/bin/sh\n")
    monkeypatch.setattr("shutil.which", lambda _: None)
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(adb_mod, "_HOMEBREW_ADB_PATHS", (brew / "adb",))
    assert AdbClient().adb_path == str(brew / "adb")


def test_fallback_miss_keeps_honest_missing(monkeypatch, tmp_path: Path) -> None:
    """全部落空 → 维持 "adb" 裸名语义（available()=False，不伪造）。"""
    monkeypatch.setattr("shutil.which", lambda _: None)
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(adb_mod, "_HOMEBREW_ADB_PATHS", ())
    client = AdbClient()
    assert client.adb_path == "adb"
    assert client.available() is False
