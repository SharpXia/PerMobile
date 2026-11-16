from __future__ import annotations

import json
from argparse import ArgumentParser
from pathlib import Path

from perfeval.cli.commands import run as run_command
from perfeval.cli.commands.run import register_parser
from perfeval.cli.main import main
from perfeval.collectors.android import (
    AdbClient,
    AndroidCollectionResult,
    AndroidPreflightResult,
    AndroidRunExecutor,
)
from perfeval.collectors.android.flow import (
    InteractionCommand,
    build_interaction_commands,
    find_launch_activity,
    parse_flow_steps,
)
from perfeval.collectors.android.plan import (
    resolve_sampling_config,
    resolve_screen_record_config,
)
from perfeval.collectors.android.samplers import (
    SAMPLER_FACTORIES,
    DeviceContext,
    ProbeResult,
    SampleRecord,
)
from perfeval.core.timeline import TimelineEvent


def test_parse_flow_steps_swipe() -> None:
    flow_config = {
        "steps": [
            {"type": "swipe", "startX": 100, "startY": 200, "endX": 100, "endY": 50, "durationMs": 300, "repeat": 3, "intervalMs": 400},
        ]
    }
    steps = parse_flow_steps(flow_config)
    assert len(steps) == 1
    assert steps[0]["type"] == "swipe"

    adb = AdbClient("/adb")
    commands = build_interaction_commands(steps, adb=adb, package_name="com.test", serial=None)
    swipe_commands = [c for c in commands if c.name.startswith("swipe_")]
    assert len(swipe_commands) == 3
    assert swipe_commands[0].command[-5:] == ("100", "200", "100", "50", "300")
    assert swipe_commands[0].wait_s == 0.4


def test_parse_flow_steps_reset_gfxinfo() -> None:
    flow_config = {"steps": [{"type": "reset_gfxinfo"}]}
    steps = parse_flow_steps(flow_config)
    adb = AdbClient("/adb")
    commands = build_interaction_commands(steps, adb=adb, package_name="com.test", serial=None)
    reset_cmds = [c for c in commands if c.name == "reset_gfxinfo"]
    assert len(reset_cmds) == 1
    assert "gfxinfo" in reset_cmds[0].command
    assert "com.test" in reset_cmds[0].command
    assert "reset" in reset_cmds[0].command


def test_parse_flow_steps_idle() -> None:
    flow_config = {"steps": [{"type": "idle", "durationMs": 1500}]}
    steps = parse_flow_steps(flow_config)
    adb = AdbClient("/adb")
    commands = build_interaction_commands(steps, adb=adb, package_name="com.test", serial=None)
    idle_cmds = [c for c in commands if c.name == "idle"]
    assert len(idle_cmds) == 1
    assert idle_cmds[0].command == ()
    assert idle_cmds[0].wait_s == 1.5


def test_parse_flow_steps_manual() -> None:
    flow_config = {"steps": [{"type": "manual", "prompt": "press enter", "timeoutMs": 3000}]}
    steps = parse_flow_steps(flow_config)
    adb = AdbClient("/adb")
    commands = build_interaction_commands(steps, adb=adb, package_name="com.test", serial=None)
    manual_cmds = [c for c in commands if c.is_manual]
    assert len(manual_cmds) == 1
    assert manual_cmds[0].prompt == "press enter"
    assert manual_cmds[0].command == ()
    assert manual_cmds[0].timeout_s == 3.0


def test_build_interaction_commands_skips_launch() -> None:
    flow_config = {
        "steps": [
            {"type": "launch", "activity": ".MainActivity"},
            {"type": "idle", "durationMs": 500},
        ]
    }
    steps = parse_flow_steps(flow_config)
    adb = AdbClient("/adb")
    commands = build_interaction_commands(steps, adb=adb, package_name="com.test", serial=None)
    assert all(not c.name.startswith("launch") for c in commands)
    assert len(commands) == 1
    assert commands[0].name == "idle"


def test_find_launch_activity_returns_activity() -> None:
    steps = parse_flow_steps({"steps": [{"type": "launch", "activity": ".ScrollListActivity"}]})
    assert find_launch_activity(steps) == ".ScrollListActivity"


def test_find_launch_activity_returns_none() -> None:
    steps = parse_flow_steps({"steps": [{"type": "idle", "durationMs": 500}]})
    assert find_launch_activity(steps) is None


def test_find_launch_activity_empty_steps() -> None:
    assert find_launch_activity([]) is None


def test_parse_flow_steps_empty_config() -> None:
    assert parse_flow_steps(None) == []
    assert parse_flow_steps({}) == []
    assert parse_flow_steps({"steps": "not a list"}) == []


def test_swipe_default_repeat() -> None:
    flow_config = {"steps": [{"type": "swipe", "startX": 0, "startY": 0, "endX": 0, "endY": 0, "durationMs": 100}]}
    steps = parse_flow_steps(flow_config)
    adb = AdbClient("/adb")
    commands = build_interaction_commands(steps, adb=adb, package_name="com.test", serial=None)
    swipe_cmds = [c for c in commands if c.name.startswith("swipe_")]
    assert len(swipe_cmds) == 1


# --- CM-TASK-006：采样配置归并、runner 生命周期与 CLI 参数 ---

def test_resolve_sampling_config_merges_cli_overrides() -> None:
    config = {"collectors": {"android": {"samplers": {"mem": {"enabled": "auto", "intervalMs": 2000}}}}}

    assert resolve_sampling_config(config) == {"mem": {"enabled": "auto", "intervalMs": 2000}}
    assert resolve_sampling_config(config, sampling_interval_ms=8000) == {
        "mem": {"enabled": "auto", "intervalMs": 8000}
    }
    assert resolve_sampling_config(config, sampling_off=True) == {}
    assert resolve_sampling_config(config, sampling_off=True, sampling_interval_ms=8000) == {}

    bool_config = {"collectors": {"android": {"samplers": {"mem": True}}}}
    assert resolve_sampling_config(bool_config, sampling_interval_ms=500) == {
        "mem": {"enabled": True, "intervalMs": 500}
    }

    off_entry = {"collectors": {"android": {"samplers": {"mem": False}}}}
    assert resolve_sampling_config(off_entry, sampling_interval_ms=500) == {"mem": False}

    assert resolve_sampling_config(None) == {}
    assert resolve_sampling_config({"collectors": {"android": {}}}) == {}


