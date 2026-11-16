"""Android 真机采集 runner：命令计划执行、采样调度、录屏生命周期与时间对齐。

录屏（CM-TASK-020/021，SCREEN_RECORD_SYNC）窗口口径（ux/feedback-fixes fix-3）：
- 含 manual 步骤的场景：人工时长不可预估，plan 阶段 time-limit 直接取单段上限
  175s（不再按 manual timeoutMs 估算——估算偏小时录屏会在人工步骤中段提前自然
  到时、早于用户第二次按 Enter），collect 前的 pkill -INT 负责提前正常收尾；
- 段到时的中途 rollover：后台 watchdog 线程（默认每 0.5s 检查段内耗时，时钟可
  注入）在段耗时达到 time-limit 时立即收段并起下一段——即使主线程仍阻塞在
  manual 步骤的 stdin 等待中。未设 maxDurationMs 的 manual 场景按需动态生成续
  段（extendable），直到 stop；设了 maxDurationMs 时按预算预分段、预算封顶；
- 纯自动步骤场景保持估算口径：est = 等待之和，time-limit = min(est+10, 175)，
  est>170s 预分段。原有的"步内尾部不覆盖"限制由 watchdog 中途 rollover 取代
  （段到时不再需要等待步骤边界）。
- N5：repeats>1 时 clockAnchors 仅随各 repeat 目录（manifest context / summary）
  落盘，run 级合并产物不提供（多轮锚点歧义，回放区为单 run 功能）。
"""

from __future__ import annotations

import re
import select
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .adb import AdbClient, launch_command, parse_launcher_activity_component
from .collector import index_android_artifacts, parse_android_artifacts
from .flow import InteractionCommand
from .plan import (
    SCREENRECORD_MIN_VALID_BYTES,
    AndroidRunCommandPlan,
    ScreenRecordPlan,
    build_android_run_plan,
)
from .samples import SamplesWriter
from .samplers import DeviceContext, SamplingScheduler, build_samplers
from .timebase import (
    VIDEO_FIRST_FRAME_DELAY_MS,
    derive_clock_anchors,
    marker_events_from_timeline,
    parse_device_clock_output,
)

CommandRunner = Callable[[list[str], float | None], subprocess.CompletedProcess[str]]
PhaseCallback = Callable[[str, str, str, str | None], None]

_AM_START_TOTAL_TIME_RE = re.compile(r"^\s*TotalTime:\s*(\d+)\s*$", re.MULTILINE)
_AM_START_LAUNCH_STATE_RE = re.compile(r"^\s*LaunchState:\s*(\w+)\s*$", re.MULTILINE)

# pkill -INT 成功收尾封装 MP4 的等待（SCREEN_RECORD_SYNC §1）；仅注入用，真机路径默认生效
SCREENRECORD_FINALIZE_WAIT_S = 1.0
# 后台 watchdog 检查段内耗时的间隔（ux/feedback-fixes fix-3）：段到时后即使主线程
# 阻塞在 manual 步骤等待中也能收段起下一段
SCREENRECORD_WATCHDOG_INTERVAL_S = 0.5


@dataclass(frozen=True)
class AndroidCommandResult:
    name: str
    command: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stdout_path: str | None = None
    stderr: str = ""


@dataclass(frozen=True)
class AndroidCollectionResult:
    run_id: str
    artifact_dir: Path
    events: list[dict]
    artifacts: list[dict]
    commands: tuple[AndroidCommandResult, ...] = field(default_factory=tuple)
    diagnostics: tuple[str, ...] = field(default_factory=tuple)
    # CM-TASK-021：三方时钟锚点（t0/video/markers），写入 manifest context.clockAnchors
    clock_anchors: dict | None = None


