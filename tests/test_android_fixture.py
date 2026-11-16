from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


FIXTURE_ROOT = Path("fixtures/android-fixture")


def test_android_fixture_source_tree_is_self_consistent() -> None:
    root_build = (FIXTURE_ROOT / "build.gradle").read_text(encoding="utf-8")
    app_build = (FIXTURE_ROOT / "app" / "build.gradle").read_text(encoding="utf-8")
    manifest = (FIXTURE_ROOT / "app" / "src" / "main" / "AndroidManifest.xml").read_text(encoding="utf-8")
    activity = (
        FIXTURE_ROOT / "app" / "src" / "main" / "java" / "com" / "perfeval" / "fixture" / "MainActivity.java"
    ).read_text(encoding="utf-8")

    assert 'id "com.android.application" version "8.6.1" apply false' in root_build
    assert 'namespace "com.perfeval.fixture"' in app_build
    assert 'applicationId "com.perfeval.fixture"' in app_build
    assert 'android:name=".PerfEvalFixtureApp"' in manifest
    assert 'android:name=".MainActivity"' in manifest
    assert "startup_delay_ms" in activity
    assert "main_thread_block_ms" in activity
    assert "jank_block_ms" in activity
    assert "memory_alloc_mb" in activity
    assert "crash" in activity


def test_android_fixture_sustained_jank_wiring() -> None:
    """持续型 jank 注入（2026-11-06 体检 fixture-sustained-jank）。

    jank_duration_ms > 0 走 JankInjector.scheduleSustained（持续窗口逐帧回调，
    每帧阻塞 jank_block_ms），优先于一次性 jank_count；两个 Activity 共用
    JankInjector，不各自维护注入实现。
    """
    src_root = FIXTURE_ROOT / "app" / "src" / "main" / "java" / "com" / "perfeval" / "fixture"
    injector = (src_root / "JankInjector.java").read_text(encoding="utf-8")
    main_activity = (src_root / "MainActivity.java").read_text(encoding="utf-8")
    scroll_activity = (src_root / "ScrollListActivity.java").read_text(encoding="utf-8")

    # 注入器承载两种模式；持续模式以 uptimeMillis 窗口收口
    assert "scheduleCount" in injector
    assert "scheduleSustained" in injector
    assert "jank.sustained.start" in injector
    assert "jank.sustained.stop" in injector
    assert "SystemClock.uptimeMillis" in injector

    for activity_source in (main_activity, scroll_activity):
        assert "jank_duration_ms" in activity_source
        assert "JankInjector.scheduleSustained" in activity_source
        assert "JankInjector.scheduleCount" in activity_source
        # 注入实现收敛到 JankInjector（不重复实现 Choreographer 循环）
        assert "Choreographer.getInstance().postFrameCallback" not in activity_source


# ---- 编译验证（复核 2026-11-06 漏测面补强：有 Android SDK + javac 的环境即验证 ----
# ---- fixture 源码可编译，补上「源码断言不护编译正确性」的缺口；无 SDK 环境显式 skip）----


def _android_jar() -> Path | None:
    sdk = os.environ.get("ANDROID_HOME")
    if not sdk:
        return None
    platforms = Path(sdk) / "platforms"
    if not platforms.is_dir():
        return None
    jars = sorted(platforms.glob("android-*/android.jar"))
    return jars[-1] if jars else None


def test_android_fixture_sources_compile() -> None:
    jar = _android_jar()
    javac = shutil.which("javac")
    if jar is None or javac is None:
        pytest.skip("需要 ANDROID_HOME(platforms android.jar) 与 javac；缺失环境跳过（源码断言测试仍生效）")
    src_root = FIXTURE_ROOT / "app" / "src" / "main" / "java" / "com" / "perfeval" / "fixture"
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        result = subprocess.run(  # noqa: S603 - 固定参数编译 fixture 源码
            [javac, "--release", "11", "-classpath", str(jar), "-d", tmp,
             *[str(p) for p in sorted(src_root.glob("*.java"))]],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    assert result.returncode == 0, f"fixture 源码编译失败：\n{result.stderr}"