class FlowSampler:
    """生命周期探针：start/stop 打点进 order，stop 返回终止读数保证落盘确定性。"""

    name = "flow"
    metric_ids = ("flow.metric",)
    interval_ms = 60_000

    def __init__(self, order: list[str]) -> None:
        self._order = order

    def probe(self, ctx: DeviceContext) -> ProbeResult:
        return ProbeResult.ok()

    def start(self, ctx: DeviceContext) -> None:
        self._order.append("sampler.start")

    def sample(self, ctx: DeviceContext) -> list[SampleRecord]:
        return []

    def stop(self) -> list[SampleRecord]:
        self._order.append("sampler.stop")
        return [
            SampleRecord(
                metric_id="flow.metric",
                value=9.0,
                ts_ms=123,
                tags={"unit": "x", "collector": "sampler.flow"},
            )
        ]


def test_android_run_executor_sampling_lifecycle(tmp_path) -> None:
    order: list[str] = []

    def fake_runner(command, timeout):
        joined = " ".join(command)
        if "date +%s%3N" in joined:
            order.append("sampler_clock")
            return _completed("1757300002000\n812345.678 12345.6\n")
        if "am start" in joined:
            order.append("launch")
        elif "dumpsys gfxinfo" in joined and "reset" in command:
            order.append("reset_gfxinfo")
        elif "dumpsys gfxinfo" in joined and "framestats" not in joined:
            order.append("collect_gfxinfo")
            return _completed("Total frames rendered: 100\nJanky frames: 12 (12.00%)\n95th percentile: 24ms\n")
        elif "dumpsys meminfo" in joined:
            return _completed("App Summary\n           Java Heap:    51200\n                TOTAL:    90112\n")
        elif "logcat -d" in joined:
            return _completed("")
        return _completed("")

    SAMPLER_FACTORIES["flow"] = lambda *, interval_ms=1000: FlowSampler(order)
    try:
        executor = run_executor_with_runner(fake_runner)
        result = executor.collect(
            run_id="run-sampling",
            run_dir=tmp_path / "run",
            package_name="com.perfeval.fixture",
            activity=".MainActivity",
            post_launch_wait_s=0,
            samplers_config={"flow": {"enabled": True, "intervalMs": 60000}},
        )
    finally:
        SAMPLER_FACTORIES.pop("flow")

    # launch 前启动、collect 第一个命令前停止
    assert order.index("sampler.start") < order.index("launch")
    assert order.index("reset_gfxinfo") < order.index("sampler.start")
    assert order.index("sampler.stop") < order.index("collect_gfxinfo")
    assert result.diagnostics == ()

    samples_path = tmp_path / "run" / "artifacts" / "android" / "samples.ndjson"
    lines = [json.loads(line) for line in samples_path.read_text(encoding="utf-8").splitlines()]
    assert lines == [
        {"tsMs": 123, "metricId": "flow.metric", "value": 9.0, "tags": {"unit": "x", "collector": "sampler.flow"}}
    ]

    # 采样记录转为 counter 事件进入 timeline
    counters = [event for event in result.events if event["type"] == "counter"]
    assert len(counters) == 1
    assert counters[0]["name"] == "flow.metric"
    assert counters[0]["source"]["collector"] == "sampler.flow"
    assert counters[0]["data"]["value"] == 9.0
    for event in result.events:
        TimelineEvent.from_dict(event)


def test_android_run_executor_reports_unavailable_sampler(tmp_path) -> None:
    class UnavailableSampler(FlowSampler):
        name = "flow"

        def __init__(self) -> None:
            super().__init__([])

        def probe(self, ctx: DeviceContext) -> ProbeResult:
            return ProbeResult.unavailable("device offline")

    def fake_runner(command, timeout):
        return _completed("")

    SAMPLER_FACTORIES["flow"] = lambda *, interval_ms=1000: UnavailableSampler()
    try:
        executor = run_executor_with_runner(fake_runner)
        result = executor.collect(
            run_id="run-unavailable",
            run_dir=tmp_path / "run",
            package_name="com.perfeval.fixture",
            activity=".MainActivity",
            post_launch_wait_s=0,
            samplers_config={"flow": {"enabled": True}},
        )
    finally:
        SAMPLER_FACTORIES.pop("flow")

    assert "sampler.flow.unavailable:device offline" in result.diagnostics
    assert not (tmp_path / "run" / "artifacts" / "android" / "samples.ndjson").exists()


def test_android_run_executor_without_samplers_writes_no_samples(tmp_path) -> None:
    executed: list[str] = []

    def fake_runner(command, timeout):
        joined = " ".join(command)
        executed.append(joined)
        if "dumpsys gfxinfo" in joined and "framestats" not in joined:
            return _completed("Total frames rendered: 100\nJanky frames: 12 (12.00%)\n95th percentile: 24ms\n")
        return _completed("")

    executor = run_executor_with_runner(fake_runner)
    result = executor.collect(
        run_id="run-no-sampling",
        run_dir=tmp_path / "run",
        package_name="com.perfeval.fixture",
        activity=".MainActivity",
        post_launch_wait_s=0,
        samplers_config=None,
    )

    assert all("sampler." not in item for item in result.diagnostics)
    assert not (tmp_path / "run" / "artifacts" / "android" / "samples.ndjson").exists()
    assert result.events


def test_android_run_executor_flushes_samples_when_launch_fails(tmp_path) -> None:
    """launch 失败的提前返回路径也必须停采样并刷入 samples.ndjson。"""
    order: list[str] = []

    def fake_runner(command, timeout):
        joined = " ".join(command)
        if "am start" in joined:
            order.append("launch")
            return _failed(stderr="am start crashed")
        return _completed("")

    SAMPLER_FACTORIES["flow"] = lambda *, interval_ms=1000: FlowSampler(order)
    try:
        executor = run_executor_with_runner(fake_runner)
        result = executor.collect(
            run_id="run-launch-failed",
            run_dir=tmp_path / "run",
            package_name="com.perfeval.fixture",
            activity=".MainActivity",
            post_launch_wait_s=0,
            samplers_config={"flow": {"enabled": True}},
        )
    finally:
        SAMPLER_FACTORIES.pop("flow")

    assert any(item.startswith("android.command_failed:launch:1:") for item in result.diagnostics)
    assert order == ["sampler.start", "launch", "sampler.stop"]

    samples_path = tmp_path / "run" / "artifacts" / "android" / "samples.ndjson"
    lines = [json.loads(line) for line in samples_path.read_text(encoding="utf-8").splitlines()]
    assert lines == [
        {"tsMs": 123, "metricId": "flow.metric", "value": 9.0, "tags": {"unit": "x", "collector": "sampler.flow"}}
    ]


def test_run_cli_registers_sampling_flags() -> None:
    parser = ArgumentParser()
    sub = parser.add_subparsers()
    run_parser = register_parser(sub)

    args = run_parser.parse_args(["scenario.yaml", "--sampling-off", "--sampling-interval-ms", "500"])
    assert args.sampling_off is True
    assert args.sampling_interval_ms == 500

    defaults = run_parser.parse_args(["scenario.yaml"])
    assert defaults.sampling_off is False
    assert defaults.sampling_interval_ms is None


