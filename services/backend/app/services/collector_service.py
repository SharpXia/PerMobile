"""采集执行（ARCH §4.1/§4.3/§4.4）。

- SyntheticCollector：确定性种子合成指标流（H5），按各指标 intervalMs 出点，
  另负责 60s ping 事件；
- RealCollector：复用 perfeval SamplingScheduler（子包路径 perfeval.collectors
  .android.samplers，ARCH §1 E8 实测修正），on_record → 会话缓冲 + SSE；
  per-metric 启停以 enabled 集合过滤（R26 注记：先满足「停止出点」）；
  目标进程不存在时进程级指标出 diagnostic(app.process.not_found) + value:null
  （契约 §6.1 real 路径）。

采集是同步阻塞调用，统一在 daemon 线程驱动；sample/diagnostic 经 SseBus 扇出。
"""

from __future__ import annotations

import hashlib
import math
import random
import subprocess
import threading
import time
from dataclasses import replace
from typing import Any

from perfeval.collectors.android.adb import ADB_CREATION_FLAGS, AdbClient
from perfeval.collectors.android.samplers import build_samplers, register_sampler
from perfeval.collectors.android.samplers.base import (
    DeviceContext,
    SampleRecord,
    SamplingScheduler,
)
from perfeval.collectors.android.samplers.cpu_sampler import CpuSampler
from perfeval.collectors.android.timebase import parse_device_clock_output

from ..errors import ApiError
from ..registry import get_metric
from ..sse import SseBus, now_ms
from .cpu_advanced import (
    CpuFreqNormalizedSampler,
    CpuThreadSampler,
    ProcessTargetCpuSampler,
)
from .meminfo_detail import MemDetailSampler
from .fps_sampler import FpsSampler

# M6 采样器注册（backend 侧扩展 perfeval SAMPLER_FACTORIES；间隔缺省对齐契约
# §21.2/§21.3 defaultIntervalMs）
register_sampler(
    "cpu_adv",
    lambda *, interval_ms=1000: CpuFreqNormalizedSampler(interval_ms=interval_ms),
)
register_sampler(
    "cpu_thread",
    lambda *, interval_ms=2000: CpuThreadSampler(interval_ms=interval_ms),
)
register_sampler(
    "mem_detail",
    lambda *, interval_ms=2000: MemDetailSampler(interval_ms=interval_ms),
)
# FPS real 通道（矩阵 R01，契约 §5 注 2：gfxinfo 周期 reset+轮询，ARCH §9.3 选型
# 落地）；契约 §21.5（S2 接线 2026-11-10）：fpsWindow entry 经 build_samplers 的
# 签名透传（camelCase→snake_case）送达 FpsSampler，三通道按窗口过滤
register_sampler(
    "fps",
    lambda *, interval_ms=500, fps_window=None: FpsSampler(
        interval_ms=interval_ms, fps_window=fps_window
    ),
)

# metricId -> 采样器名（perfeval SAMPLER_FACTORIES 注册名）
SAMPLER_BY_METRIC: dict[str, str] = {
    "cpu.total.pct": "cpu",
    "cpu.proc.pct": "cpu",
    "frame.fps.mean": "fps",
    "memory.pss.peak_mb": "mem",
    "net.rx.kbps": "net",
    "net.tx.kbps": "net",
    "power.current.ma": "power",
    "power.level.pct": "power",
    "power.energy.mah": "power",
    "gpu.busy.pct": "gpu_thermal",
    "thermal.max.c": "gpu_thermal",
    # ---- M6（契约 §21.2/§21.3）：R16 CPU 高阶 + R17 内存明细 ----
    "cpu.freq.mhz": "cpu_adv",
    "cpu.proc.normalized.pct": "cpu_adv",
    "cpu.proc.thread.pct": "cpu_thread",
    "memory.detail.native_mb": "mem_detail",
    "memory.detail.dalvik_mb": "mem_detail",
    "memory.detail.gfx_mb": "mem_detail",
    "memory.detail.gl_mb": "mem_detail",
}

