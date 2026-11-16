"""ux(fix-3) 回归：录屏窗口与 manual 步骤实际时长的对齐。

用户实测：连续记录（录屏）时间比实际两次 Enter 之间的操作时长偏小。根因：
plan 阶段把 manual 步骤按其 timeoutMs（而非实际人工时长）计入估计窗口，
time-limit = min(est+10, 175) 在 plan 时固化——人工步骤实际超过估算时，录屏
在估算点自然到时停止，早于用户第二次按 Enter；rollover 只在步骤边界检查。

修复口径：
- 含 manual 步骤：time-limit 直接用单段上限 175s（人工时长不可预估）；
- 后台 watchdog（时钟/间隔可注入）在段到时后中途 rollover——即使主线程仍阻塞
  在 manual 步骤的 stdin 等待中；未设 maxDurationMs 时动态续段（extendable）；
- 纯自动步骤场景保持 est 口径（既有 test_build_screen_record_plan_* 锁定）。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from perfeval.collectors.android import AdbClient, AndroidRunExecutor
from perfeval.collectors.android.flow import InteractionCommand
from perfeval.collectors.android.plan import (
    SCREENRECORD_TIME_LIMIT_MAX_S,
    build_screen_record_plan,
)
from perfeval.collectors.android.runner import _ScreenRecordSession


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> object:
    class _R:
        pass

    result = _R()
    result.stdout = stdout
    result.returncode = returncode
    result.stderr = stderr
    return result


def _manual_plan(tmp_path: Path, *, interaction, config=None, run_id: str = "run-manual"):
    artifact_dir = tmp_path / "artifacts" / "android"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return build_screen_record_plan(
        adb=AdbClient("/adb"),
        run_id=run_id,
        config={"enabled": True} if config is None else config,
        interaction_commands=interaction,
        post_launch_wait_s=2.0,
        artifact_dir=str(artifact_dir),
    )


# --- a) plan：manual 场景 time-limit 直接用 175s 上限，不再按 timeoutMs 估算 ---


def test_manual_scenario_time_limit_uses_cap_not_timeout_estimate(tmp_path: Path) -> None:
    interaction = [
        InteractionCommand(name="manual", is_manual=True, prompt="go", timeout_s=10.0),
        InteractionCommand(name="idle", wait_s=2.0),
    ]
    plan = _manual_plan(tmp_path, interaction=interaction)
    assert plan is not None
    # 旧口径 min(est 12s + 10, 175) = 22s——人工超过 22s 时录屏提前到时停止；
    # 新口径直接用单段上限
    assert plan.time_limit_s == SCREENRECORD_TIME_LIMIT_MAX_S
    assert plan.segment_count == 1
    assert plan.extendable is True
    assert plan.segmented is False


def test_manual_scenario_with_budget_presegments_and_caps_time_limit(tmp_path: Path) -> None:
    interaction = [InteractionCommand(name="manual", is_manual=True, prompt="go", timeout_s=30.0)]
    plan = _manual_plan(
        tmp_path,
        interaction=interaction,
        config={"enabled": True, "maxDurationMs": 400_000},
    )
    assert plan is not None
    # 预算 400s：每段 175s、预分 3 段、预算封顶（不动态续段）
    assert plan.time_limit_s == SCREENRECORD_TIME_LIMIT_MAX_S
    assert plan.segment_count == 3
    assert plan.extendable is False

    small = _manual_plan(
        tmp_path,
        interaction=interaction,
        config={"enabled": True, "maxDurationMs": 10_000},
    )
    assert small is not None
    # 小预算同时封顶单段 time-limit（不出现 175s 段超出 10s 预算）
    assert small.time_limit_s == 10
    assert small.segment_count == 1


def test_extendable_plan_builds_dynamic_continuation_commands(tmp_path: Path) -> None:
    interaction = [InteractionCommand(name="manual", is_manual=True, prompt="go", timeout_s=None)]
    plan = _manual_plan(tmp_path, interaction=interaction, run_id="dyn")
    assert plan is not None and plan.extendable

    first = plan.segment_commands(0)
    assert first is not None
    assert first[0] == plan.start_commands[0]
    assert first[1] == plan.pull_commands[0]

    second = plan.segment_commands(1)
    assert second is not None
    start_script = second[0][second[0].index("shell") + 1]
    assert start_script == (
        f"nohup screenrecord --bugreport --time-limit {SCREENRECORD_TIME_LIMIT_MAX_S} "
        "--bit-rate 8000000 /sdcard/perfeval-dyn-seg002.mp4 >/dev/null 2>&1 &"
    )
    assert second[1][-3:] == ("pull", "/sdcard/perfeval-dyn-seg002.mp4", str(tmp_path / "artifacts" / "android" / "screen-seg002.mp4"))
    assert second[2] == "/sdcard/perfeval-dyn-seg002.mp4"
    assert second[3].endswith("screen-seg002.mp4")

    # 非 extendable 计划：预算外无段
    auto_plan = build_screen_record_plan(
        adb=AdbClient("/adb"),
        run_id="auto",
        config={"enabled": True},
        interaction_commands=[InteractionCommand(name="idle", wait_s=20.0)],
        post_launch_wait_s=0.0,
        artifact_dir=str(tmp_path / "a"),
    )
    assert auto_plan is not None and not auto_plan.extendable
    assert auto_plan.segment_commands(0) is not None
    assert auto_plan.segment_commands(1) is None


# --- b) watchdog：段到时后中途 rollover（可注入时钟），stop 时文件有效 ---


class _FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _screen_record_fake_runner(executed: list[str], artifact_dir: Path):
    def fake_runner(command, timeout):
        joined = " ".join(command)
        if "command -v screenrecord" in joined:
            executed.append("probe")
            return _completed("/system/bin/screenrecord\n")
        if "date +%s%3N" in joined:
            executed.append("clock")
            return _completed("1757300002000\n812345.678 12345.6\n")
        if "nohup screenrecord" in joined:
            remote = next(token for token in command[-1].split() if token.startswith("/sdcard/perfeval-"))
            executed.append(f"start:{remote}")
            return _completed("")
        if "pkill -INT screenrecord" in joined:
            executed.append("stop")
            return _completed("")
        if "pull" in joined:
            executed.append(f"pull:{Path(command[-1]).name}")
            Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048)
            return _completed("pulled")
        return _completed("")

    return fake_runner


def _wait_until(predicate, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met before timeout")


def test_watchdog_rolls_over_mid_segment_and_keeps_recording(tmp_path: Path) -> None:
    interaction = [InteractionCommand(name="manual", is_manual=True, prompt="go", timeout_s=None)]
    plan = _manual_plan(tmp_path, interaction=interaction)
    assert plan is not None and plan.extendable

    executed: list[str] = []
    clock = _FakeClock()
    session = _ScreenRecordSession(
        AndroidRunExecutor(adb=AdbClient("/adb"), command_runner=_screen_record_fake_runner(executed, tmp_path)),
        plan,
        finalize_wait_s=0.0,
        clock=clock,
        watchdog_interval_s=0.01,
    )
    diagnostics: list[str] = []
    session.start_first(diagnostics)
    assert executed[:2] == ["probe", "clock"]
    assert executed[2] == "start:/sdcard/perfeval-run-manual.mp4"

    # 段内时间流逝超过 time-limit → watchdog 收段并起下一段（主线程未做任何边界检查）
    clock.advance(plan.time_limit_s + 1)
    _wait_until(lambda: any(item.startswith("start:/sdcard/perfeval-run-manual-seg002") for item in executed))

    # 再超一段：动态续段仍在录制
    clock.advance(plan.time_limit_s + 1)
    _wait_until(lambda: any(item.startswith("start:/sdcard/perfeval-run-manual-seg003") for item in executed))

    session.stop(diagnostics)
    session.pull(diagnostics)

    assert session.segment_index == 2
    assert executed.count("stop") >= 3
    assert (tmp_path / "artifacts" / "android" / "screen.mp4").exists()
    assert (tmp_path / "artifacts" / "android" / "screen-seg002.mp4").exists()
    assert (tmp_path / "artifacts" / "android" / "screen-seg003.mp4").exists()
    assert not [item for item in diagnostics if item.startswith("screen.record.unavailable")]


def test_watchdog_disabled_by_interval_zero_keeps_boundary_only_behavior(tmp_path: Path) -> None:
    interaction = [InteractionCommand(name="manual", is_manual=True, prompt="go", timeout_s=None)]
    plan = _manual_plan(tmp_path, interaction=interaction)
    assert plan is not None

    executed: list[str] = []
    clock = _FakeClock()
    session = _ScreenRecordSession(
        AndroidRunExecutor(adb=AdbClient("/adb"), command_runner=_screen_record_fake_runner(executed, tmp_path)),
        plan,
        finalize_wait_s=0.0,
        clock=clock,
        watchdog_interval_s=0.0,
    )
    diagnostics: list[str] = []
    session.start_first(diagnostics)
    starts_before = len([item for item in executed if item.startswith("start:")])

    clock.advance(plan.time_limit_s + 1)
    time.sleep(0.05)
    # watchdog 关闭：段到时也不翻转（与既有"步骤边界检查"行为一致）
    assert len([item for item in executed if item.startswith("start:")]) == starts_before

    session.stop(diagnostics)
    session.pull(diagnostics)
    assert (tmp_path / "artifacts" / "android" / "screen.mp4").exists()


# --- c) 端到端：manual 步骤中段超过原 est，collect 阻塞在 stdin 等待时仍完成 rollover ---


def test_collect_rolls_over_while_blocked_in_manual_step(tmp_path: Path, monkeypatch) -> None:
    from perfeval.collectors.android import runner as runner_module

    interaction = [InteractionCommand(name="manual", is_manual=True, prompt="go", timeout_s=30.0)]
    plan = _manual_plan(tmp_path, interaction=interaction)
    assert plan is not None

    executed: list[str] = []
    clock = _FakeClock()
    entered = threading.Event()
    release = threading.Event()

    def fake_input_with_timeout(prompt, timeout_s, *, timeout_message):
        # 模拟 GUI/终端路径：主线程阻塞等待用户第二次按 Enter
        entered.set()
        release.wait(10)
        return True

    monkeypatch.setattr(runner_module, "_input_with_timeout", fake_input_with_timeout)

    executor = AndroidRunExecutor(
        adb=AdbClient("/adb"),
        command_runner=_screen_record_fake_runner(executed, tmp_path),
        screenrecord_finalize_wait_s=0.0,
        clock=clock,
        screenrecord_watchdog_interval_s=0.01,
    )

    results: list[object] = []

    def run_collect() -> None:
        results.append(
            executor.collect(
                run_id="run-manual-e2e",
                run_dir=tmp_path / "run",
                package_name="com.perfeval.fixture",
                activity=".MainActivity",
                post_launch_wait_s=0,
                interaction_commands=interaction,
                interactive=True,
                screen_record_config={"enabled": True},
            )
        )

    worker = threading.Thread(target=run_collect, daemon=True)
    worker.start()
    try:
        assert entered.wait(5), "manual step never entered"
        # 人工步骤中段超过原 est / 单段 time-limit：watchdog 应在阻塞期间完成 rollover
        clock.advance(plan.time_limit_s + 1)
        _wait_until(lambda: any(item.endswith("seg002.mp4") and item.startswith("start:") for item in executed))
    finally:
        release.set()
        worker.join(timeout=10)

    assert worker.is_alive() is False
    result = results[0]
    command_names = [c.name for c in result.commands]
    assert "screenrecord_start_seg002" in command_names
    assert "screenrecord_stop" in command_names
    assert "screenrecord_pull_seg001" in command_names
    assert "screenrecord_pull_seg002" in command_names
    assert (tmp_path / "run" / "artifacts" / "android" / "screen.mp4").exists()
    assert (tmp_path / "run" / "artifacts" / "android" / "screen-seg002.mp4").exists()
    assert not [item for item in result.diagnostics if item.startswith("screen.record.unavailable")]