def test_run_cli_passes_resolved_samplers_config(tmp_path, monkeypatch) -> None:
    scenario = tmp_path / "scenario-samplers.yaml"
    scenario.write_text(
        "\n".join(
            [
                "schemaVersion: perfeval.scenario.v1",
                "scenarioId: cm006_cli_sampling",
                "description: sampler config passthrough",
                "apps:",
                "  - id: target",
                "    platform: android",
                "    packageName: com.example.app",
                "run:",
                "  repeats: 1",
                "collectors:",
                "  android:",
                "    samplers:",
                "      mem:",
                "        enabled: auto",
                "        intervalMs: 2000",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    calls: dict[str, object] = {}
    monkeypatch.setattr(
        run_command,
        "run_preflight",
        lambda adb, serial=None: AndroidPreflightResult(
            ok=True,
            adb_available=True,
            selected_serial=serial or "emulator-5554",
        ),
    )

    def fake_collect(**kwargs):
        calls.update(kwargs)
        return AndroidCollectionResult(
            run_id=str(kwargs["run_id"]),
            artifact_dir=Path(kwargs["run_dir"]) / "artifacts" / "android",
            events=[],
            artifacts=[],
            commands=(),
            diagnostics=(),
        )

    monkeypatch.setattr(run_command, "collect_android_run", fake_collect)

    code = main(
        [
            "run",
            str(scenario),
            "--repeat",
            "1",
            "--out",
            str(tmp_path / "runs"),
            "--android-device-run",
            "--package-name",
            "com.example.app",
            "--sampling-interval-ms",
            "7000",
        ]
    )

    assert code == 0
    assert calls["samplers_config"] == {"mem": {"enabled": "auto", "intervalMs": 7000}}

    code = main(
        [
            "run",
            str(scenario),
            "--repeat",
            "1",
            "--out",
            str(tmp_path / "runs-off"),
            "--android-device-run",
            "--package-name",
            "com.example.app",
            "--sampling-off",
        ]
    )

    assert code == 0
    assert calls["samplers_config"] == {}


def run_executor_with_runner(fake_runner=None) -> AndroidRunExecutor:
    def default_fake(command, timeout):
        return _completed("")

    return AndroidRunExecutor(adb=AdbClient("/adb"), command_runner=fake_runner or default_fake)


class _Completed:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _completed(stdout: str) -> _Completed:
    return _Completed(stdout=stdout)


def _failed(stdout: str = "", stderr: str = "boom") -> _Completed:
    return _Completed(stdout=stdout, returncode=1, stderr=stderr)


# --- F0：runner 停止采样处统一 drain sampler 级内部诊断 / 采样配置透传 ---


class _InternalDiagSampler:
    """带内部诊断缓冲的采样器 stub（模拟 cpu/net/power/gpu_thermal 的 drain 契约）。"""

    name = "cpu"
    interval_ms = 1000
    metric_ids = ("cpu.total.pct",)

    def __init__(self) -> None:
        self._diagnostics: list[str] = []

    def probe(self, ctx: DeviceContext) -> ProbeResult:
        return ProbeResult.ok()

    def start(self, ctx: DeviceContext) -> None:
        return None

    def sample(self, ctx: DeviceContext) -> list[SampleRecord]:
        self._diagnostics.append("sampler.cpu.out_of_range:core0 dropped 120.0 (outside [0, 100])")
        return []

    def stop(self) -> list[SampleRecord]:
        return []

    def drain_diagnostics(self) -> list[str]:
        drained = list(self._diagnostics)
        self._diagnostics.clear()
        return drained


def test_stop_sampling_drains_sampler_internal_diagnostics_into_run_diagnostics() -> None:
    from perfeval.collectors.android.adb import AdbClient
    from perfeval.collectors.android.runner import _stop_sampling
    from perfeval.collectors.android.samplers import SamplingScheduler

    scheduler = SamplingScheduler(
        [_InternalDiagSampler()],
        context=DeviceContext(adb=AdbClient(), command_runner=lambda command, timeout: None),
    )
    scheduler.start()
    scheduler.poll_once()
    diagnostics: list[str] = []

    _stop_sampling(scheduler, None, diagnostics)

    # sampler 内部级 diagnostic 并入 run diagnostics（与调度器级同落点）
    assert "sampler.cpu.out_of_range:core0 dropped 120.0 (outside [0, 100])" in diagnostics
    # 幂等复用：二次 stop 不重复 drain
    _stop_sampling(scheduler, None, diagnostics)
    assert diagnostics.count("sampler.cpu.out_of_range:core0 dropped 120.0 (outside [0, 100])") == 1


def test_resolve_sampling_config_preserves_entry_config_keys() -> None:
    config = {
        "collectors": {
            "android": {
                "samplers": {"cpu": {"enabled": "auto", "intervalMs": 1000, "perCore": True}}
            }
        }
    }

    resolved = resolve_sampling_config(config)

    # 原始 entry 保留，scenario YAML 的 perCore 可达 build_samplers
    assert resolved == {"cpu": {"enabled": "auto", "intervalMs": 1000, "perCore": True}}
    assert resolve_sampling_config(config, sampling_interval_ms=500)["cpu"]["perCore"] is True


# --- CM-TASK-020：screenrecord 采集（plan 命令扩展 / runner 生命周期 / 180s 分段 / 失败降级） ---

from perfeval.collectors.android.plan import build_screen_record_plan, estimate_interaction_window_s  # noqa: E402
from perfeval.collectors.android.plan import SCREENRECORD_TIME_LIMIT_MAX_S  # noqa: E402


def test_resolve_screen_record_config_defaults_off() -> None:
    assert resolve_screen_record_config({}) is None
    assert resolve_screen_record_config(None) is None
    assert resolve_screen_record_config({"collectors": {"android": {"screenRecord": {"enabled": False}}}}) is None
    # CLI 显式开启（YAML 未配置）：默认参数
    assert resolve_screen_record_config({}, screen_record=True) == {
        "enabled": True,
        "bitrateMbps": 8.0,
        "size": None,
        "maxDurationMs": None,
    }
    # CLI off 优先级最高
    enabled = {"collectors": {"android": {"screenRecord": {"enabled": True}}}}
    assert resolve_screen_record_config(enabled, screen_record_off=True) is None
    assert resolve_screen_record_config(enabled, screen_record=True, screen_record_off=True) is None


def test_resolve_screen_record_config_normalizes_entry() -> None:
    config = {
        "collectors": {
            "android": {
                "screenRecord": {"enabled": True, "bitrateMbps": 4, "sizePx": 1080, "maxDurationMs": 120000}
            }
        }
    }
    assert resolve_screen_record_config(config) == {
        "enabled": True,
        "bitrateMbps": 4.0,
        "size": "1080x1080",
        "maxDurationMs": 120000,
    }
    # size 字符串直通优先于 sizePx
    config["collectors"]["android"]["screenRecord"]["size"] = "1080x1920"
    assert resolve_screen_record_config(config)["size"] == "1080x1920"


def test_estimate_interaction_window_s() -> None:
    commands = [
        InteractionCommand(name="idle", wait_s=2.0),
        InteractionCommand(name="manual", is_manual=True, prompt="go", timeout_s=30.0),
    ]
    assert estimate_interaction_window_s(commands, post_launch_wait_s=1.5) == 33.5
    assert estimate_interaction_window_s(None) == 0.0


def test_build_screen_record_plan_single_segment() -> None:
    adb = AdbClient("/adb")
    plan = build_screen_record_plan(
        adb=adb,
        run_id="run-a",
        config={"enabled": True},
        interaction_commands=[InteractionCommand(name="idle", wait_s=20.0)],
        post_launch_wait_s=2.0,
        artifact_dir="/tmp/artifacts/android",
    )
    assert plan is not None
    assert not plan.segmented
    assert plan.segment_count == 1
    # min(估计窗口 22s + 10s, 175s)
    assert plan.time_limit_s == 32
    # nohup 设备后台化 + --bugreport 水印 + runId 命名
    start = plan.start_commands[0]
    script = start[start.index("shell") + 1]
    assert script.startswith("nohup screenrecord --bugreport --time-limit 32")
    assert "--bit-rate 8000000" in script
    assert script.endswith("/sdcard/perfeval-run-a.mp4 >/dev/null 2>&1 &")
    assert plan.remote_paths == ("/sdcard/perfeval-run-a.mp4",)
    assert plan.local_paths == ("/tmp/artifacts/android/screen.mp4",)
    assert plan.stop_command[-1] == "pkill -INT screenrecord"
    assert plan.pull_commands[0][:2] == ("/adb", "pull")


def test_build_screen_record_plan_segments_over_180s_limit() -> None:
    adb = AdbClient("/adb")
    # 400s 窗口 → 单段 175s 上限装不下 → 分段（每段 --time-limit 175）
    plan = build_screen_record_plan(
        adb=adb,
        run_id="run-long",
        config={"enabled": True},
        interaction_commands=[InteractionCommand(name="idle", wait_s=400.0)],
        post_launch_wait_s=0.0,
        artifact_dir="/tmp/artifacts/android",
    )
    assert plan is not None
    assert plan.segmented
    assert plan.time_limit_s == SCREENRECORD_TIME_LIMIT_MAX_S
    assert plan.segment_count == 3
    assert [path.rsplit("/", 1)[-1] for path in plan.remote_paths] == [
        "perfeval-run-long.mp4",
        "perfeval-run-long-seg002.mp4",
        "perfeval-run-long-seg003.mp4",
    ]
    assert plan.local_paths[1].endswith("screen-seg002.mp4")
    assert len(plan.start_commands) == len(plan.pull_commands) == 3


def test_build_screen_record_plan_max_duration_truncates_window() -> None:
    adb = AdbClient("/adb")
    plan = build_screen_record_plan(
        adb=adb,
        run_id="run-capped",
        config={"enabled": True, "maxDurationMs": 10_000},
        interaction_commands=[InteractionCommand(name="idle", wait_s=600.0)],
        post_launch_wait_s=0.0,
        artifact_dir="/tmp/a",
    )
    assert plan is not None
    assert plan.estimated_window_s == 10.0
    assert plan.time_limit_s == 20
    assert not plan.segmented


def test_build_screen_record_plan_disabled_returns_none() -> None:
    adb = AdbClient("/adb")
    assert build_screen_record_plan(
        adb=adb,
        run_id="run-off",
        config=None,
        interaction_commands=[],
        post_launch_wait_s=0.0,
        artifact_dir="/tmp/a",
    ) is None
    assert build_screen_record_plan(
        adb=adb,
        run_id="run-off",
        config={"enabled": False},
        interaction_commands=[],
        post_launch_wait_s=0.0,
        artifact_dir="/tmp/a",
    ) is None


def _screen_record_executor(fake_runner) -> AndroidRunExecutor:
    return AndroidRunExecutor(
        adb=AdbClient("/adb"),
        command_runner=fake_runner,
        screenrecord_finalize_wait_s=0.0,
    )


def test_android_run_executor_screenrecord_lifecycle(tmp_path) -> None:
    """probe → launch 前启动 → collect 前 pkill 停止 → pull 首段。"""
    executed: list[str] = []

    def fake_runner(command, timeout):
        joined = " ".join(command)
        if "command -v screenrecord" in joined:
            executed.append("probe")
            return _completed("/system/bin/screenrecord\n")
        if "nohup screenrecord" in joined:
            executed.append("start")
            return _completed("")
        if "pkill -INT screenrecord" in joined:
            executed.append("stop")
            return _completed("")
        if "pull" in joined:
            executed.append("pull")
            local_path = Path(command[-1])
            local_path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048)  # >=1KB 有效帧数据
            return _completed("pulled")
        if "am start" in joined:
            return _completed("TotalTime: 100\n")
        return _completed("")

    executor = _screen_record_executor(fake_runner)
    result = executor.collect(
        run_id="run-screen",
        run_dir=tmp_path / "run",
        package_name="com.perfeval.fixture",
        activity=".MainActivity",
        post_launch_wait_s=0,
        screen_record_config={"enabled": True},
    )

    assert executed == ["probe", "start", "stop", "pull"]
    assert result.diagnostics == ()
    # F-fix（R1-N3）：screenrecord 命令按执行顺序留痕进 result.commands
    screen_cmd_names = [c.name for c in result.commands if c.name.startswith("screenrecord")]
    assert screen_cmd_names == [
        "screenrecord_probe",
        "screenrecord_clock",
        "screenrecord_start_seg001",
        "screenrecord_stop",
        "screenrecord_pull_seg001",
    ]
    video = tmp_path / "run" / "artifacts" / "android" / "screen.mp4"
    assert video.exists()
    # 录屏 artifact 进索引：collector=screenrecord、不关联对比指标
    screen_artifacts = [a for a in result.artifacts if a["path"].endswith("screen.mp4")]
    assert len(screen_artifacts) == 1
    assert screen_artifacts[0]["collector"] == "screenrecord"
    assert screen_artifacts[0]["metricData"]["relatedMetrics"] == []


def test_android_run_executor_screenrecord_unsupported_degrades(tmp_path) -> None:
    """设备无 screenrecord：diagnostic 降级，不阻断 run，不产生 start/stop/pull。"""
    executed: list[str] = []

    def fake_runner(command, timeout):
        joined = " ".join(command)
        if "command -v screenrecord" in joined:
            executed.append("probe")
            return _failed(stderr="/system/bin/sh: screenrecord: not found")
        if "nohup screenrecord" in joined:
            executed.append("start")
        if "am start" in joined:
            return _completed("TotalTime: 100\n")
        return _completed("")

    executor = _screen_record_executor(fake_runner)
    result = executor.collect(
        run_id="run-nosr",
        run_dir=tmp_path / "run",
        package_name="com.perfeval.fixture",
        activity=".MainActivity",
        post_launch_wait_s=0,
        screen_record_config={"enabled": True},
    )

    assert executed == ["probe"]
    assert any(
        item.startswith("screen.record.unavailable:screenrecord_not_found:") for item in result.diagnostics
    )
    assert not (tmp_path / "run" / "artifacts" / "android" / "screen.mp4").exists()
    # 降级不阻断：其余指标照常
    assert any(event["name"] == "startup.cold.ms" for event in result.events)


def test_android_run_executor_screenrecord_start_failure_degrades(tmp_path) -> None:
    """probe 通过但启动失败（设备离线/编码器异常）：diagnostic 降级不阻断。"""

    def fake_runner(command, timeout):
        joined = " ".join(command)
        if "command -v screenrecord" in joined:
            return _completed("/system/bin/screenrecord\n")
        if "nohup screenrecord" in joined:
            return _failed(stderr="adb: device offline")
        if "am start" in joined:
            return _completed("TotalTime: 100\n")
        return _completed("")

    executor = _screen_record_executor(fake_runner)
    result = executor.collect(
        run_id="run-startfail",
        run_dir=tmp_path / "run",
        package_name="com.perfeval.fixture",
        activity=".MainActivity",
        post_launch_wait_s=0,
        screen_record_config={"enabled": True},
    )

    assert any(item.startswith("screen.record.unavailable:start_failed_seg001:") for item in result.diagnostics)
    assert not (tmp_path / "run" / "artifacts" / "android" / "screen.mp4").exists()


def test_android_run_executor_screenrecord_segment_rollover(tmp_path) -> None:
    """180s 硬限分段：单段到时后 pkill 收尾并起下一段，各段都被 pull。

    以 _ScreenRecordSession 单元级驱动（完整 collect 流程无法等待真实 175s
    time-limit，用 segment_started_mono 前移模拟段内经过时间）。
    """
    from perfeval.collectors.android.runner import _ScreenRecordSession

    executed: list[str] = []

    def fake_runner(command, timeout):
        joined = " ".join(command)
        if "command -v screenrecord" in joined:
            executed.append("probe")
            return _completed("/system/bin/screenrecord\n")
        if "nohup screenrecord" in joined and "seg003" in joined:
            executed.append("start_seg3")
            return _completed("")
        if "nohup screenrecord" in joined and "seg002" in joined:
            executed.append("start_seg2")
            return _completed("")
        if "nohup screenrecord" in joined:
            executed.append("start_seg1")
            return _completed("")
        if "pkill -INT screenrecord" in joined:
            executed.append("stop")
            return _completed("")
        if "pull" in joined and "seg003" in joined:
            executed.append("pull_seg3")
            Path(command[-1]).write_bytes(b"fake-mp4-seg3" + b"\x00" * 2048)  # >=1KB
            return _completed("")
        if "pull" in joined and "seg002" in joined:
            executed.append("pull_seg2")
            Path(command[-1]).write_bytes(b"fake-mp4-seg2" + b"\x00" * 2048)  # >=1KB
            return _completed("")
        if "pull" in joined:
            executed.append("pull_seg1")
            Path(command[-1]).write_bytes(b"fake-mp4-seg1" + b"\x00" * 2048)  # >=1KB
            return _completed("")
        return _completed("")

    adb = AdbClient("/adb")
    artifact_dir = tmp_path / "artifacts" / "android"
    artifact_dir.mkdir(parents=True)
    plan = build_screen_record_plan(
        adb=adb,
        run_id="run-seg",
        config={"enabled": True},
        interaction_commands=[InteractionCommand(name="idle", wait_s=400.0)],
        post_launch_wait_s=0.0,
        artifact_dir=str(artifact_dir),
    )
    assert plan is not None and plan.segment_count == 3

    executor = _screen_record_executor(fake_runner)
    session = _ScreenRecordSession(executor, plan, finalize_wait_s=0.0)
    diagnostics: list[str] = []
    session.start_first(diagnostics)
    assert executed == ["probe", "start_seg1"]

    # 模拟段内时间流逝超过 time-limit → 收尾并起下一段
    session.segment_started_mono -= plan.time_limit_s + 1
    session.maybe_rollover(diagnostics)
    assert executed[-2:] == ["stop", "start_seg2"]

    session.segment_started_mono -= plan.time_limit_s + 1
    session.maybe_rollover(diagnostics)
    assert executed[-2:] == ["stop", "start_seg3"]

    # 段未到时不翻转
    session.maybe_rollover(diagnostics)
    assert executed[-1] == "start_seg3"

    session.stop(diagnostics)
    session.pull(diagnostics)

    assert executed == [
        "probe",
        "start_seg1",
        "stop",
        "start_seg2",
        "stop",
        "start_seg3",
        "stop",
        "pull_seg1",
        "pull_seg2",
        "pull_seg3",
    ]
    assert diagnostics == []
    assert (artifact_dir / "screen.mp4").exists()
    assert (artifact_dir / "screen-seg002.mp4").exists()
    assert (artifact_dir / "screen-seg003.mp4").exists()


def test_android_run_executor_screenrecord_pull_failure_first_segment(tmp_path) -> None:
    """首段 pull 失败 → diagnostic；run 不阻断。"""

    def fake_runner(command, timeout):
        joined = " ".join(command)
        if "command -v screenrecord" in joined:
            return _completed("/system/bin/screenrecord\n")
        if "nohup screenrecord" in joined:
            return _completed("")
        if "pkill" in joined:
            return _completed("")
        if "pull" in joined:
            return _failed(stderr="adb: no such file or directory")
        if "am start" in joined:
            return _completed("TotalTime: 100\n")
        return _completed("")

    executor = _screen_record_executor(fake_runner)
    result = executor.collect(
        run_id="run-pullfail",
        run_dir=tmp_path / "run",
        package_name="com.perfeval.fixture",
        activity=".MainActivity",
        post_launch_wait_s=0,
        screen_record_config={"enabled": True},
    )

    assert any(item.startswith("screen.record.unavailable:pull_failed:") for item in result.diagnostics)
    assert any(event["name"] == "startup.cold.ms" for event in result.events)


def test_run_cli_registers_screen_record_flags() -> None:
    parser = ArgumentParser()
    sub = parser.add_subparsers()
    run_parser = register_parser(sub)

    args = run_parser.parse_args(["scenario.yaml", "--screen-record"])
    assert args.screen_record is True
    assert args.screen_record_off is False

    args = run_parser.parse_args(["scenario.yaml", "--screen-record-off"])
    assert args.screen_record is False
    assert args.screen_record_off is True


def test_run_cli_passes_resolved_screen_record_config(tmp_path, monkeypatch) -> None:
    scenario = tmp_path / "scenario-screen.yaml"
    scenario.write_text(
        "\n".join(
            [
                "schemaVersion: perfeval.scenario.v1",
                "scenarioId: cm020_cli_screen",
                "description: screen record config passthrough",
                "apps:",
                "  - id: target",
                "    platform: android",
                "    packageName: com.example.app",
                "run:",
                "  repeats: 1",
                "collectors:",
                "  android:",
                "    screenRecord:",
                "      enabled: true",
                "      bitrateMbps: 4",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    calls: dict[str, object] = {}
    monkeypatch.setattr(
        run_command,
        "run_preflight",
        lambda adb, serial=None: AndroidPreflightResult(
            ok=True,
            adb_available=True,
            selected_serial=serial or "emulator-5554",
        ),
    )

    def fake_collect(**kwargs):
        calls.update(kwargs)
        return AndroidCollectionResult(
            run_id=str(kwargs["run_id"]),
            artifact_dir=Path(kwargs["run_dir"]) / "artifacts" / "android",
            events=[],
            artifacts=[],
            commands=(),
            diagnostics=(),
        )

    monkeypatch.setattr(run_command, "collect_android_run", fake_collect)

    code = main(
        [
            "run",
            str(scenario),
            "--repeat",
            "1",
            "--out",
            str(tmp_path / "runs"),
            "--android-device-run",
            "--package-name",
            "com.example.app",
        ]
    )
    assert code == 0
    assert calls["screen_record_config"] == {
        "enabled": True,
        "bitrateMbps": 4.0,
        "size": None,
        "maxDurationMs": None,
    }

    code = main(
        [
            "run",
            str(scenario),
            "--repeat",
            "1",
            "--out",
            str(tmp_path / "runs-off"),
            "--android-device-run",
            "--package-name",
            "com.example.app",
            "--screen-record-off",
        ]
    )
    assert code == 0
    assert calls["screen_record_config"] is None


# --- B1（TASK-023-fix R1）：scheduler.start() 同刻设备时钟锚点 → clockAnchors.samplers ---


def test_android_run_executor_captures_sampler_clock_anchor(tmp_path) -> None:
    """采样序列 ts 原点是 scheduler.start()（host 单调）——同刻设备时钟读数
    经 parse_device_clock_output 落盘为 clockAnchors.samplers，供报告竖线换算。"""
    order: list[str] = []

    def fake_runner(command, timeout):
        joined = " ".join(command)
        if "date +%s%3N" in joined:
            order.append("sampler_clock")
            return _completed("1757300002000\n812345.678 12345.6\n")
        if "am start" in joined:
            order.append("launch")
        return _completed("")

    SAMPLER_FACTORIES["flow"] = lambda *, interval_ms=1000: FlowSampler(order)
    try:
        executor = run_executor_with_runner(fake_runner)
        result = executor.collect(
            run_id="run-sampler-clock",
            run_dir=tmp_path / "run",
            package_name="com.perfeval.fixture",
            activity=".MainActivity",
            post_launch_wait_s=0,
            samplers_config={"flow": {"enabled": True}},
        )
    finally:
        SAMPLER_FACTORIES.pop("flow")

    # 锚点读取发生在 scheduler.start() 与 launch 之间（同刻口径）
    assert order[:3] == ["sampler.start", "sampler_clock", "launch"]
    clock_commands = [c for c in result.commands if c.name == "sampler_clock"]
    assert len(clock_commands) == 1
    # 无 logcat epoch / 无录屏时仅 samplers 锚点也落盘（报告竖线唯一数据源）。
    # 形状即报告 consumer 契约：producer 的 epochMs 在 derive_clock_anchors 统一
    # 映射为 startEpochMs（B1-fix2），报告侧 _samplers_clock_anchor 只认它。
    from perfeval.collectors.android.timebase import SAMPLERS_ANCHOR_METHOD

    assert result.clock_anchors == {
        "samplers": {
            "startEpochMs": 1757300002000,
            "source": "device_date",
            "boottimeNs": 812345678000000,
            "method": SAMPLERS_ANCHOR_METHOD,
        }
    }


def test_android_run_executor_sampler_clock_shared_with_screenrecord(tmp_path) -> None:
    """screenrecord 的 boottime↔epoch 读数与采样锚点同款同刻：共用一次调用。"""
    executed: list[str] = []

    def fake_runner(command, timeout):
        joined = " ".join(command)
        if "date +%s%3N" in joined:
            executed.append("sampler_clock")
            return _completed("1757300002000\n812345.678 12345.6\n")
        if "command -v screenrecord" in joined:
            executed.append("probe")
            return _completed("/system/bin/screenrecord\n")
        if "nohup screenrecord" in joined:
            executed.append("start")
            return _completed("")
        if "pkill -INT screenrecord" in joined:
            executed.append("stop")
            return _completed("")
        if "pull" in joined:
            executed.append("pull")
            Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048)  # >=1KB
            return _completed("pulled")
        if "am start" in joined:
            return _completed("TotalTime: 100\n")
        return _completed("")

    SAMPLER_FACTORIES["flow"] = lambda *, interval_ms=1000: FlowSampler(executed)
    try:
        executor = _screen_record_executor(fake_runner)
        result = executor.collect(
            run_id="run-shared-clock",
            run_dir=tmp_path / "run",
            package_name="com.perfeval.fixture",
            activity=".MainActivity",
            post_launch_wait_s=0,
            samplers_config={"flow": {"enabled": True}},
            screen_record_config={"enabled": True},
        )
    finally:
        SAMPLER_FACTORIES.pop("flow")

    # 共用采样锚点：screenrecord 不再自读时钟（一次 adb 调用两方受益）
    assert "sampler_clock" in executed
    assert "screenrecord_clock" not in executed
    assert executed.index("sampler_clock") < executed.index("start")
    command_names = [c.name for c in result.commands]
    assert "sampler_clock" in command_names and "screenrecord_clock" not in command_names
    # samplers 锚点落盘（startEpochMs 映射形状）；视频锚点复用同一读数
    assert result.clock_anchors["samplers"]["startEpochMs"] == 1757300002000
    assert result.clock_anchors["video"]["startEpochMs"] == 1757300002000


def test_android_run_executor_no_sampler_clock_without_samplers(tmp_path) -> None:
    """未启用采样时无 sampler_clock 调用；clockAnchors 无 samplers 键（旧结构不变）。"""
    executed: list[str] = []

    def fake_runner(command, timeout):
        joined = " ".join(command)
        if "date +%s%3N" in joined:
            executed.append("sampler_clock")
        if "dumpsys gfxinfo" in joined and "framestats" not in joined:
            return _completed("Total frames rendered: 100\nJanky frames: 12 (12.00%)\n95th percentile: 24ms\n")
        return _completed("")

    executor = run_executor_with_runner(fake_runner)
    result = executor.collect(
        run_id="run-no-clock",
        run_dir=tmp_path / "run",
        package_name="com.perfeval.fixture",
        activity=".MainActivity",
        post_launch_wait_s=0,
        samplers_config=None,
    )

    assert executed == []
    assert all(c.name != "sampler_clock" for c in result.commands)
    assert result.clock_anchors is None or "samplers" not in result.clock_anchors


# --- B1-fix2（R2 必修）：端到端形状契约 + 反补分支 ---


def test_sampler_anchor_end_to_end_reaches_report_charts(tmp_path) -> None:
    """producer/consumer 形状契约（B1-fix2 R2 必修）：

    runner 采集（fake adb 产出 parse_device_clock_output 真实输出形状 epochMs）
    → derive_clock_anchors 映射 → manifest context.clockAnchors 落盘 →
    run.py _load_clock_anchors_from_manifest 离线读回 → build_summary_report。
    断言 cpu 图 payload 携带 timeBase{sampler_epoch, startEpochMs} 且 marker
    竖线 x 为正确换算值——producer/consumer 任何一侧改键名/形状都会红
    （R1 断链事故：producer 透传 epochMs、consumer 读 startEpochMs，两套
    单测互不覆盖导致真实链路竖线永不渲染）。
    """
    run_root = tmp_path / "run"
    logcat_text = "\n".join(
        [
            "1757300000.000 I/System: boot",
            '1757300005.000 D/PerfevalAgent: PERFEVAL_EVENT {"type": "metric", "name": "flow.step.login", "value": 1}',
        ]
    )

    def fake_runner(command, timeout):
        joined = " ".join(command)
        if "date +%s%3N" in joined:
            # parse_device_clock_output 真实输出形状（producer 逐字透传起点）
            return _completed("1757300002000\n812345.678 12345.6\n")
        if "logcat -d" in joined:
            return _completed(logcat_text)
        if "am start" in joined:
            return _completed("TotalTime: 100\n")
        if "dumpsys gfxinfo" in joined and "reset" in command:
            return _completed("")
        if "dumpsys gfxinfo" in joined and "framestats" not in joined:
            return _completed("Total frames rendered: 100\nJanky frames: 12 (12.00%)\n95th percentile: 24ms\n")
        if "dumpsys meminfo" in joined:
            return _completed("App Summary\n           Java Heap:    51200\n                TOTAL:    90112\n")
        return _completed("")

    SAMPLER_FACTORIES["flow"] = lambda *, interval_ms=1000: FlowSampler([])
    try:
        executor = run_executor_with_runner(fake_runner)
        result = executor.collect(
            run_id="run-e2e-anchor",
            run_dir=run_root,
            package_name="com.perfeval.fixture",
            activity=".MainActivity",
            post_launch_wait_s=0,
            samplers_config={"flow": {"enabled": True}},
        )
    finally:
        SAMPLER_FACTORIES.pop("flow")

    # --- producer 侧：真实设备时钟读数已映射为 startEpochMs（与 video 命名对齐）
    anchors = result.clock_anchors
    assert anchors["t0EpochMs"] == 1757300000000
    assert anchors["samplers"]["startEpochMs"] == 1757300002000
    assert anchors["markers"] == [{"name": "flow.step.login", "epochMs": 1757300005000}]

    # --- 落盘 + 离线读回（run.py 真函数，照 CM-TASK-021 manifest 双写模式）
    manifest = {"context": {"clockAnchors": anchors}}
    (run_root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    from perfeval.cli.commands.run import _load_clock_anchors_from_manifest

    loaded = _load_clock_anchors_from_manifest(run_root / "artifacts" / "android")
    assert loaded is not None and loaded["samplers"]["startEpochMs"] == 1757300002000

    # --- consumer 侧：summary → 报告 payload
    from perfeval.core.report import build_summary_report

    summary = {
        "schemaVersion": "perfeval.summary.v1",
        "runId": "run-e2e-anchor",
        "status": "completed",
        "clockAnchors": loaded,
        "metrics": {
            "cpu.proc.pct": {
                "value": 12.5,
                "unit": "%",
                "n": 20,
                "series": [{"ts": index * 1000, "v": 10.0 + index} for index in range(20)],
            },
        },
    }
    html_text = build_summary_report(summary)
    chart_payload_start = html_text.index("var __seriesCharts = ") + len("var __seriesCharts = ")
    payload, _end = json.JSONDecoder().raw_decode(html_text[chart_payload_start:])
    cpu = next(chart for chart in payload["charts"] if chart["id"] == "series-chart-cpu")
    # 竖线基准 = samplers.startEpochMs（序列原点），非 t0：
    # x = (1757300005000 - 1757300002000)/1000 = 3.0（错用 t0 会得 5.0，
    # 键名断链则 markers 键直接缺失——两种回归都在此变红）
    assert cpu["timeBase"] == {"domain": "sampler_epoch", "startEpochMs": 1757300002000}
    assert cpu["markers"] == [{"name": "flow.step.login", "x": 3.0}]


def test_android_run_executor_sampler_clock_failure_backfills_from_screenrecord(tmp_path) -> None:
    """sampler_clock 读失败（设备 date 不可解析且无 uptime → 无 host 兜底）时，
    由 screenrecord 自读结果反补 samplers 锚点（B1-fix2 小项）。"""
    clock_calls = {"n": 0}

    def fake_runner(command, timeout):
        joined = " ".join(command)
        if "date +%s%3N" in joined:
            clock_calls["n"] += 1
            if clock_calls["n"] == 1:
                return _completed("not-a-clock")  # sampler_clock：解析失败 → None
            return _completed("1757300002100\n812345.678 12345.6\n")  # screenrecord_clock
        if "command -v screenrecord" in joined:
            return _completed("/system/bin/screenrecord\n")
        if "nohup screenrecord" in joined:
            return _completed("")
        if "pkill -INT screenrecord" in joined:
            return _completed("")
        if "pull" in joined:
            Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048)  # >=1KB
            return _completed("pulled")
        if "am start" in joined:
            return _completed("TotalTime: 100\n")
        return _completed("")

    SAMPLER_FACTORIES["flow"] = lambda *, interval_ms=1000: FlowSampler([])
    try:
        executor = _screen_record_executor(fake_runner)
        result = executor.collect(
            run_id="run-clock-backfill",
            run_dir=tmp_path / "run",
            package_name="com.perfeval.fixture",
            activity=".MainActivity",
            post_launch_wait_s=0,
            samplers_config={"flow": {"enabled": True}},
            screen_record_config={"enabled": True},
        )
    finally:
        SAMPLER_FACTORIES.pop("flow")

    assert "sampler.clock_anchor_failed:unavailable" in result.diagnostics
    # 两次读取各自发生：sampler_clock 失败、screenrecord 自读成功
    command_names = [c.name for c in result.commands]
    assert "sampler_clock" in command_names and "screenrecord_clock" in command_names
    # screenrecord 读数反补 samplers 锚点（映射后形状）；视频锚点同源
    assert result.clock_anchors["samplers"]["startEpochMs"] == 1757300002100
    assert result.clock_anchors["video"]["startEpochMs"] == 1757300002100


# --- smoke-fix：ROM 限制空录屏降级链（pull 后 <1KB 判定无有效帧数据） ---

def test_android_run_executor_empty_recording_degrades(tmp_path) -> None:
    """0 字节 screen.mp4（ROM 限制等）：diagnostic / 索引 / 锚点 / 回放区四点降级。"""
    from perfeval.core.report import build_summary_report

    def fake_runner(command, timeout):
        joined = " ".join(command)
        if "command -v screenrecord" in joined:
            return _completed("/system/bin/screenrecord\n")
        if "date +%s%3N" in joined:
            return _completed("1757300001100\n100.500 100.500\n")
        if "nohup screenrecord" in joined:
            return _completed("")
        if "pkill -INT screenrecord" in joined:
            return _completed("")
        if "pull" in joined:
            # ROM 限制：pull 返回成功但文件 0 字节
            Path(command[-1]).write_bytes(b"")
            return _completed("pulled")
        if "logcat -d" in joined:
            return _completed("1757300000.000 I/System: ready\n")
        if "am start" in joined:
            return _completed("TotalTime: 100\n")
        return _completed("")

    executor = _screen_record_executor(fake_runner)
    result = executor.collect(
        run_id="run-empty-sr",
        run_dir=tmp_path / "run",
        package_name="com.perfeval.fixture",
        activity=".MainActivity",
        post_launch_wait_s=0,
        screen_record_config={"enabled": True},
    )

    # 1) diagnostic：*.unavailable 语义，附定位信息（文件名 + 实际大小）
    assert any(
        item.startswith("screen.record.unavailable:empty recording")
        and "screen.mp4:0B" in item
        for item in result.diagnostics
    )
    # 2) 无效文件已删、不入 artifact 索引
    video = tmp_path / "run" / "artifacts" / "android" / "screen.mp4"
    assert not video.exists()
    assert not [a for a in result.artifacts if a["path"].endswith(".mp4")]
    # 3) clockAnchors：video 锚点撤销，t0 保留（离线可复算口径不受影响）
    anchors = result.clock_anchors
    assert anchors is not None
    assert "video" not in anchors
    assert anchors["t0EpochMs"] == 1757300000000

    # 4) 报告回放区降级为"录屏不可用"文案，不渲染 video 标签
    summary = {
        "schemaVersion": "perfeval.summary.v1",
        "runId": "run-empty-sr",
        "scenarioId": "empty_sr",
        "status": "completed",
        "metrics": {},
        "jankSegments": [],
        "clockAnchors": anchors,
        "diagnostics": [
            {
                "name": "screen.record.unavailable",
                "data": {
                    "severity": "warning",
                    "code": "screen.record.unavailable",
                    "message": result.diagnostics[0] if result.diagnostics else "",
                },
            }
        ],
        "artifacts": [],
    }
    html_text = build_summary_report(summary, base_dir=tmp_path, report_dir=tmp_path / "report")
    assert "录屏回放" in html_text
    assert "<video" not in html_text
    assert "screen.record.unavailable:empty recording" in html_text


def test_android_run_executor_partial_valid_segments_keep_anchor(tmp_path) -> None:
    """首段有效（>=1KB）、第 2 段空：锚点保留，仅空段降级。"""
    plan_segments: list[str] = []

    def fake_runner(command, timeout):
        joined = " ".join(command)
        if "command -v screenrecord" in joined:
            return _completed("/system/bin/screenrecord\n")
        if "nohup screenrecord" in joined:
            return _completed("")
        if "pkill -INT screenrecord" in joined:
            return _completed("")
        if "pull" in joined:
            local_path = Path(command[-1])
            plan_segments.append(local_path.name)
            if local_path.name == "screen-seg002.mp4":
                local_path.write_bytes(b"")  # 第 2 段 ROM 限制空文件
            else:
                local_path.write_bytes(b"fake-mp4" + b"\x00" * 2048)  # >=1KB 有效
            return _completed("pulled")
        if "am start" in joined:
            return _completed("TotalTime: 100\n")
        return _completed("")

    from perfeval.collectors.android.runner import _ScreenRecordSession
    from perfeval.collectors.android.plan import build_screen_record_plan

    adb = AdbClient("/adb")
    artifact_dir = tmp_path / "artifacts" / "android"
    artifact_dir.mkdir(parents=True)
    plan = build_screen_record_plan(
        adb=adb,
        run_id="run-partial",
        config={"enabled": True},
        interaction_commands=[InteractionCommand(name="idle", wait_s=400.0)],
        post_launch_wait_s=0.0,
        artifact_dir=str(artifact_dir),
    )
    assert plan is not None and plan.segment_count == 3

    session = _ScreenRecordSession(_screen_record_executor(fake_runner), plan, finalize_wait_s=0.0)
    diagnostics: list[str] = []
    session.start_first(diagnostics)
    session.segment_started_mono -= plan.time_limit_s + 1
    session.maybe_rollover(diagnostics)
    session.stop(diagnostics)
    session.pull(diagnostics)

    # 空段 diagnostic（仅第 2 段）；有效段无 diagnostic
    empty_diags = [item for item in diagnostics if item.startswith("screen.record.unavailable:empty recording")]
    assert len(empty_diags) == 1 and "screen-seg002.mp4:0B" in empty_diags[0]
    # 无效段文件已删、有效段保留（第 3 段从未启动，pull 按 segment_index 截断）
    assert (artifact_dir / "screen.mp4").exists()
    assert not (artifact_dir / "screen-seg002.mp4").exists()
    # 有效段存在 → video 锚点保留
    assert session.video_anchors is not None