def default_command_runner(command: list[str], timeout: float | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


class AndroidRunExecutor:
    def __init__(
        self,
        *,
        adb: AdbClient | None = None,
        command_runner: CommandRunner = default_command_runner,
        screenrecord_finalize_wait_s: float = SCREENRECORD_FINALIZE_WAIT_S,
        clock: Callable[[], float] = time.monotonic,
        screenrecord_watchdog_interval_s: float = SCREENRECORD_WATCHDOG_INTERVAL_S,
    ) -> None:
        self.adb = adb or AdbClient()
        self.command_runner = command_runner
        self.screenrecord_finalize_wait_s = max(0.0, float(screenrecord_finalize_wait_s))
        self._clock = clock
        self._screenrecord_watchdog_interval_s = max(0.0, float(screenrecord_watchdog_interval_s))

    def collect(
        self,
        *,
        run_id: str,
        run_dir: str | Path,
        package_name: str,
        activity: str | None = None,
        apk_path: str | None = None,
        serial: str | None = None,
        clear_data: bool = True,
        reinstall: bool = True,
        auto_resolve_activity: bool = False,
        perfetto_enabled: bool = False,
        perfetto_duration_ms: int = 20_000,
        post_launch_wait_s: float = 2.0,
        interaction_commands: list[InteractionCommand] | None = None,
        interactive: bool = False,
        phase_callback: PhaseCallback | None = None,
        samplers_config: Mapping[str, Any] | None = None,
        screen_record_config: Mapping[str, Any] | None = None,
    ) -> AndroidCollectionResult:
        run_root = Path(run_dir)
        artifact_dir = run_root / "artifacts" / "android"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        _clear_previous_artifacts(artifact_dir)
        perfetto_local = artifact_dir / "perfetto.pftrace"
        perfetto_remote = f"/data/misc/perfetto-traces/perfeval-{run_id}.pftrace"
        plan = build_android_run_plan(
            package_name=package_name,
            activity=activity,
            apk_path=apk_path,
            serial=serial,
            clear_data=clear_data,
            reinstall=reinstall,
            auto_resolve_activity=auto_resolve_activity,
            perfetto_enabled=perfetto_enabled,
            perfetto_duration_ms=perfetto_duration_ms,
            perfetto_remote_path=perfetto_remote,
            perfetto_local_path=str(perfetto_local),
            screen_record_config=screen_record_config,
            screen_record_run_id=run_id,
            screen_record_artifact_dir=artifact_dir,
            post_launch_wait_s=post_launch_wait_s,
            interaction_commands=interaction_commands,
            adb=self.adb,
        )
        scheduler: SamplingScheduler | None = None
        writer: SamplesWriter | None = None
        samplers = build_samplers(samplers_config)
        diagnostics: list[str] = []
        if samplers:
            # 在线追加写原始采样（CM-001 §6）：崩溃容错 + 离线 --android-artifacts 可重算
            writer = SamplesWriter(artifact_dir / "samples.ndjson")
            scheduler = SamplingScheduler(
                samplers,
                context=DeviceContext(
                    adb=self.adb,
                    command_runner=self.command_runner,
                    serial=serial,
                    package_name=package_name,
                ),
                on_record=writer.append,
            )
        recorder = (
            _ScreenRecordSession(
                self,
                plan.screenrecord,
                finalize_wait_s=self.screenrecord_finalize_wait_s,
                clock=self._clock,
                watchdog_interval_s=self._screenrecord_watchdog_interval_s,
                diagnostics=diagnostics,
            )
            if plan.screenrecord is not None
            else None
        )
        # F-fix（R1-N3）：session 与 _execute_plan 共享同一命令列表，
        # screenrecord 命令按实际执行顺序留痕进 result.commands
        commands, diagnostics, sampler_clock_anchor = self._execute_plan(
            plan,
            artifact_dir=artifact_dir,
            perfetto_duration_ms=perfetto_duration_ms,
            post_launch_wait_s=post_launch_wait_s,
            package_name=package_name,
            serial=serial,
            interactive=interactive,
            phase_callback=phase_callback,
            scheduler=scheduler,
            writer=writer,
            recorder=recorder,
            recorder_commands=recorder.commands if recorder is not None else None,
            diagnostics=diagnostics,
        )
        _notify_phase(phase_callback, "analysis", "started", "分析采集数据")
        # 从 am start -W 输出中解析 TotalTime，派生 startup.cold.ms 指标事件。
        startup_event = _derive_startup_event(commands, run_id=run_id)
        # CM-TASK-021：boottime↔epoch 锚点供 tp 帧事件升格为 epoch 对齐语义
        boottime_anchor = recorder.clock_anchor if recorder is not None else None
        events = parse_android_artifacts(
            artifact_dir,
            run_id=run_id,
            package_name=package_name,
            start_seq=2 if startup_event is not None else 1,
            clock_anchors={"boottimeAnchor": boottime_anchor} if boottime_anchor else None,
        )
        if startup_event is not None:
            events.insert(0, startup_event)
        artifacts = index_android_artifacts(artifact_dir)
        # CM-TASK-021：t0（logcat 首条 epoch）/ video 偏移 / marker 锚点 → manifest context
        video_anchors = recorder.video_anchors if recorder is not None else None
        logcat_text_path = artifact_dir / "logcat.txt"
        clock_anchors = derive_clock_anchors(
            logcat_text=logcat_text_path.read_text(encoding="utf-8", errors="replace") if logcat_text_path.exists() else None,
            video_anchors=video_anchors,
            markers=marker_events_from_timeline(events),
            samplers_anchor=sampler_clock_anchor,
        )
        _notify_phase(phase_callback, "analysis", "completed", "分析采集数据")
        return AndroidCollectionResult(
            run_id=run_id,
            artifact_dir=artifact_dir,
            events=events,
            artifacts=artifacts,
            commands=tuple(commands),
            diagnostics=tuple(diagnostics),
            clock_anchors=clock_anchors,
        )

    def _execute_plan(
        self,
        plan: AndroidRunCommandPlan,
        *,
        artifact_dir: Path,
        perfetto_duration_ms: int,
        post_launch_wait_s: float,
        package_name: str,
        serial: str | None,
        interactive: bool = False,
        phase_callback: PhaseCallback | None = None,
        scheduler: SamplingScheduler | None = None,
        writer: SamplesWriter | None = None,
        recorder: _ScreenRecordSession | None = None,
        recorder_commands: list[AndroidCommandResult] | None = None,
        diagnostics: list[str] | None = None,
    ) -> tuple[list[AndroidCommandResult], list[str]]:
        # F-fix（R1-N3）：recorder_commands 与本列表为同一对象时，session 追加的
        # screenrecord 命令按执行顺序留在返回值里；不同对象则收尾时并回
        commands: list[AndroidCommandResult] = (
            recorder_commands if recorder_commands is not None else []
        )
        # ux(fix-3)：diagnostics 由 collect() 创建并与 recorder session 共享同一
        # 列表——watchdog 线程的中途 rollover diagnostic 直接进入同一落点
        diagnostics = diagnostics if diagnostics is not None else []
        # B1（TASK-023-fix R1）：scheduler.start() 同刻设备时钟锚点，随结果穿出
        sampler_clock_anchor: dict | None = None
        perfetto_started = False
        perfetto_deadline: float | None = None

        _notify_phase(phase_callback, "app_prepare", "started", "启动应用")
        launch = plan.launch
        if plan.install is not None:
            result = self._run("install", plan.install, timeout=120)
            commands.append(result)
            if result.returncode != 0:
                diagnostics.append(
                    f"android.command_failed:install:{result.returncode}:{result.stderr}"
                )
                _notify_phase(phase_callback, "app_prepare", "failed", "启动应用", result.stderr)
                return commands, diagnostics, sampler_clock_anchor

        if plan.resolve_activity is not None:
            result = self._run("resolve_activity", plan.resolve_activity, timeout=30)
            commands.append(result)
            component = (
                parse_launcher_activity_component(
                    result.stdout,
                    package_name=package_name,
                )
                if result.returncode == 0
                else None
            )
            if component is None:
                if result.returncode != 0:
                    diagnostics.append(
                        "android.command_failed:"
                        f"resolve_activity:{result.returncode}:{result.stderr}"
                    )
                else:
                    diagnostics.append(f"android.launch_activity_unresolved:{package_name}")
                detail = _launcher_activity_resolution_error(package_name, result)
                _notify_phase(phase_callback, "app_prepare", "failed", "启动应用", detail)
                raise ValueError(detail)
            launch = launch_command(self.adb, component, serial=serial)

        for name, command, timeout in (
            ("force_stop", plan.force_stop, 60),
            ("clear_data", plan.clear_data, 60),
            ("clear_logcat", plan.clear_logcat, 10),
            ("reset_gfxinfo", plan.reset_gfxinfo, 30),
            ("perfetto", plan.perfetto, 60),
        ):
            if command is None:
                continue
            result = self._run(name, command, timeout=timeout)
            commands.append(result)
            if result.returncode != 0:
                diagnostics.append(f"android.command_failed:{name}:{result.returncode}:{result.stderr}")
            elif name == "perfetto":
                perfetto_started = True
                perfetto_deadline = time.monotonic() + max(0.0, perfetto_duration_ms / 1000.0)

        # launch 前启动采样（CM-001 §6：采样窗口覆盖启动过程）；probe 剔除不可用采样器
        if scheduler is not None:
            scheduler.probe()
            scheduler.start()
            diagnostics.extend(scheduler.drain_diagnostics())
            # B1（TASK-023-fix R1）：scheduler.start() 同一刻读一次设备时钟——采样
            # 序列 series ts 原点是 start()（host 单调 elapsed），与 t0/logcat 首行
            # 相差 setup 间隙，报告侧 marker 竖线必须以本锚点换算才与曲线同轴。
            # 只读 adb 调用，不触碰采样逻辑；screenrecord 同款读取共用本结果。
            # 不做 host epoch 兜底：host 与设备墙钟偏差会让竖线系统性错位，宁可
            # 无锚点（报告优雅缺省），失败时由 screenrecord 自读结果反补。
            clock_command = self.adb.command("shell", "date +%s%3N; cat /proc/uptime", serial=serial)
            clock_result = self._run("sampler_clock", clock_command, timeout=10)
            commands.append(clock_result)
            sampler_clock_anchor = parse_device_clock_output(clock_result.stdout)
            if sampler_clock_anchor is None:
                diagnostics.append("sampler.clock_anchor_failed:unavailable")

        # launch 前后台启动 screenrecord（SCREEN_RECORD_SYNC §1/§5：录屏窗口覆盖整个 flow，
        # 启动失败/设备不支持时 diagnostic 降级，不阻断 run）
        if recorder is not None:
            recorder.start_first(diagnostics, clock_anchor=sampler_clock_anchor)
            # 共享读取失败（None）时回退用 screenrecord 自己的读数补 samplers 锚点
            if sampler_clock_anchor is None and recorder.clock_anchor is not None:
                sampler_clock_anchor = recorder.clock_anchor

        if launch is not None:
            result = self._run("launch", launch, timeout=60)
            commands.append(result)
            if result.returncode != 0:
                diagnostics.append(f"android.command_failed:launch:{result.returncode}:{result.stderr}")
                _stop_sampling(scheduler, writer, diagnostics)
                if recorder is not None:
                    recorder.stop(diagnostics)
                _notify_phase(phase_callback, "app_prepare", "failed", "启动应用", result.stderr)
                return commands, diagnostics, sampler_clock_anchor

        if launch is not None and post_launch_wait_s > 0:
            time.sleep(post_launch_wait_s)
        _notify_phase(phase_callback, "app_prepare", "completed", "启动应用")

        # --- interaction phase ---
        _notify_phase(phase_callback, "interaction", "started", "执行测试操作")
        for cmd in plan.interaction_commands:
            if recorder is not None:
                # 180s 硬限分段（SCREEN_RECORD_SYNC §1）：单段到时后收尾并起下一段
                recorder.maybe_rollover(diagnostics)
            if cmd.is_manual:
                if interactive:
                    try:
                        if cmd.timeout_s is not None:
                            completed_by_enter = _input_with_timeout(
                                cmd.prompt,
                                cmd.timeout_s,
                                timeout_message=cmd.timeout_message
                                or f"已达到手动步骤最长等待 {_format_seconds(cmd.timeout_s)}s，自动继续采集。",
                            )
                            if not completed_by_enter:
                                diagnostics.append(
                                    f"flow.manual.timeout:{cmd.name}:{_format_seconds(cmd.timeout_s)}s"
                                )
                        else:
                            print(cmd.prompt)
                            input()
                    except KeyboardInterrupt:
                        diagnostics.append("flow.manual.cancelled")
                        _stop_sampling(scheduler, writer, diagnostics)
                        if recorder is not None:
                            recorder.stop(diagnostics)
                        return commands, diagnostics, sampler_clock_anchor
                else:
                    diagnostics.append("flow.manual.skipped")
                    continue

            if cmd.wait_s > 0 and not cmd.command:
                time.sleep(cmd.wait_s)
                continue

            if cmd.command:
                result = self._run(cmd.name, list(cmd.command))
                commands.append(result)
                if cmd.output_name:
                    output_path = artifact_dir / cmd.output_name
                    output_path.write_text(result.stdout, encoding="utf-8")
                if result.returncode != 0:
                    diagnostics.append(
                        f"android.command_failed:{cmd.name}:{result.returncode}:{result.stderr}"
                    )
                if cmd.wait_s > 0:
                    time.sleep(cmd.wait_s)
        _notify_phase(phase_callback, "interaction", "completed", "执行测试操作")

        if perfetto_deadline is not None:
            remaining = perfetto_deadline - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)

        # collect 阶段第一个命令前停止采样，刷入终止读数（CM-001 §6）
        _stop_sampling(scheduler, writer, diagnostics)

        # collect 前终止 screenrecord：优先自然到时，未到时 pkill -INT 正常收尾封装 MP4
        if recorder is not None:
            recorder.stop(diagnostics)

        # --- collect phase ---
        _notify_phase(phase_callback, "collection", "started", "采集性能数据")
        for name, command, output_name in (
            ("collect_window", plan.collect_window, "window.txt"),
            ("collect_gfxinfo", plan.collect_gfxinfo, "gfxinfo.txt"),
            ("collect_framestats", plan.collect_framestats, "gfxinfo-framestats.txt"),
            ("collect_meminfo", plan.collect_meminfo, "meminfo.txt"),
            ("collect_logcat", plan.collect_logcat, "logcat.txt"),
        ):
            result = self._run(name, command)
            output_path = artifact_dir / output_name
            output_path.write_text(result.stdout, encoding="utf-8")
            commands.append(
                AndroidCommandResult(
                    name=result.name,
                    command=result.command,
                    returncode=result.returncode,
                    stdout=result.stdout,
                    stdout_path=str(output_path),
                    stderr=result.stderr,
                )
            )
            if result.returncode != 0:
                diagnostics.append(f"android.command_failed:{name}:{result.returncode}:{result.stderr}")

        if plan.perfetto_pull is not None and perfetto_started:
            result = self._run("perfetto_pull", plan.perfetto_pull, timeout=60)
            commands.append(result)
            if result.returncode != 0:
                diagnostics.append(f"android.perfetto_pull_failed:{result.stderr}")
            else:
                local_path = Path(plan.perfetto_pull[-1])
                if not local_path.exists():
                    diagnostics.append(f"android.perfetto_pull_missing:{local_path}")

        # pull 录屏到 artifacts/android/（首段必须成功，后续段 best-effort；
        # collect 命令耗时已覆盖 pkill 收尾封装 MP4 的时间）
        if recorder is not None:
            recorder.pull(diagnostics)

        _notify_phase(phase_callback, "collection", "completed", "采集性能数据")

        return commands, diagnostics, sampler_clock_anchor

    def _run(
        self,
        name: str,
        command: list[str],
        *,
        timeout: float | None = 60,
    ) -> AndroidCommandResult:
        try:
            completed = self.command_runner(command, timeout)
        except subprocess.TimeoutExpired as exc:
            return AndroidCommandResult(
                name=name,
                command=tuple(command),
                returncode=124,
                stdout=_timeout_output_to_text(exc.stdout),
                stderr=_timeout_output_to_text(exc.stderr) or f"timed out after {exc.timeout} seconds",
            )
        stdout_value = completed.stdout or ""
        return AndroidCommandResult(
            name=name,
            command=tuple(command),
            returncode=completed.returncode,
            stdout=stdout_value,
            stderr=(completed.stderr or "").strip(),
        )


