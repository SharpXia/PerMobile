from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timezone

from .base import DeviceContext, ProbeResult, SampleRecord

# 指标 ID 固定（CM-001 METRIC_SOURCES §4）：sampler.gpu_thermal -> gpu.busy.pct /
# gpu.freq.mhz / thermal.max.c / thermal.{cpu,gpu,btemp}.c
METRIC_GPU_BUSY_PCT = "gpu.busy.pct"
METRIC_GPU_FREQ_MHZ = "gpu.freq.mhz"
METRIC_THERMAL_MAX_C = "thermal.max.c"
METRIC_THERMAL_CPU_C = "thermal.cpu.c"
METRIC_THERMAL_GPU_C = "thermal.gpu.c"
METRIC_THERMAL_BTEMP_C = "thermal.btemp.c"

# GPU/温度采样默认间隔 1s（CM-TASK-018）
DEFAULT_INTERVAL_MS = 1000

GPUBUSY_PATH = "/sys/class/kgsl/kgsl-3d0/gpubusy"
# kgsl 频率（gpu.freq.mhz，2026-11-10 实测 SM_F9560：clock_mhz -> 231 可读；
# devfreq/cur_freq -> 231000000（Hz）为降源，读数 >=1e6 视为 Hz 按 /1e6 归一 MHz）
GPUCLOCK_PATH = "/sys/class/kgsl/kgsl-3d0/clock_mhz"
GPUDEVFREQ_CURFREQ_PATH = "/sys/class/kgsl/kgsl-3d0/devfreq/cur_freq"
_HZ_TO_MHZ_THRESHOLD = 1_000_000.0
# 单次 adb shell 遍历全部 thermal_zone（CM-001 设计 §6，减少进程启动开销）
THERMAL_ZONE_COMMAND = (
    "for z in /sys/class/thermal/thermal_zone*; do "
    'echo "$z $(cat $z/type 2>/dev/null) $(cat $z/temp 2>/dev/null)"; done'
)

# 温度 zone 选型（METRIC_SOURCES §3）：HAL mType 枚举 0=CPU、1=GPU、2=BATTERY；
# 名字兜底匹配 cpu/gpu/batt（厂商命名如 BAT/SUBBAT/cpu-0-1）。SKIN/USB/CP 等不采集。
_THERMAL_TYPES_CPU_GPU_BATTERY = frozenset({0, 1, 2})
_ZONE_NAME_KEYWORDS = ("cpu", "gpu", "batt")

# 分量序列（thermal-component-split，2026-11-10）：mType -> 分量 metricId；sysfs
# 降级（zone_type=None）按名字关键词归类（关键词顺序敏感：cpu 先于 gpu/batt）。
# NPU（NTemp）不映射：SM_F9560（Android 16）与模拟器 dumpsys thermalservice 实测
# 均无 NPU/GPU 独立 zone（mType 仅 0/2/3/4/5/12），无对应枚举/无值不出点
# （unavailable，不填 0，[R§8.6]）。
THERMAL_COMPONENT_BY_TYPE: dict[int, str] = {
    0: METRIC_THERMAL_CPU_C,
    1: METRIC_THERMAL_GPU_C,
    2: METRIC_THERMAL_BTEMP_C,
}
_COMPONENT_BY_NAME_KEYWORD: tuple[tuple[str, str], ...] = (
    ("cpu", METRIC_THERMAL_CPU_C),
    ("gpu", METRIC_THERMAL_GPU_C),
    ("batt", METRIC_THERMAL_BTEMP_C),
)
# 分量记录的固定输出顺序（cpu -> gpu -> btemp）
_COMPONENT_ORDER = (METRIC_THERMAL_CPU_C, METRIC_THERMAL_GPU_C, METRIC_THERMAL_BTEMP_C)