# ---- S1 新指标映射（metrics-registry-contract 注册表行落地即生效）----
# 登记以 S1 注册表行（app.registry.METRICS）为唯一事实源：注册表行未落地前条目
# 不进活跃映射（会话创建侧 is_registered 校验先行，未注册 metricId 进不了会话
# 指标集——inert 条目）；S1 注册表落地后自动激活，无需二次编辑。一致性测试
# （test_fps_real_channel.py：SAMPLER_BY_METRIC 键 ⊆ registry.METRICS 键）保证
# 两流不漂移：活跃映射任何时刻都是注册表键的子集。
S1_PENDING_METRIC_MAPPINGS: dict[str, str] = {
    "gpu.freq.mhz": "gpu_thermal",
    "thermal.cpu.c": "gpu_thermal",
    "thermal.gpu.c": "gpu_thermal",
    "thermal.btemp.c": "gpu_thermal",
    "memory.swap.mb": "mem_detail",
    "memory.vss.mb": "mem_detail",
    "memory.available.mb": "mem_detail",
}


def _activate_pending_metric_mappings() -> None:
    """S1 注册表行已落地的待激活映射并入活跃映射（幂等；模块导入时执行一次）。"""
    from ..registry import is_registered

    for metric_id, sampler_name in S1_PENDING_METRIC_MAPPINGS.items():
        if is_registered(metric_id):
            SAMPLER_BY_METRIC.setdefault(metric_id, sampler_name)


_activate_pending_metric_mappings()

# 进程级指标（契约 §6.1：目标进程不存在时出 null）
PROCESS_METRICS = (
    "cpu.proc.pct", "memory.pss.peak_mb", "net.rx.kbps", "net.tx.kbps",
    "cpu.proc.normalized.pct", "cpu.proc.thread.pct", "frame.fps.mean",
    "memory.detail.native_mb", "memory.detail.dalvik_mb",
    "memory.detail.gfx_mb", "memory.detail.gl_mb",
)

_TICK_S = 0.02


class BaseCollector:
    """采集线程骨架：per-metric due 调度 + sample/diagnostic/ping 发布。"""

    def __init__(self, session: Any, bus: SseBus, ping_s: float = 60.0) -> None:
        self.session = session
        self.bus = bus
        self._ping_s = max(0.1, float(ping_s))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_due: dict[str, float] = {}
        self._last_ping = time.monotonic()
        self._clock = time.monotonic

    # -- 生命周期 --

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"collector-{self.session.id}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    # -- 子类钩子 --

    def _emit_due(self, now: float) -> None:
        """到期指标出点；返回下一个最近到期时间（秒）。"""
        raise NotImplementedError

    def _on_start(self) -> None:  # noqa: B027
        pass

    def _on_stop(self) -> None:  # noqa: B027
        pass

    def _run(self) -> None:
        self._on_start()
        try:
            while not self._stop_event.is_set():
                now = self._clock()
                if now - self._last_ping >= self._ping_s:
                    self._last_ping = now
                    self.bus.publish("ping")
                self._emit_due(now)
                self._sleep_tick()
        finally:
            self._on_stop()

    def _sleep_tick(self) -> None:
        self._stop_event.wait(_TICK_S)

    # -- 发布 --

    def emit_sample(self, record: SampleRecord) -> None:
        spec = get_metric(record.metric_id)
        # perfeval 采样器时戳为相对 elapsed ms → 归一 epoch ms（契约 §1）后
        # 再进缓冲/SSE/落盘，三处同源一致
        ts_ms = self.session.to_wall_ms(record.ts_ms)
        if ts_ms != record.ts_ms:
            record = replace(record, ts_ms=ts_ms)
        self.session.append_record(record)
        self.bus.publish(
            "sample",
            metricId=record.metric_id,
            value=record.value,
            unit=spec.unit,
            tags=dict(record.tags),
            tsMs=record.ts_ms,  # perfeval SampleRecord 同构直传（契约 §10）
        )

    def emit_null_sample(self, metric_id: str, tags: dict[str, Any] | None = None) -> None:
        self.session.append_null_record(metric_id, tags)
        spec = get_metric(metric_id)
        self.bus.publish(
            "sample",
            metricId=metric_id,
            value=None,
            unit=spec.unit,
            tags=dict(tags or {}),
            tsMs=now_ms(),
        )

    def emit_diagnostic(
        self, code: str, severity: str, message: str, detail: Any = None
    ) -> None:
        self.bus.publish(
            "diagnostic",
            code=code,
            severity=severity,
            message=message,
            detail=detail,
        )

    def _schedule_next(self, metric_id: str, now: float) -> None:
        interval = self.session.metrics.get(metric_id, 1000) / 1000.0
        self._next_due[metric_id] = now + interval