def collect_android_run(**kwargs) -> AndroidCollectionResult:
    return AndroidRunExecutor().collect(**kwargs)


class _ScreenRecordSession:
    """单次 run 的 screenrecord 生命周期（CM-TASK-020，SCREEN_RECORD_SYNC §1/§5）。

    - probe：`adb shell command -v screenrecord` 只读探测，不支持则 diagnostic 降级；
    - start_first / maybe_rollover：launch 前后台启动首段，交互窗口超过单段
      time-limit 时 pkill 收尾并起下一段（180s 硬限分段）；
    - watchdog（ux/feedback-fixes fix-3）：后台线程每 watchdog_interval_s 检查
      段内耗时，段到时立即收段起下一段——即使主线程阻塞在 manual 步骤的 stdin
      等待中；时钟经 clock 注入便于测试。interval=0 时关闭（单元测试直连驱动）。
      extendable 计划（manual 且未设 maxDurationMs）按需动态生成续段；
    - stop：先停 watchdog 再 `pkill -INT screenrecord` 触发正常收尾封装 MP4；
    - pull：首段必须拉回（失败记 diagnostic），后续段 best-effort（flow 提前结束
      时对应文件不存在属正常）。
    """

    def __init__(
        self,
        executor: AndroidRunExecutor,
        plan: ScreenRecordPlan,
        *,
        finalize_wait_s: float = SCREENRECORD_FINALIZE_WAIT_S,
        commands: list[AndroidCommandResult] | None = None,
        clock: Callable[[], float] = time.monotonic,
        watchdog_interval_s: float = 0.0,
        diagnostics: list[str] | None = None,
    ) -> None:
        self._executor = executor
        self._plan = plan
        self._finalize_wait_s = max(0.0, float(finalize_wait_s))
        self._clock = clock
        self._watchdog_interval_s = max(0.0, float(watchdog_interval_s))
        # F-fix（R1-N3）：screenrecord probe/clock/start/stop/pull 命令结果留痕进
        # result.commands（与 launch 等命令同模式），run 目录可审计实际执行序列
        self._commands = commands if commands is not None else []
        # watchdog 中途 rollover 的 diagnostic 落点（collect 与 runner 共享同一列表）
        self._diagnostics = diagnostics if diagnostics is not None else []
        self._lock = threading.Lock()
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: threading.Thread | None = None
        self.available = True
        self.started = False
        self.active = False
        self.segment_index = 0
        self.segment_started_mono = 0.0
        # CM-TASK-021：boottime↔epoch 锚点（tp 帧事件升格 epoch 语义）与视频锚点
        self.clock_anchor: dict | None = None
        self.video_anchors: dict | None = None

    def _run_tracked(self, name: str, command: list[str], *, timeout: float | None = 15) -> AndroidCommandResult:
        result = self._executor._run(name, command, timeout=timeout)
        self._commands.append(result)
        return result

    @property
    def commands(self) -> list[AndroidCommandResult]:
        return self._commands

    # --- probe / start ---

    def start_first(self, diagnostics: list[str], *, clock_anchor: dict | None = None) -> None:
        if not self._probe(diagnostics):
            return
        # B1（TASK-023-fix R1）：screenrecord 的 boottime↔epoch 读数与采样锚点
        # 同款同刻——调用方在 scheduler.start() 后已读过时直接共用（少一次 adb
        # 调用）；未提供时保留原有自读兜底。
        if clock_anchor is not None:
            self.clock_anchor = clock_anchor
        else:
            self._read_clock_anchor(diagnostics)
        self._start_segment(0, diagnostics)
        if self.started:
            self._record_video_anchor()
            self._start_watchdog()

    def _start_watchdog(self) -> None:
        """段到时中途 rollover 的后台 watchdog（interval=0 表示关闭）。"""
        if self._watchdog_interval_s <= 0.0:
            return
        self._watchdog_stop.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="perfeval-screenrecord-watchdog",
            daemon=True,
        )
        self._watchdog_thread.start()

    def _stop_watchdog(self, *, join_timeout_s: float = 2.0) -> None:
        self._watchdog_stop.set()
        thread = self._watchdog_thread
        if thread is not None:
            # join 超时不视为失败：watchdog 卡在 adb 调用时由锁 + active 标记兜底，
            # 不会在 stop 之后再次翻转
            thread.join(timeout=max(join_timeout_s, self._watchdog_interval_s * 4))
            self._watchdog_thread = None

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.wait(self._watchdog_interval_s):
            with self._lock:
                self._rollover_if_due(self._diagnostics, reason=None)

    def _read_clock_anchor(self, diagnostics: list[str]) -> None:
        """读取设备时钟 + /proc/uptime，建立 boottime↔epoch 锚点（只读）。

        失败时回退 host epoch（误差含主从时钟偏差，标注 approx）。
        """
        command = list(self._plan.start_commands[0])
        anchor_command = command[: command.index("shell") + 1] + ["date +%s%3N; cat /proc/uptime"]
        result = self._run_tracked("screenrecord_clock", anchor_command)
        anchor = parse_device_clock_output(result.stdout, host_epoch_ms=int(time.time() * 1000))
        if anchor is None:
            diagnostics.append("screen.record.clock_anchor_failed:host_epoch_fallback")
            anchor = {"source": "host_epoch_approx", "epochMs": int(time.time() * 1000)}
        self.clock_anchor = anchor

    def _record_video_anchor(self) -> None:
        """记录视频起点锚点：首个可见帧按启动时刻 +1s 编码延迟估算（§2 口径）。"""
        anchor = self.clock_anchor or {}
        start_epoch_ms = anchor.get("epochMs")
        if not isinstance(start_epoch_ms, (int, float)):
            return
        self.video_anchors = {
            "startEpochMs": int(start_epoch_ms),
            "firstFrameEpochMs": int(start_epoch_ms) + VIDEO_FIRST_FRAME_DELAY_MS,
            "method": "videoStart+1000ms encoding delay (SCREEN_RECORD_SYNC §2)",
            "timeLimitS": self._plan.time_limit_s,
            "segments": self._plan.segment_count,
            "clockSource": str(anchor.get("source") or "unknown"),
        }

    def _probe(self, diagnostics: list[str]) -> bool:
        command = list(self._plan.start_commands[0])
        # 探测命令复用 adb 客户端构造（command -v screenrecord，只读）
        probe_command = command[: command.index("shell") + 1] + ["command -v screenrecord"]
        result = self._run_tracked("screenrecord_probe", probe_command)
        if result.returncode != 0 or not result.stdout.strip():
            self.available = False
            diagnostics.append(
                f"screen.record.unavailable:screenrecord_not_found:{result.stderr or result.stdout.strip()}"
            )
            return False
        return True

    def _start_segment(self, index: int, diagnostics: list[str]) -> None:
        segment = self._plan.segment_commands(index)
        if segment is None:
            # 非 extendable 计划的段已用尽：录制按预算封顶
            return
        start_command = segment[0]
        result = self._run_tracked(f"screenrecord_start_seg{index + 1:03d}", list(start_command))
        self.segment_index = index
        self.segment_started_mono = self._clock()
        if result.returncode != 0:
            # adb 侧失败（设备离线/参数不支持等）：降级不阻断
            self.active = False
            if not self.started:
                self.available = False
            diagnostics.append(
                f"screen.record.unavailable:start_failed_seg{index + 1:03d}:{result.returncode}:{result.stderr}"
            )
            return
        self.started = True
        self.active = True

    # --- rollover / stop ---

    def _rollover_if_due(self, diagnostics: list[str], *, reason: str | None) -> bool:
        """段耗时达到 time-limit 时收段起下一段；调用方需持有 self._lock。"""
        if not self.active:
            return False
        elapsed = self._clock() - self.segment_started_mono
        if elapsed < self._plan.time_limit_s:
            return False
        self._stop_active(diagnostics, reason=reason)
        self._start_segment(self.segment_index + 1, diagnostics)
        return True

    def maybe_rollover(self, diagnostics: list[str]) -> None:
        with self._lock:
            self._rollover_if_due(diagnostics, reason=None)

    def stop(self, diagnostics: list[str]) -> None:
        # 先停 watchdog：避免收尾 pkill 与 rollover 竞争（fix-3）
        self._stop_watchdog()
        if not self.available or not self.started:
            return
        with self._lock:
            self._stop_active(diagnostics, reason="collect")

    def _stop_active(self, diagnostics: list[str], *, reason: str | None) -> None:
        self.active = False
        result = self._run_tracked("screenrecord_stop", list(self._plan.stop_command))
        if result.returncode not in (0, 1):
            # 0=已发送 SIGINT 正常收尾；1=已自然到时退出（pkill 无匹配进程）；
            # F-fix（R1-N7）：rollover 与 collect 路径的异常返回码均不留静默
            diagnostics.append(
                f"screen.record.stop_failed:{reason or 'rollover'}:{result.returncode}:{result.stderr}"
            )
        elif result.returncode == 0 and self._finalize_wait_s > 0:
            time.sleep(self._finalize_wait_s)

    # --- pull ---

    def pull(self, diagnostics: list[str]) -> None:
        """拉取各段并校验有效性（smoke-fix：ROM 限制空录屏降级链）。

        pull 成功但文件 < SCREENRECORD_MIN_VALID_BYTES（含 0 字节）判定无有效
        帧数据：记 `screen.record.unavailable:empty recording` diagnostic、删除
        本地无效文件（不入 artifact 索引）；全部段均无效时撤销 clockAnchors.video
        锚点——报告回放区自然降级为"录屏不可用"文案，不渲染 404 video 标签。
        """
        if not self.available or not self.started:
            return
        valid_segments = 0
        for index in range(self.segment_index + 1):
            segment = self._plan.segment_commands(index)
            if segment is None:
                break
            pull_command = segment[1]
            result = self._run_tracked(
                f"screenrecord_pull_seg{index + 1:03d}",
                list(pull_command),
                timeout=120,
            )
            if result.returncode != 0:
                if index == 0:
                    diagnostics.append(
                        f"screen.record.unavailable:pull_failed:{result.returncode}:{result.stderr}"
                    )
                # 后续段 best-effort：失败静默（可能是部分段未写完）
                continue
            if self._discard_empty_segment(index, diagnostics):
                continue
            valid_segments += 1
        if valid_segments == 0:
            # 无任何有效段：撤销视频锚点（离线可复算口径不受影响，boottime 锚点保留）
            self.video_anchors = None

    def _discard_empty_segment(self, index: int, diagnostics: list[str]) -> bool:
        """pull 成功但文件过小（<1KB，ROM 限制等未知原因）时判定无效并清理。"""
        segment = self._plan.segment_commands(index)
        local_path = Path(segment[3]) if segment is not None else Path(self._plan.local_paths[index])
        try:
            size = local_path.stat().st_size if local_path.exists() else 0
        except OSError:
            size = 0
        if size >= SCREENRECORD_MIN_VALID_BYTES:
            return False
        diagnostics.append(
            "screen.record.unavailable:empty recording (ROM 限制等未知原因，建议前台运行 screenrecord 核实)"
            f":{local_path.name}:{size}B"
        )
        try:
            if local_path.exists():
                local_path.unlink()
        except OSError:
            pass
        return True