_TEMPERATURE_ENTRY_RE = re.compile(r"Temperature\{([^}]*)\}")
_TEMPERATURE_KV_RE = re.compile(r"(mValue|mType|mName)=([^,}]*)")
# sysfs fallback 行（THERMAL_ZONE_COMMAND 输出）：<path> <type> <temp>
_ZONE_LINE_PARTS = 3
# milli-Celsius 判定（METRIC_SOURCES：/1000 按设备判定）：>=1000 视为毫度
_MILLICELSIUS_THRESHOLD = 1000.0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _is_selected_zone(name: str, zone_type: int | None) -> bool:
    if zone_type in _THERMAL_TYPES_CPU_GPU_BATTERY:
        return True
    lowered = name.lower()
    return any(keyword in lowered for keyword in _ZONE_NAME_KEYWORDS)


def thermal_component_id(name: str, zone_type: int | None) -> str | None:
    """zone -> 分量 metricId（thermal.cpu.c/gpu.c/btemp.c）；不属于任一分量 → None。

    mType 枚举优先（0/1/2），sysfs 降级（zone_type=None）按名字关键词归类，
    关键词判定与 _is_selected_zone 同源（cpu 先于 gpu/batt，避免歧义名误归）。
    """
    if zone_type in THERMAL_COMPONENT_BY_TYPE:
        return THERMAL_COMPONENT_BY_TYPE[zone_type]
    lowered = name.lower()
    for keyword, metric_id in _COMPONENT_BY_NAME_KEYWORD:
        if keyword in lowered:
            return metric_id
    return None


def parse_gpu_clock_mhz(stdout: str) -> float | None:
    """kgsl 频率节点 stdout -> MHz；读数 >=1e6 视为 Hz 按 /1e6 归一（Hz→MHz）。

    2026-11-10 SM_F9560 实测：``cat clock_mhz`` -> ``231``（MHz 直读）、
    ``cat devfreq/cur_freq`` -> ``231000000``（Hz 降源，归一 231 MHz）。
    """
    for token in stdout.split():
        try:
            value = float(token)
        except ValueError:
            continue
        if value < 0:
            continue
        if value >= _HZ_TO_MHZ_THRESHOLD:
            return round(value / 1_000_000.0, 3)
        return round(value, 3)
    return None


def parse_gpubusy(stdout: str) -> tuple[int, int] | None:
    """kgsl gpubusy 两列（busy total，自上次读取的累计 tick）-> (busy, total)。"""
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            continue
    return None


def parse_thermalservice(stdout: str) -> dict[str, tuple[int | None, float]]:
    """dumpsys thermalservice -> {zone 名: (mType, °C)}。

    Cached 与 Current from HAL 段会重复出现同名 zone，按出现顺序后者（更新鲜）覆盖。
    TemperatureThreshold{...} 因前缀不同不会被 `Temperature{` 匹配。
    """
    zones: dict[str, tuple[int | None, float]] = {}
    for body in _TEMPERATURE_ENTRY_RE.findall(stdout):
        fields: dict[str, str] = dict(_TEMPERATURE_KV_RE.findall(body))
        name = fields.get("mName")
        raw_value = fields.get("mValue")
        if not name or raw_value is None:
            continue
        try:
            value = float(raw_value)
        except ValueError:
            continue
        zone_type: int | None
        try:
            zone_type = int(fields["mType"])
        except (KeyError, ValueError):
            zone_type = None
        zones[name] = (zone_type, value)
    return zones


def parse_thermal_zone_sysfs(stdout: str) -> dict[str, float]:
    """THERMAL_ZONE_COMMAND 输出 -> {zone 名: °C}；毫度（>=1000）按 /1000 折算。"""
    zones: dict[str, float] = {}
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) != _ZONE_LINE_PARTS:
            continue
        name = parts[-2]
        try:
            raw = float(parts[-1])
        except ValueError:
            continue
        zones[name] = raw / 1000.0 if abs(raw) >= _MILLICELSIUS_THRESHOLD else raw
    return zones