class SyntheticCollector(BaseCollector):
    """确定性合成流：值由 sha256(sessionId|metricId) 种子驱动，测试可复现。"""

    def __init__(self, session: Any, bus: SseBus, ping_s: float = 60.0) -> None:
        super().__init__(session, bus, ping_s=ping_s)
        self._generators: dict[str, _SyntheticGenerator] = {}

    def set_enabled(self, metric_ids: dict[str, int]) -> None:
        """PATCH /metrics 生效点：增删合成序列（ARCH §4.4 注记：synthetic 无约束）。"""
        self.session.set_metrics(dict(metric_ids))
        self._generators = {
            m: g for m, g in self._generators.items() if m in metric_ids
        }

    def _generator_for(self, metric_id: str) -> "_SyntheticGenerator":
        gen = self._generators.get(metric_id)
        if gen is None:
            gen = _SyntheticGenerator(self.session.id, metric_id)
            self._generators[metric_id] = gen
        return gen

    def _emit_due(self, now: float) -> None:
        for metric_id in list(self.session.metrics):
            due = self._next_due.get(metric_id)
            if due is not None and now < due:
                continue
            spec = get_metric(metric_id)
            if not spec.synthetic_available:
                self._schedule_next(metric_id, now)
                continue
            self._next_due.setdefault(metric_id, now)
            # 契约 §21.5：frame.fps.mean tags 携带 fpsWindow 来源标注（synthetic
            # 窗口枚举演示；消费方忽略未知 tags 键，§8.3）
            extra_tags: dict[str, Any] = {}
            if metric_id == "frame.fps.mean":
                extra_tags["fpsWindow"] = self.session.fps_window_resolved
            for value, tags in self._generator_for(metric_id).next_values():
                self.emit_sample(
                    SampleRecord(
                        metric_id=metric_id,
                        value=value,
                        ts_ms=now_ms(),
                        tags={"unit": spec.unit, **extra_tags, **tags},
                    )
                )
            self._schedule_next(metric_id, now)

    def _on_start(self) -> None:
        self.emit_diagnostic(
            "flow.synthetic.started",
            "info",
            "synthetic 指标流开始（无真机合成数据，H5）",
        )