def _stop_sampling(
    scheduler: SamplingScheduler | None,
    writer: SamplesWriter | None,
    diagnostics: list[str],
) -> None:
    """停止采样调度并刷入 samples.ndjson；幂等，供各退出路径复用。

    停止后统一 drain 调度器级与各 sampler 内部级 diagnostic（cpu/net/power/gpu_thermal
    采样器自带内部诊断缓冲），落点与调度器 diagnostic 相同：run diagnostics → manifest
    / summary。
    """
    if scheduler is None:
        return
    scheduler.stop()
    diagnostics.extend(scheduler.drain_diagnostics())
    for sampler in scheduler.samplers:
        drain = getattr(sampler, "drain_diagnostics", None)
        if callable(drain):
            diagnostics.extend(drain())
    if writer is not None:
        writer.close()


def _notify_phase(
    callback: PhaseCallback | None,
    phase_id: str,
    state: str,
    label: str,
    detail: str | None = None,
) -> None:
    if callback is not None:
        callback(phase_id, state, label, detail)


def _timeout_output_to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return str(value)


def _launcher_activity_resolution_error(
    package_name: str,
    result: AndroidCommandResult,
) -> str:
    message = (
        f"无法根据包名 {package_name} 自动识别 Launcher Activity；"
        "请确认应用已安装且包含桌面入口，或在“更换 APK 或应用”中填写 Activity"
        "（CLI 使用 --activity）。"
    )
    adb_detail = result.stderr.strip() or result.stdout.strip()
    if adb_detail:
        return f"{message} ADB 返回：{adb_detail}"
    return message