class GpuThermalSampler:
    """GPU busy% + GPU 频率 + 温度多 zone 周期采样（CM-TASK-018，METRIC_SOURCES §3）。

    GPU：kgsl gpubusy 两列差分 busy% = Δbusy/Δtotal×100；设备无 kgsl（非 Qualcomm/
    模拟器）-> GPU 子指标 unavailable，温度可用则整体 available（子项降级）。
    GPU 频率（gpu.freq.mhz）：kgsl clock_mhz 主源（MHz 直读）、devfreq/cur_freq 降源
    （Hz→MHz 归一），瞬时读数无基线；不可读时不出点并记 diagnostic。
    温度：主源 dumpsys thermalservice（结构化含 zone 名与 mType），
    降级 /sys/class/thermal/thermal_zone*（/1000 按设备判定）；
    每个 CPU/GPU/battery 相关 zone 一条序列（tags.zone），summary 聚合出 thermal.max.c；
    分量序列 thermal.cpu.c/thermal.gpu.c/thermal.btemp.c 各取该类 zone 的 max
    （tags.zone 为贡献 zone；mType 0/1/2 枚举优先，sysfs 降级按名字归类），
    thermal.max.c 原 per-zone 口径不变（向后兼容）。
    单 sampler 内子指标异常隔离：某子源读失败记 diagnostic，其余继续。
    """

    name = "gpu_thermal"
    metric_ids = (
        METRIC_GPU_BUSY_PCT,
        METRIC_GPU_FREQ_MHZ,
        METRIC_THERMAL_MAX_C,
        METRIC_THERMAL_CPU_C,
        METRIC_THERMAL_GPU_C,
        METRIC_THERMAL_BTEMP_C,
    )

    def __init__(
        self,
        *,
        interval_ms: int = DEFAULT_INTERVAL_MS,
        utc_now: Callable[[], str] | None = None,
    ) -> None:
        self.interval_ms = max(1, int(interval_ms))
        self._utc_now = utc_now or _utc_now_iso
        self._gpu_enabled = True
        self._freq_enabled = True
        # GPU 差分基线：(busy, total, ts_ms)
        self._gpu_baseline: tuple[int, int, int] | None = None
        self._diagnostics: list[str] = []

    def drain_diagnostics(self) -> list[str]:
        drained = list(self._diagnostics)
        self._diagnostics.clear()
        return drained

    # --- 设备读数 ---

    def _run(self, ctx: DeviceContext, command: list[str], timeout: float) -> str | None:
        try:
            completed = ctx.command_runner(command, timeout)
        except Exception:  # noqa: BLE001 - 单次读取失败按无数据处理，调度器继续
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout

    def _read_gpubusy(self, ctx: DeviceContext) -> tuple[int, int] | None:
        command = ctx.adb.command("shell", "cat", GPUBUSY_PATH, serial=ctx.serial)
        stdout = self._run(ctx, command, 10)
        if stdout is None:
            return None
        return parse_gpubusy(stdout)

    def _read_gpu_clock(self, ctx: DeviceContext) -> float | None:
        """kgsl 频率：clock_mhz 主源（MHz），降源 devfreq/cur_freq（Hz→MHz 归一）。"""
        command = ctx.adb.command("shell", "cat", GPUCLOCK_PATH, serial=ctx.serial)
        stdout = self._run(ctx, command, 10)
        if stdout is not None:
            clock = parse_gpu_clock_mhz(stdout)
            if clock is not None:
                return clock
        fallback = ctx.adb.command(
            "shell", "cat", GPUDEVFREQ_CURFREQ_PATH, serial=ctx.serial
        )
        stdout = self._run(ctx, fallback, 10)
        if stdout is None:
            return None
        return parse_gpu_clock_mhz(stdout)

    def _read_zones(self, ctx: DeviceContext) -> dict[str, tuple[int | None, float]] | None:
        """主源 dumpsys thermalservice，降级 sysfs thermal_zone。

        返回选型后的 {zone 名: (mType, °C)}；sysfs 降源 mType=None（分量按名字归类）。
        """
        command = ctx.adb.command("shell", "dumpsys", "thermalservice", serial=ctx.serial)
        stdout = self._run(ctx, command, 30)
        if stdout is not None:
            zones = {
                name: (zone_type, value)
                for name, (zone_type, value) in parse_thermalservice(stdout).items()
                if _is_selected_zone(name, zone_type)
            }
            if zones:
                return zones
        fallback = ctx.adb.command("shell", THERMAL_ZONE_COMMAND, serial=ctx.serial)
        stdout = self._run(ctx, fallback, 10)
        if stdout is None:
            return None
        zones = {
            name: (None, value)
            for name, value in parse_thermal_zone_sysfs(stdout).items()
            if _is_selected_zone(name, None)
        }
        return zones or None

    # --- 协议实现 ---

    def probe(self, ctx: DeviceContext) -> ProbeResult:
        gpu_ok = self._read_gpubusy(ctx) is not None
        self._gpu_enabled = gpu_ok  # 无 kgsl 的设备关停 GPU 子源，避免逐轮 diagnostic 刷屏
        self._freq_enabled = self._read_gpu_clock(ctx) is not None
        thermal_ok = self._read_zones(ctx) is not None
        gpu_reason = f"gpu busy unavailable: {GPUBUSY_PATH} unreadable"
        thermal_reason = (
            "thermal unavailable: no readings from dumpsys thermalservice "
            "or /sys/class/thermal/thermal_zone*"
        )
        if gpu_ok and thermal_ok:
            return ProbeResult.ok()
        if gpu_ok:
            return ProbeResult(available=True, reason=f"degraded: {thermal_reason}")
        if thermal_ok:
            return ProbeResult(available=True, reason=f"degraded: {gpu_reason}")
        return ProbeResult.unavailable(f"{gpu_reason}; {thermal_reason}")

    def start(self, ctx: DeviceContext) -> None:
        self._gpu_baseline = None
        self._diagnostics.clear()
        if self._gpu_enabled:
            reading = self._read_gpubusy(ctx)
            if reading is None:
                self._diagnostics.append(
                    "sampler.gpu_thermal.gpu.read_failed:gpu baseline read failed; "
                    "first sample() re-baselines"
                )
                return
            busy, total = reading
            self._gpu_baseline = (busy, total, ctx.elapsed_ms())

    def sample(self, ctx: DeviceContext) -> list[SampleRecord]:
        records: list[SampleRecord] = []
        records.extend(self._gpu_records(ctx))
        records.extend(self._gpu_freq_records(ctx))
        records.extend(self._thermal_records(ctx))
        return records

    def _gpu_freq_records(self, ctx: DeviceContext) -> list[SampleRecord]:
        """gpu.freq.mhz：kgsl 瞬时频率（clock_mhz 主源 / devfreq cur_freq 降源）。"""
        if not self._freq_enabled:
            return []
        clock_mhz = self._read_gpu_clock(ctx)
        if clock_mhz is None:
            self._diagnostics.append(
                "sampler.gpu_thermal.gpu_freq.read_failed:kgsl clock read failed; "
                "gpu.freq.mhz skipped this round"
            )
            return []
        return [
            SampleRecord(
                metric_id=METRIC_GPU_FREQ_MHZ,
                value=clock_mhz,
                ts_ms=ctx.elapsed_ms(),
                tags={
                    "unit": "MHz",
                    "collector": f"sampler.{self.name}",
                    "samplingWindowMs": self.interval_ms,
                    "tsUtc": self._utc_now(),
                },
            )
        ]

    def _gpu_records(self, ctx: DeviceContext) -> list[SampleRecord]:
        if not self._gpu_enabled:
            return []
        reading = self._read_gpubusy(ctx)
        if reading is None:
            self._diagnostics.append(
                "sampler.gpu_thermal.gpu.read_failed:gpubusy read failed; "
                "gpu.busy.pct skipped this round"
            )
            return []
        busy, total = reading
        baseline = self._gpu_baseline
        ts_ms = ctx.elapsed_ms()
        self._gpu_baseline = (busy, total, ts_ms)
        if baseline is None:
            return []
        prev_busy, prev_total, prev_ts = baseline
        delta_total = total - prev_total
        delta_busy = busy - prev_busy
        window_ms = ts_ms - prev_ts
        if window_ms <= 0:  # 时钟未前进，跳过本点
            return []
        if delta_busy < 0 or delta_total < 0:
            # 计数器回绕/整体清零重计（任一列倒退）：重建基线，本跳点不产出
            self._diagnostics.append(
                "sampler.gpu_thermal.gpu.counter_reset:gpu busy counter reset "
                "detected; re-baselined"
            )
            return []
        if delta_total == 0:
            # 统计窗口无增量（GPU 门控，kgsl 门控时读数恒为 0 0）：无法差分，
            # 跳过本点并记 diagnostic（R1 修复：不再静默跳点）
            self._diagnostics.append(
                "sampler.gpu_thermal.gpu.no_increment:gpu total counter no "
                "increment; skipped this round"
            )
            return []
        busy_pct = max(0.0, min(100.0, delta_busy / delta_total * 100.0))
        return [
            SampleRecord(
                metric_id=METRIC_GPU_BUSY_PCT,
                value=round(busy_pct, 4),
                ts_ms=ts_ms,
                tags={
                    "unit": "%",
                    "collector": f"sampler.{self.name}",
                    # 实测差分窗口（net-2 修复：kgsl 门控/调度串行会拉长真实窗口，
                    # 名义间隔标签会误导按窗口二次归一的消费方）
                    "samplingWindowMs": window_ms,
                    "tsUtc": self._utc_now(),
                },
            )
        ]

    def _thermal_records(self, ctx: DeviceContext) -> list[SampleRecord]:
        zones = self._read_zones(ctx)
        if zones is None:
            self._diagnostics.append(
                "sampler.gpu_thermal.thermal.read_failed:thermal sources read "
                "failed; thermal.max.c skipped this round"
            )
            return []
        ts_ms = ctx.elapsed_ms()
        tags = {
            "unit": "°C",
            "collector": f"sampler.{self.name}",
            "samplingWindowMs": self.interval_ms,
            "tsUtc": self._utc_now(),
        }
        # 每个 CPU/GPU/battery 相关 zone 一条序列（tags.zone）；summary 聚合输出 thermal.max.c
        records = [
            SampleRecord(
                metric_id=METRIC_THERMAL_MAX_C,
                value=value,
                ts_ms=ts_ms,
                tags={**tags, "zone": name.lower()},
            )
            for name, (_, value) in sorted(zones.items())
        ]
        # 分量序列：每分量取该类 zone 的 max（tags.zone 为贡献 zone）；某分量无
        # zone 时不出点（unavailable，不填 0，[R§8.6]）。thermal.max.c per-zone
        # 原口径不变（向后兼容）。
        for component in _COMPONENT_ORDER:
            members = [
                (name, value)
                for name, (zone_type, value) in zones.items()
                if thermal_component_id(name, zone_type) == component
            ]
            if not members:
                continue
            top_name, top_value = max(members, key=lambda pair: pair[1])
            records.append(
                SampleRecord(
                    metric_id=component,
                    value=top_value,
                    ts_ms=ts_ms,
                    tags={**tags, "zone": top_name.lower()},
                )
            )
        return records

    def stop(self) -> list[SampleRecord]:
        # 与 mem_sampler 同口径：协议 stop() 不携带 ctx，无法补一次设备读数。
        return []