class RealCollector(BaseCollector):
    """real 采集：SamplingScheduler 线程驱动，诊断经 SSE 广播（ARCH §4.3）。"""

    def __init__(
        self,
        session: Any,
        bus: SseBus,
        *,
        adb: AdbClient | None = None,
        ping_s: float = 60.0,
        serial: str | None = None,
        fps_window_resolved: str | None = None,
    ) -> None:
        super().__init__(session, bus, ping_s=ping_s)
        # WiFi 行会话（id 带 -wifi 后缀，契约 §21.1）：device_id 不是 adb serial，
        # 须经 provider.adb_serial 映射回 host:port；USB 行 device_id 即 serial
        # （默认回退）。缺失映射时采样命令会打到不存在的设备上静默失败
        # （2026-11-04 WiFi 数据面实测暴露）。
        self._serial = serial or session.device_id
        # 契约 §21.5（S2 接线 2026-11-10）：resolve_fps_window 解析结果经
        # create_collector 新参送达；未显式传入时回退会话已解析属性（router 创建
        # 路径不传参——Session.__init__ 已按 auto 策略解析，单一事实源不变）
        self._fps_window_resolved = (
            fps_window_resolved
            if fps_window_resolved is not None
            else getattr(session, "fps_window_resolved", None)
        )
        self._adb = adb or AdbClient()
        self._scheduler: SamplingScheduler | None = None
        self._enabled_lock = threading.Lock()
        self._scheduler_diagnostics: list[str] = []
        self._diag_lock = threading.Lock()
        self._last_pid_check = 0.0
        self._pid_notified = False

    def _build_scheduler(self) -> SamplingScheduler:
        config: dict[str, dict[str, Any]] = {}
        for metric_id, interval in self.session.metrics.items():
            sampler_name = SAMPLER_BY_METRIC.get(metric_id)
            if sampler_name is None:
                continue
            entry = config.setdefault(sampler_name, {"intervalMs": interval})
            entry["intervalMs"] = min(entry["intervalMs"], interval)
            if sampler_name == "fps" and self._fps_window_resolved:
                # 契约 §21.5（S2 接线）：窗口配置经 build_samplers 签名透传送达
                # FpsSampler（fpsWindow → fps_window），三通道按窗口过滤
                entry["fpsWindow"] = {"type": self._fps_window_resolved}
        samplers = build_samplers(config)
        # M6 R20 子进程目标：cpu/mem 维度采样按 processName 定位 pid（pidof/
        # dumpsys meminfo 接受完整进程名，经 ctx.package_name 改道复用既有解析）；
        # net/power 维度保持 packageName（net 为 uid 级、同包各进程共享 uid）
        samplers = [_retarget_sampler(s, self.session.process_name) for s in samplers]

        def on_record(record: SampleRecord) -> None:
            with self._enabled_lock:
                enabled = self.session.metrics
            if record.metric_id in enabled:
                self.emit_sample(record)

        context = DeviceContext(
            adb=self._adb,
            command_runner=_default_command_runner,
            serial=self._serial,
            package_name=self.session.package_name,
        )
        return SamplingScheduler(samplers, context=context, on_record=on_record)

    def _on_start(self) -> None:
        self._scheduler = self._build_scheduler()
        self._scheduler.probe()
        self._drain_diagnostics()
        self._scheduler.start()
        self._drain_diagnostics()
        # M4（契约 §16.1 四锚点 / perfeval B1 TASK-023-fix R1 同语义）：采样启动
        # 同刻的设备时钟读数 → session.sampler_clock_anchor（save 时经
        # derive_clock_anchors._normalize_samplers_anchor 落盘 clockAnchors.samplers）。
        # 只读探测，失败置 None（无锚点，不伪造）。
        self._read_sampler_clock_anchor()

    def _read_sampler_clock_anchor(self) -> None:
        command = self._adb.command(
            "shell", "date +%s%3N; cat /proc/uptime", serial=self._serial
        )
        try:
            completed = _default_command_runner(command, 5)
        except (OSError, subprocess.SubprocessError):
            return
        anchor = parse_device_clock_output(completed.stdout, host_epoch_ms=now_ms())
        if anchor is not None:
            self.session.sampler_clock_anchor = anchor

    def _on_stop(self) -> None:
        if self._scheduler is not None:
            try:
                self._scheduler.stop()
            finally:
                self._drain_diagnostics()
                self._scheduler = None

    def _drain_diagnostics(self) -> None:
        if self._scheduler is None:
            return
        for diag in self._scheduler.drain_diagnostics():
            self.emit_diagnostic(diag, "warn", f"采样器诊断: {diag}")
        # 实机验证修复（2026-11-03）：sampler 内部级诊断（fps 通道升级
        # fps.source.fallback_surfaceflinger/escalate_timestats、mem/power 连击等）
        # 挂在各 sampler 自身缓冲——perfeval runner 收口时统一 drain
        # （runner.py:775-779 同口径）；后端调度此前只 drain 调度器级，会话内
        # 通道升级对用户不可见（真机实测复现）。emit_due 每轮调用本方法，
        # 在此一并接走；drain 幂等（取走即清）。
        for sampler in self._scheduler.samplers:
            drain = getattr(sampler, "drain_diagnostics", None)
            if not callable(drain):
                continue
            try:
                for diag in drain():
                    self.emit_diagnostic(diag, "warn", f"采样器诊断: {diag}")
            except Exception:  # noqa: BLE001 —— 诊断收集失败不影响采样
                continue

    def set_enabled(self, metric_ids: dict[str, int]) -> None:
        with self._enabled_lock:
            self.session.set_metrics(dict(metric_ids))

    def _emit_due(self, now: float) -> None:
        self._drain_diagnostics()
        self._check_process(now)
        # SamplingScheduler 自带轮询线程；此处仅守护诊断与进程探测

    def _check_process(self, now: float) -> None:
        """契约 §6.1：launchOnCreate=false 且进程不存在 → diagnostic + value:null。

        M6 R20：processName 提供时按子进程名探测（与采样定位同一目标）。
        """
        target = self.session.process_name or self.session.package_name
        if not target or self._scheduler is None:
            return
        if now - self._last_pid_check < 1.0:
            return
        self._last_pid_check = now
        try:
            completed = self._adb.run(
                "shell", "pidof", target,
                serial=self._serial, timeout=5,
            )
            pid_ok = completed.returncode == 0 and completed.stdout.split()
        except (OSError, subprocess.SubprocessError):
            pid_ok = False
        if pid_ok:
            self._pid_notified = False
            return
        if not self._pid_notified:
            self._pid_notified = True
            self.emit_diagnostic(
                "app.process.not_found",
                "warn",
                f"目标进程 {target} 不存在，进程级指标出 null",
            )
        with self._enabled_lock:
            enabled = list(self.session.metrics)
        for metric_id in enabled:
            if metric_id in PROCESS_METRICS:
                self.emit_null_sample(metric_id)


