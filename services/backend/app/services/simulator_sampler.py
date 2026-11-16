"""simulator 采集执行（M-iOS，契约 §23.2）。

- ``SimulatorCollector``：宿主口径采样线程——``ps -o pid,pcpu,rss,comm -p <pid>``
  一条命令同取被测 App 进程 CPU%（cpu.proc.host.pct）与 RSS MB（memory.rss.mb）；
  进程不存在 → diagnostic(app.process.not_found) + value:null（契约 §6.1 real
  路径同语义）；FPS 无逐帧公开 API，指标在 iOS 设备上 available=false
  （platform_unsupported）不会进会话指标集，采集器层面不造数。
- ``SimulatorScreenRecorder``：``simctl io <udid> recordVideo --codec=h264
  <artifact>/screen.mp4`` 子进程生命周期（SIGINT 收尾 → 文件落盘 → <1KB 判废）。
  单段无 rollover（simctl 无分段机制，如实声明，§23.3）；video 锚点
  startEpochMs/method 由本模块产出（save 时经 derive_clock_anchors 落盘）。
- ``capture_simulator_screenshot_bytes``：``simctl io <udid> screenshot <tmp>``
  → PNG 字节（simctl 不支持 stdout 直出，经临时文件读取）。
- ``simctl_log_source``/``SimctlLogSource``：``log show --last 10s --style
  compact`` 周期轮询行源（已发布行去重），级别字母映射契约 logLevel（§21.7）。

诚实性口径（§23.2，写进 tags/description/文档）：CPU/内存为**模拟器宿主口径**
（iOS 应用进程 = macOS 宿主进程），tags.basis="host"；数值与真机 iOS 不可直接
对比。
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from ..providers import simctl
from ..sse import now_ms
from .collector_service import BaseCollector

# 进程级指标（宿主口径两指标均进程级：App 进程不存在即 null，§6.1 同语义）
PROCESS_METRICS = ("cpu.proc.host.pct", "memory.rss.mb")

# <1KB 判废（对齐 perfeval SCREENRECORD_MIN_VALID_BYTES 与契约 §16.1 判废口径）
MIN_VALID_VIDEO_BYTES = 1024

_PS_TIMEOUT_S = 8.0
_SCREENSHOT_TIMEOUT_S = 30.0
_PS_ARGS = ("-o", "pid,pcpu,rss,comm")  # rss 单位 KB（macOS ps 约定）


def _run(command: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
    try:
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
    except (OSError, subprocess.SubprocessError):
        return None


def _sample_record(metric_id: str, value: float, tags: dict[str, Any]) -> Any:
    from perfeval.collectors.android.samplers.base import SampleRecord

    return SampleRecord(
        metric_id=metric_id,
        value=float(value),
        ts_ms=int(time.time() * 1000),  # epoch ms 直出（to_wall_ms 原样透传）
        tags=dict(tags),
    )


class SimulatorCollector(BaseCollector):
    """宿主口径采样（simulator 模式）：ps 单命令同取 CPU%/RSS，per-metric due 调度。"""

    def __init__(self, session: Any, bus: Any, ping_s: float = 60.0) -> None:
        super().__init__(session, bus, ping_s=ping_s)
        self._last_pid_check = 0.0
        self._pid_notified = False
        self._current_pid: int | None = None
        # pid/ps 注入面（测试替换，无模拟器常驻驱动采集循环）
        self._pid_resolver: Callable[[], int | None] = self._target_pid_via_simctl
        self._ps_sampler: Callable[[int], tuple[float, float] | None] = self._sample_ps
        # 装配流 S4 顺手修缮：pid 探测复用同一 SimulatorProvider 实例——原实现
        # 每秒新建实例（_apps_cache 随实例丢弃失效 + 对象churn）；launchctl
        # 子进程轮询本身是探测语义，保留。
        self._sim_provider: Any | None = None

    # -- pid 定位 --

    def _target_pid_via_simctl(self) -> int | None:
        """被测 App 进程 pid（launchctl list UIKitApplication 表）。"""
        if self._sim_provider is None:
            from ..providers.simulator import SimulatorProvider  # 局部导入避免环

            self._sim_provider = SimulatorProvider()
        bundles = self._sim_provider._running_bundles(self.session.device_id)
        return bundles.get(self.session.package_name)

    def _sample_ps(self, pid: int) -> tuple[float, float] | None:
        """宿主 ps → (pcpu, rss_mb)；进程消失/命令失败 → None（不猜）。"""
        completed = _run(["ps", *_PS_ARGS, "-p", str(pid)], timeout=_PS_TIMEOUT_S)
        if completed is None or completed.returncode != 0:
            return None
        return simctl.parse_ps_stat(completed.stdout)

    # -- 调度（SyntheticCollector 同款 per-metric due 循环）--

    def _emit_due(self, now: float) -> None:
        self._check_process(now)
        pid = self._current_pid
        if pid is None:
            return
        due_metrics = [
            metric_id for metric_id in list(self.session.metrics)
            if metric_id in PROCESS_METRICS and self._is_due(metric_id, now)
        ]
        if not due_metrics:
            return
        stat = self._ps_sampler(pid)
        for metric_id in due_metrics:
            self._schedule_next(metric_id, now)
            if stat is None:
                # 进程消失窗口内的单点：契约 §6.1 null 语义（仅 SSE，不落盘）
                self.emit_null_sample(metric_id, tags={"basis": "host"})
                continue
            value = stat[0] if metric_id == "cpu.proc.host.pct" else stat[1]
            self.emit_sample(_sample_record(metric_id, value, {"basis": "host"}))

    def _is_due(self, metric_id: str, now: float) -> bool:
        due = self._next_due.get(metric_id)
        return due is None or now >= due

    def _on_start(self) -> None:
        # M-iOS 时钟锚点：模拟器与宿主共享内核时钟，host epoch 即设备墙钟。
        # 形状经 derive_clock_anchors._normalize_samplers_anchor 映射
        # （epochMs → startEpochMs，source 原样保留落盘）。
        self.session.sampler_clock_anchor = {
            "epochMs": now_ms(),
            "source": "host_epoch",
        }

    def set_enabled(self, metric_ids: dict[str, int]) -> None:
        self.session.set_metrics(dict(metric_ids))

    # -- 进程探测（§6.1：不存在 → 诊断 + null 点，RealCollector 同语义）--

    def _check_process(self, now: float) -> None:
        if now - self._last_pid_check < 1.0:
            return
        self._last_pid_check = now
        pid = self._pid_resolver()
        if pid is not None:
            self._current_pid = pid
            self._pid_notified = False
            return
        self._current_pid = None
        if not self._pid_notified:
            self._pid_notified = True
            self.emit_diagnostic(
                "app.process.not_found",
                "warn",
                f"目标进程 {self.session.package_name} 不存在，进程级指标出 null",
            )
        for metric_id in list(self.session.metrics):
            if metric_id in PROCESS_METRICS:
                self.emit_null_sample(metric_id, tags={"basis": "host"})


class SimulatorScreenRecorder:
    """``simctl io recordVideo`` 会话录屏生命周期（M-iOS §23.3）。

    - start：子进程启动（``--codec=h264``，产物直接写本地 artifact 目录）；
    - stop：SIGINT 收尾（simctl 以 SIGINT 正常收尾写盘，实测 6s → 110KB MP4）；
      <1KB 判废删除；video 锚点 startEpochMs/method 恒产出（判废后 None）。
    - 单段无 rollover：simctl recordVideo 无分段机制，录到 stop 为止（契约
      §23.3 如实声明，与 perfeval 175s 分段/watchdog 机制不同源不同承诺）。
    """

    def __init__(
        self,
        *,
        session: Any,
        artifact_dir: Path,
        case_name: str,
        clock: Callable[[], float] = time.monotonic,
        popen: Callable[..., Any] | None = None,
    ) -> None:
        self._session = session
        self._artifact_dir = artifact_dir
        self._case_name = case_name
        self._clock = clock
        self._popen = popen or self._default_popen
        self.diagnostics: list[str] = []
        self._process: Any = None
        self._start_epoch_ms: int | None = None
        self._artifact_path: Path | None = None

    @staticmethod
    def _default_popen(command: list[str]) -> Any:
        return subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    @property
    def video_anchors(self) -> dict[str, Any] | None:
        if self._start_epoch_ms is None:
            return None
        return {
            "startEpochMs": self._start_epoch_ms,
            # 首帧无独立时戳（simctl 不提供）：以 start+1s 估算口径声明，不伪造
            "firstFrameEpochMs": self._start_epoch_ms + 1000,
            "method": "simctl.io.recordVideo（首帧=start+1s 编码延迟估算，无独立时戳源）",
            "segments": 1,
            "clockSource": "host_epoch",
        }

    def start(self) -> None:
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        self._artifact_path = self._artifact_dir / "screen.mp4"
        command = [
            "xcrun", "simctl", "io", self._session.device_id,
            "recordVideo", "--codec=h264", str(self._artifact_path),
        ]
        try:
            self._process = self._popen(command)
        except (OSError, subprocess.SubprocessError) as exc:
            self.diagnostics.append(f"screen.record.unavailable:simctl_start_failed:{exc}")
            self._process = None
            return
        self._start_epoch_ms = int(time.time() * 1000)

    def stop(self) -> None:
        process = self._process
        if process is None:
            return
        self._process = None
        if process.poll() is None:
            try:
                process.send_signal(signal.SIGINT)
            except (OSError, subprocess.SubprocessError) as exc:
                self.diagnostics.append(f"screen.record.unavailable:simctl_stop_failed:{exc}")
        try:
            process.wait(timeout=15.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5.0)
            self.diagnostics.append("screen.record.unavailable:simctl_stop_timeout")
        # <1KB 判废（契约 §16.1 同口径；SIGINT 过快中断时产物为空壳）
        try:
            if self._artifact_path is not None and self._artifact_path.exists():
                if self._artifact_path.stat().st_size < MIN_VALID_VIDEO_BYTES:
                    self._artifact_path.unlink()
                    self.diagnostics.append("screen.record.unavailable:artifact_invalidated")
                    self._start_epoch_ms = None
        except OSError as exc:
            self.diagnostics.append(f"screen.record.unavailable:artifact_check_failed:{exc}")

    def drain_diagnostics(self) -> list[str]:
        drained = list(self.diagnostics)
        self.diagnostics.clear()
        return drained


def capture_simulator_screenshot_bytes(
    device_id: str,
    *,
    timeout: float = _SCREENSHOT_TIMEOUT_S,
    runner: Callable[[list[str], float], Any] | None = None,
) -> bytes:
    """``simctl io <udid> screenshot <tmp>`` → PNG 字节（M-iOS §23.3）。

    simctl 无 stdout 直出，经临时文件读取后删除。执行失败/产物非 PNG →
    ``artifact_pull_failed``（§3 既有码，details.phase="screenshot"——与 Android
    screencap 失败同语义）。``runner`` 注入面供测试驱动（不触真机/模拟器）。
    """
    from ..errors import ApiError

    png_magic = b"\x89PNG\r\n\x1a\n"
    with tempfile.TemporaryDirectory(prefix="pd-simshot-") as tmp:
        target = os.path.join(tmp, "screenshot.png")
        command = ["xcrun", "simctl", "io", device_id, "screenshot", target]
        completed = runner(command, timeout) if runner is not None else _run(command, timeout)
        if completed is None:
            raise ApiError(
                "artifact_pull_failed", "screenshot 执行失败（simctl 不可用）",
                details={"phase": "screenshot"},
            )
        if completed.returncode != 0 or not os.path.exists(target):
            detail = (completed.stderr or completed.stdout or "").strip()[:200]
            raise ApiError(
                "artifact_pull_failed",
                f"simctl screenshot 返回 {completed.returncode}: {detail}",
                details={"phase": "screenshot"},
            )
        with open(target, "rb") as handle:
            data = handle.read()
    if not data.startswith(png_magic):
        raise ApiError(
            "artifact_pull_failed",
            f"simctl screenshot 产物非 PNG（{len(data)} 字节，魔数不符）",
            details={"phase": "screenshot"},
        )
    return data


# ---- R33 实时日志流（M-iOS §23.4：simctl log show 轮询行源）----

# `log show --style compact` 行：`2026-11-03 23:40:59.882 Df chronod[80616:tid] …`
_COMPACT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+ ([A-Z])[a-z]? (\S+)\[\d+(?::[0-9a-fx]+)?\] ?"
)
_LEVEL_BY_LETTER = {"V": "verbose", "D": "debug", "I": "info", "W": "warn", "E": "error"}
_POLL_WINDOW_S = 10
_POLL_INTERVAL_S = 1.0
_SEEN_CAP = 5000


def parse_simctl_log_line(line: str) -> tuple[str, str, str] | None:
    """``log show --style compact`` 行 → (level, source, message)。

    级别字母（Df/De/If/Er…——首字母为级别、次字母为 flags）映射契约 logLevel；
    无级别可判的行（多行消息续行等）→ None（不伪造，logcat 解析器同哲学）。
    """
    match = _COMPACT_RE.match(line)
    if match is None:
        return None
    level = _LEVEL_BY_LETTER.get(match.group(1))
    if level is None:
        return None
    return level, match.group(2), line[match.end():].lstrip()


class SimctlLogSource:
    """``log show --last Ns --style compact`` 周期轮询行源（已发布行去重）。

    生命周期与 LogStreamChannel 线程同构：``lines()`` 生成器循环轮询，stop 后
    判停退出。轮询而非 ``log stream``：show 可控窗口、生命周期简单（无子进程
    信号收尾），重复行以行原文 set 去重（有界，超限清空重来）。
    """

    def __init__(
        self,
        device_id: str,
        *,
        stop_event: threading.Event | None = None,
        sleep: Callable[[float], Any] | None = None,
        window_s: int = _POLL_WINDOW_S,
        interval_s: float = _POLL_INTERVAL_S,
        runner: Callable[[list[str], float], Any] | None = None,
    ) -> None:
        self._device_id = device_id
        self._stop_event = stop_event or threading.Event()
        self._sleep = sleep if sleep is not None else self._stop_event.wait
        self._window_s = max(1, int(window_s))
        self._interval_s = interval_s
        self._runner = runner
        self._seen: set[str] = set()

    def lines(self) -> Iterator[str]:
        while not self._stop_event.is_set():
            completed = self._poll()
            if completed is not None and completed.returncode == 0:
                for line in (completed.stdout or "").splitlines():
                    if not line or line in self._seen:
                        continue
                    if parse_simctl_log_line(line) is None:
                        continue
                    if len(self._seen) >= _SEEN_CAP:
                        self._seen.clear()
                    self._seen.add(line)
                    yield line
            if self._stop_event.wait(self._interval_s):
                break

    def _poll(self) -> Any:
        command = [
            "xcrun", "simctl", "spawn", self._device_id, "log", "show",
            "--last", f"{self._window_s}s", "--style", "compact",
        ]
        if self._runner is not None:
            return self._runner(command, 20.0)
        return _run(command, timeout=20.0)


def simctl_log_source(device_id: str) -> Callable[[], Iterator[str] | None]:
    """行源工厂（session_manager._start_log_stream 接线，adb_logcat_time_source 同形）。

    返回 ``() -> Iterator[str] | None``：None = 通道不可用（log 首轮命令失败，
    由 LogStreamChannel 出诊断）。
    """
    import logging

    logger = logging.getLogger("pd-backend.simctl-log")

    def source() -> Iterator[str] | None:
        # 可用性预检：simctl log 一次轻量探测（失败 → None → 通道诊断）
        probe = _run(
            [
                "xcrun", "simctl", "spawn", device_id, "log", "show",
                "--last", "1s", "--style", "compact",
            ],
            timeout=20.0,
        )
        if probe is None or probe.returncode != 0:
            logger.info("simctl log 探测失败（device=%s）", device_id)
            return None
        stop_event = threading.Event()
        generator = SimctlLogSource(device_id, stop_event=stop_event).lines()

        def lines() -> Iterator[str]:
            try:
                for line in generator:
                    yield line
            finally:
                # 生成器收尾：LogStreamChannel close 时经 finally 判停退出
                stop_event.set()

        return lines()

    return source