def _input_with_timeout(prompt: str, timeout_s: float, *, timeout_message: str) -> bool:
    wait_s = max(1.0, float(timeout_s))
    if not _stdin_timeout_supported():
        try:
            input(prompt)
            return True
        except EOFError:
            print(timeout_message)
            return False

    print(prompt, end="", flush=True)
    try:
        ready, _, _ = select.select([sys.stdin], [], [], wait_s)
    except (OSError, ValueError):
        try:
            input()
            return True
        except EOFError:
            print()
            print(timeout_message)
            return False
    if ready:
        sys.stdin.readline()
        return True
    print()
    print(timeout_message)
    return False


def _stdin_timeout_supported() -> bool:
    try:
        sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        return False
    return bool(sys.stdin.isatty())


def _format_seconds(value: float) -> str:
    as_float = float(value)
    if as_float.is_integer():
        return str(int(as_float))
    return f"{as_float:g}"


def _derive_startup_event(commands: list[AndroidCommandResult], *, run_id: str) -> dict[str, Any] | None:
    """从 am start -W 的 stdout 中解析 TotalTime，派生 startup.cold.ms 指标事件。"""
    launch_result = next((cmd for cmd in commands if cmd.name == "launch"), None)
    if launch_result is None or not launch_result.stdout:
        return None
    total_match = _AM_START_TOTAL_TIME_RE.search(launch_result.stdout)
    if total_match is None:
        return None
    total_ms = int(total_match.group(1))
    state_match = _AM_START_LAUNCH_STATE_RE.search(launch_result.stdout)
    launch_state = state_match.group(1).lower() if state_match else "unknown"
    return {
        "schemaVersion": "perfeval.timeline.v1",
        "seq": 1,
        "runId": run_id,
        "eventId": f"{run_id}-android-startup-001",
        "type": "metric",
        "name": "startup.cold.ms",
        "tsMonotonicNs": 0,
        "source": {
            "platform": "android",
            "collector": "am_start",
            "evidenceLevel": "L0",
        },
        "data": {
            "value": float(total_ms),
            "unit": "ms",
            "direction": "lower_better",
            "launchState": launch_state,
            "source": "am_start -W TotalTime",
        },
    }


def _clear_previous_artifacts(artifact_dir: Path) -> None:
    for filename in (
        "gfxinfo.txt",
        "gfxinfo-framestats.txt",
        "window.txt",
        "meminfo.txt",
        "logcat.txt",
        "perfetto.pftrace",
        "samples.ndjson",
    ):
        path = artifact_dir / filename
        try:
            if path.exists():
                path.unlink()
        except OSError:
            # A later command will surface an actionable write/pull failure.
            pass
    # 录屏文件（含分段 screen-segNNN.mp4）同样按 run 清理
    for path in artifact_dir.glob("screen*.mp4"):
        try:
            path.unlink()
        except OSError:
            pass