def _default_command_runner(
    command: list[str], timeout: float | None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        creationflags=ADB_CREATION_FLAGS,
    )


def _retarget_sampler(sampler: Any, process_name: str | None) -> Any:
    """R20：processName 提供时把 cpu/mem 维度采样器的目标改道为该进程名。

    - cpu 维度（含 cpu_adv 内嵌进程差分）：ProcessTargetCpuSampler（pidof 按进程
      名解析，复用 CpuSampler._resolve_pid 路径）；
    - cpu_thread 线程通道：内嵌 pidof 探测器换装 ProcessTargetCpuSampler（cpu-3
      修复，2026-11-04 体检：此前线程通道不改道——processName 会话中 proc.pct 测
      子进程、thread.pct 仍测主进程，同会话口径静默分裂）；
    - mem 维度：dumpsys meminfo 按进程名查询（ProcessTargetMemSampler 组合改道）；
    - net（uid 级，同包各进程共享）/power/thermal 不改道。
    """
    if not process_name:
        return sampler
    if isinstance(sampler, CpuFreqNormalizedSampler):
        sampler.target_name = process_name
        # start 前重建内嵌进程差分采样器（基线尚未建立，重建安全）
        sampler._proc_sampler = ProcessTargetCpuSampler(
            interval_ms=sampler.interval_ms, target_name=process_name
        )
        return sampler
    if isinstance(sampler, CpuThreadSampler):
        # 线程通道同径改道：pid 解析与 CLK_TCK 探测都走内嵌探测器（CLK_TCK 不
        # 依赖 package_name，行为不变）；start 前替换，基线尚未建立
        sampler._pid_probe = ProcessTargetCpuSampler(
            interval_ms=sampler.interval_ms, target_name=process_name
        )
        return sampler
    if isinstance(sampler, CpuSampler):
        return ProcessTargetCpuSampler(
            interval_ms=sampler.interval_ms,
            include_per_core=sampler.include_per_core,
            target_name=process_name,
        )
    from perfeval.collectors.android.samplers.mem_sampler import MemSampler

    if isinstance(sampler, MemSampler):
        from .cpu_advanced import ProcessTargetMemSampler

        # dumpsys meminfo 的目标由命令参数承载，组合改道后 mem_detail 不受影响
        #（mem_detail 为独立 sampler 名，不经此分支）
        return ProcessTargetMemSampler(sampler, target_name=process_name)
    return sampler


class _SyntheticGenerator:
    """单指标确定性取值：种子 = sha256(sessionId|metricId)，波形按采样序号推进。

    M6（契约 §21.2/§21.3）新增建模：
    - cpu.freq.mhz：8 核静态基线（大核 1800 / 小核 1400 MHz）+ 确定性扰动，
      每 23 个采样点进入 5 点的降频窗（比值 0.6，演示降频观察）；
    - cpu.proc.normalized.pct：未规范化进程占用 × 频率比值（公式与注册表
      description 同式），tags 携带 basis/freqRatio；
    - cpu.proc.thread.pct：固定线程名共享比例 × 进程占用波形（每 tick 多条记录，
      tags.thread 为线程名，契约 §21.2 tags 口径）；
    - memory.detail.*：PSS 明细段四项的确定性分量（native/dalvik/gfx/gl），
      synthetic 恒有明细（无机型差异演示）。
    """

    _THREAD_SHARES: tuple[tuple[str, float], ...] = (
        ("RenderThread", 0.38),
        ("GameThread", 0.27),
        ("JIT", 0.14),
        ("HeapTaskDaemon", 0.09),
        ("RenderWorker", 0.07),
        ("GC", 0.05),
    )
    # 降频演示窗（i % 周期 < 窗长 → 全核 × 比值）
    _DOWNCLOCK_PERIOD = 23
    _DOWNCLOCK_LEN = 5
    _DOWNCLOCK_RATIO = 0.6

    def __init__(self, session_id: str, metric_id: str) -> None:
        seed = int(hashlib.sha256(f"{session_id}|{metric_id}".encode()).hexdigest()[:8], 16)
        self._metric_id = metric_id
        self._rng = random.Random(seed)
        self._i = 0
        self._detail_cumulative: dict[str, float] = {
            "native_mb": 96.0,
            "dalvik_mb": 58.0,
            "gfx_mb": 42.0,
            "gl_mb": 12.0,
        }

    def next_values(self) -> list[tuple[float, dict[str, Any]]]:
        """本 tick 的全部点（一般 1 条；线程级指标为每线程 1 条）。"""
        i = self._i
        self._i += 1
        rng = self._rng
        wave = math.sin(i / 7.0)
        metric = self._metric_id
        if metric == "cpu.total.pct":
            return [(round(_clamp(22 + 12 * wave + rng.uniform(-2, 2), 0, 100), 1), {})]
        if metric == "cpu.proc.pct":
            return [(round(_clamp(12 + 7 * math.sin(i / 5 + 1.3) + rng.uniform(-1.5, 1.5), 0, 100), 1), {})]
        if metric == "memory.pss.peak_mb":
            return [(round(340 + 35 * abs(wave) + rng.uniform(0, 4), 1), {"semantic": "peak"})]
        if metric == "net.rx.kbps":
            return [(round(400 + 350 * abs(math.sin(i / 6)) + rng.uniform(0, 60), 1), {})]
        if metric == "net.tx.kbps":
            return [(round(120 + 90 * abs(math.sin(i / 4 + 0.5)) + rng.uniform(0, 30), 1), {})]
        if metric == "power.current.ma":
            return [(round(abs(280 + 70 * wave + rng.uniform(-10, 10)), 1), {})]
        if metric == "power.level.pct":
            # 掉电速率口径（%/min，power-3 修复 2026-11-06：real 降级通道出的是
            # 电量差分速率而非电量百分比，synthetic 同口径对齐——注册表 unit/%/min、
            # lower_better；电量回升（充电）按 abs 口径同为正速率）
            return [(
                round(_clamp(0.6 + 0.4 * abs(wave) + rng.uniform(0, 0.3), 0, 10), 4),
                {},
            )]
        if metric == "power.energy.mah":
            # 逐窗增量口径（power-2 修复 2026-11-06：real 采样器每条记录 = 单窗口
            # 能耗增量，summary 聚合取 sum（积分型指标豁免中位数）；synthetic 同
            # 口径产出增量而非会话累计，跨通道语义一致）
            return [(round(0.02 + rng.uniform(0, 0.01), 4), {})]
        if metric == "thermal.max.c":
            return [(round(33 + 3.5 * math.sin(i / 11) + rng.uniform(-0.5, 0.5), 1), {})]
        if metric == "frame.fps.mean":
            if i % 17 == 0:
                return [(round(57 + rng.uniform(0, 4), 1), {})]
            return [(round(_clamp(116 + rng.uniform(-2, 2), 0, 120), 1), {})]
        if metric == "cpu.freq.mhz":
            down = i % self._DOWNCLOCK_PERIOD < self._DOWNCLOCK_LEN
            ratio = self._DOWNCLOCK_RATIO if down else 1.0
            jitter = rng.uniform(-40, 40)
            return [
                (round((1800.0 if n % 2 else 1400.0) * ratio + jitter, 1), {"core": f"cpu{n}"})
                for n in range(8)
            ]
        if metric == "cpu.proc.normalized.pct":
            proc = _clamp(14 + 6 * math.sin(i / 5 + 1.3) + rng.uniform(-1.0, 1.0), 0, 100)
            down = i % self._DOWNCLOCK_PERIOD < self._DOWNCLOCK_LEN
            # 同一降频演示窗的频率比值（全部核同比例降频 → Σ当前/Σ最大 = 降频
            # 系数；规范化公式与注册表 description 同式）
            ratio = self._DOWNCLOCK_RATIO if down else 1.0
            return [(
                round(proc * ratio, 3),
                {"basis": "normalized", "freqRatio": round(ratio, 4)},
            )]
        if metric == "cpu.proc.thread.pct":
            proc = _clamp(14 + 6 * math.sin(i / 5 + 1.3) + rng.uniform(-1.0, 1.0), 0, 100)
            return [
                (
                    round(proc * share + rng.uniform(-0.4, 0.4), 3),
                    {"thread": name, "tid": 1000 + offset},
                )
                for offset, (name, share) in enumerate(self._THREAD_SHARES)
            ]
        if metric in ("memory.detail.native_mb", "memory.detail.dalvik_mb",
                      "memory.detail.gfx_mb", "memory.detail.gl_mb"):
            key = metric.removeprefix("memory.detail.")
            self._detail_cumulative[key] = min(
                512.0,
                self._detail_cumulative[key] + 0.3 * abs(wave) + rng.uniform(0, 0.4),
            )
            return [(round(self._detail_cumulative[key], 3), {})]
        # 未建模指标退化为有界随机（仍确定性）
        return [(round(rng.uniform(0, 100), 1), {})]

    def next_value(self) -> tuple[float, dict[str, Any]]:
        """单点形态（向后兼容既有调用）。"""
        return self.next_values()[0]


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _load_real_ios_collector() -> type | None:
    """S1 真机 iOS 采集器类面（装配流 S4 分流接缝）：懒加载 ``realios_sampler
    .RealIOSCollector``；模块未落地（ImportError，三流并行期）→ None。

    S1 落点 ``app/services/realios_sampler.py``（§26.4 窗口批采样，与
    simulator_sampler 命名对齐），类签名 ``(session, bus, *, ping_s=...)`` 兼容
    BaseCollector 生命周期（start/stop/set_enabled）。模块落地后本分支零改动
    激活；未落地期间真机 iOS 会话创建按契约错误码 not_implemented 如实拒绝
    （不静默、不 AttributeError→500）。测试可 monkeypatch 本函数注入 fixture 类。
    """
    try:
        from .realios_sampler import RealIOSCollector  # type: ignore[attr-defined]
    except ImportError:
        return None
    return RealIOSCollector


def create_collector(
    session: Any,
    provider_mode: str,
    bus: SseBus,
    ping_s: float = 60.0,
    serial: str | None = None,
    fps_window_resolved: str | None = None,
) -> BaseCollector:
    """按 provider 模式 + 设备源平台构造采集器（会话创建后立即 start）。

    M-iOS（契约 §23.2）：simulator 模式 → SimulatorCollector（宿主口径采样，
    simulator_sampler 模块）。serial：WiFi 行会话经 provider.adb_serial 映射后的
    adb serial（USB 行为 None，RealCollector 回退 device_id；iOS 设备经
    CompositeProvider.adb_serial 短路为 None——udid 不冒充 serial）。
    fps_window_resolved（契约 §21.5，S2 接线 2026-11-10）：resolve_fps_window 的
    解析结果送达 FpsSampler（real 通道三通道窗口过滤）；None 时 RealCollector
    回退会话已解析属性（router 创建路径不传参）。

    装配流 S4（契约 §26 会话链路）：分流按设备源通道与设备平台——channel
    （session.device_channel，缺省回退 provider_mode）=="simulator" →
    SimulatorCollector（聚合面内模拟器设备与单源 simulator 语义一致）；real 模式
    按 ``session.device_platform``（SessionManager.create 从设备行落定的落盘平台）
    分流——android → RealCollector（原路径逐字段不变）；ios → RealIOSCollector
    （S1 类面，未落地 → 501 not_implemented 契约错误码）。聚合面
    （provider_mode 恒 "real"）下真机 iOS 会话不再误入 adb 采样链。
    """
    channel = getattr(session, "device_channel", None) or provider_mode
    if channel == "simulator":
        from .simulator_sampler import SimulatorCollector

        return SimulatorCollector(session, bus, ping_s=ping_s)
    if provider_mode == "real":
        if (getattr(session, "device_platform", "android") or "android") == "ios":
            collector_cls = _load_real_ios_collector()
            if collector_cls is None:
                raise ApiError(
                    "not_implemented",
                    "真机 iOS 采集链未接线（xctrace 执行面 realios_sampler 未落地）",
                    details={
                        "diagnostic": {
                            "code": "collector.ios_real.unavailable",
                            "severity": "info",
                            "message": (
                                "真机 iOS 采集（xctrace Activity Monitor 双序列）"
                                "本期未接线，会话创建被拒绝（不静默无数据）"
                            ),
                        },
                    },
                )
            return collector_cls(session, bus, ping_s=ping_s)
        return RealCollector(
            session, bus, ping_s=ping_s, serial=serial,
            fps_window_resolved=fps_window_resolved,
        )
    return SyntheticCollector(session, bus, ping_s=ping_s)
