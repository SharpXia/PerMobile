from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .base import DeviceContext, ProbeResult, SampleRecord

# 指标 ID 依 METRIC_SOURCES §4（counter，evidenceLevel L1）；
# 每核序列复用整机 ID 并以 tags.core 区分（METRIC_SOURCES §2「同整机，输出 tags.core」）
PROC_METRIC_ID = "cpu.proc.pct"
TOTAL_METRIC_ID = "cpu.total.pct"

# CPU 采样默认间隔（SAMPLING_FRAMEWORK §6 scenario 配置 cpu.intervalMs=1000）
DEFAULT_INTERVAL_MS = 1000

# CLK_TCK 探测失败回退值（METRIC_SOURCES §2）
DEFAULT_CLK_TCK = 100

# pid 取不到时单次跳过并计 skipped，连续 3 次记 diagnostic（METRIC_SOURCES §2 降级口径）
PID_FAILURE_DIAGNOSTIC_THRESHOLD = 3

# /proc/stat cpu 行："cpu" 聚合行与 "cpuN" 每核行；/proc/<pid>/stat 行以 pid 数字开头，二者不会混淆
_CPU_LINE_RE = re.compile(r"^cpu(\d*)\s+(.+)$")

_READ_TIMEOUT_S = 10


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class CpuTimes:
    """一行 /proc/stat CPU jiffies（列序依 proc(5)：user nice system idle iowait irq softirq steal guest guest_nice）。"""

    fields: tuple[int, ...]

    @property
    def total(self) -> int:
        """busy 口径总 jiffies：user..steal（前 8 列）之和。

        guest/guest_nice（第 9/10 列）不计入——proc(5) 口径下内核将 guest 时间并入
        user/nice 双写（fs/proc/stat.c show_stat 的 user+guest 求和展示），对全列求和
        会把 guest 重复计一次、虚增 Δtotal，使 busy% 被高估
        （cpu-guest-double-count 修复，2026-11-10）；短行（不足 8 列）按原样求和。
        """
        if len(self.fields) >= 8:
            return sum(self.fields[:8])
        return sum(self.fields)

    @property
    def idle_all(self) -> int:
        """idle + iowait（经典口径）；busy% = 1 - Δidle_all/Δtotal。"""
        idle = self.fields[3] if len(self.fields) > 3 else 0
        iowait = self.fields[4] if len(self.fields) > 4 else 0
        return idle + iowait


@dataclass(frozen=True)
class ProcTimes:
    """一行 /proc/<pid>/stat 的 utime/stime（第 14/15 列，用户态 + 内核态 jiffies）。"""

    utime: int
    stime: int

    @property
    def busy(self) -> int:
        return self.utime + self.stime


@dataclass(frozen=True)
class CpuSnapshot:
    """单次批量读取的解析结果（ts_ms 为相对调度起点的毫秒数）。

    pid 为本次 pidof 解析到的进程身份（无进程维度时 None），用于跨快照检测进程重启——
    新进程 jiffies 从零计数，与旧基线不可比。
    """

    ts_ms: int
    pid: int | None
    total: CpuTimes | None
    cores: Mapping[int, CpuTimes]
    proc: ProcTimes | None
    core_count: int


def _parse_cpu_times(text: str) -> CpuTimes | None:
    try:
        fields = tuple(int(part) for part in text.split())
    except ValueError:
        return None
    if not fields:
        return None
    return CpuTimes(fields)


def _parse_pid_stat_line(line: str, pid: int) -> ProcTimes | None:
    if not line.startswith(f"{pid} "):
        return None
    close = line.rfind(")")
    if close == -1:
        return None
    fields = line[close + 1 :].split()
    # ')' 之后首字段是 state（第 3 列），utime/stime 为第 14/15 列 → 偏移 11/12；
    # comm 内可含空格，rfind(')') 取真正的分隔符
    if len(fields) < 13:
        return None
    try:
        return ProcTimes(utime=int(fields[11]), stime=int(fields[12]))
    except ValueError:
        return None


def parse_stat_text(
    text: str,
    *,
    pid: int | None = None,
) -> tuple[CpuTimes | None, dict[int, CpuTimes], ProcTimes | None]:
    """解析单次批量 `cat /proc/stat /proc/<pid>/stat` 的拼接输出。

    设备侧 cat 对部分文件失败仍会输出其余内容，因此按行内容分流、不依赖 returncode：
    "cpu"/"cpuN" 行属于 /proc/stat，以 pid 开头的行属于 /proc/<pid>/stat（comm 已截断
    到 15 字符，rfind(')') 容忍其含特殊字符）。
    """
    total: CpuTimes | None = None
    cores: dict[int, CpuTimes] = {}
    proc: ProcTimes | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        cpu_match = _CPU_LINE_RE.match(line)
        if cpu_match is not None:
            times = _parse_cpu_times(cpu_match.group(2))
            if times is None:
                continue
            index = cpu_match.group(1)
            if index:
                cores[int(index)] = times
            else:
                total = times
            continue
        if proc is None and pid is not None:
            proc = _parse_pid_stat_line(line, pid)
    return total, cores, proc


class CpuSampler:
    """/proc/stat 与 /proc/<pid>/stat 差分采样（CM-TASK-014，METRIC_SOURCES §2）。

    - 整机/每核：`1 - Δ(idle+iowait)/Δtotal × 100`（Δtotal = Σ 全部 jiffies 字段之差）
    - 进程：`(Δ(utime+stime)/CLK_TCK) / Δt_s / 核数 × 100`，归一化到整机容量（口径声明见
      requirements §4）；核数取当前快照的 cpuN 行数（热插拔一致），缺失时回退基线快照
    - CLK_TCK 经 `getconf CLK_TCK` 探测缓存（start 时一次），失败回退 100
    - 单次 `adb shell cat /proc/stat /proc/<pid>/stat` 批量读取；pid 由 `pidof <pkg>` 解析
    - pid 取不到 / 进程 stat 缺失：跳过该次进程采样并计 skipped，连续 3 次记 diagnostic，
      整机采样不受影响；整机读取失败则保留旧基线（下次成功样本跨更大窗口差分，比例口径
      不变）并计 skipped_total_samples，连续 3 次记 diagnostic（cpu-4 修复，2026-11-04
      体检：此前整机读失败整窗静默、无计数无诊断，极端时整段空数据无解释）
    - pidof 解析到不同 pid（进程重启）：视同 re-baseline——重建基线、该采样点不产数，
      消除新进程 jiffies 与旧基线不可比导致的负差分
    - 计算出的 CPU% 超出 [0, 100]（计数器异常/窗口错位等极端场景）时丢弃该点并记
      diagnostic（out_of_range），不做掩盖性 clamp
    """

    name = "cpu"
    metric_ids = (PROC_METRIC_ID, TOTAL_METRIC_ID)

    def __init__(
        self,
        *,
        interval_ms: int = DEFAULT_INTERVAL_MS,
        include_per_core: bool = False,
        utc_now: Callable[[], str] | None = None,
    ) -> None:
        self.interval_ms = max(1, int(interval_ms))
        self.include_per_core = bool(include_per_core)
        self._utc_now = utc_now or _utc_now_iso
        self._clk_tck: int | None = None
        self._baseline: CpuSnapshot | None = None
        self._pid_fail_streak = 0
        self.skipped_proc_samples = 0
        self._total_fail_streak = 0
        self.skipped_total_samples = 0
        self._diagnostics: list[str] = []

    @property
    def clk_tck(self) -> int:
        return self._clk_tck if self._clk_tck and self._clk_tck > 0 else DEFAULT_CLK_TCK

    @property
    def diagnostics(self) -> list[str]:
        return list(self._diagnostics)

    def drain_diagnostics(self) -> list[str]:
        """取走并清空采样器内部累计的 diagnostic（与调度器 diagnostic 命名风格一致）。"""
        drained = list(self._diagnostics)
        self._diagnostics.clear()
        return drained

    # --- capability 探测（只读） ---

    def probe(self, ctx: DeviceContext) -> ProbeResult:
        # /proc/stat 可读即 available；进程维度按次容错，不参与 probe 判定
        try:
            total, _, _ = self._read_stat(ctx, pid=None)
        except Exception:  # noqa: BLE001 - 探测失败按能力缺失处理，由调度器剔除
            return ProbeResult.unavailable("/proc/stat unavailable")
        if total is None:
            return ProbeResult.unavailable("/proc/stat unavailable")
        return ProbeResult.ok()

    # --- 生命周期 ---

    def start(self, ctx: DeviceContext) -> None:
        self._clk_tck = self._probe_clk_tck(ctx)
        pid = self._resolve_pid(ctx)
        self._baseline = self._read_snapshot(ctx, pid, ctx.elapsed_ms())

    def sample(self, ctx: DeviceContext) -> list[SampleRecord]:
        ts_ms = ctx.elapsed_ms()
        pid = self._resolve_pid(ctx)
        snapshot = self._read_snapshot(ctx, pid, ts_ms)
        if snapshot is None:
            # 整机读取失败：保留旧基线，下次成功样本跨越更大窗口差分，比例口径仍正确；
            # cpu-4 修复：计入 skipped 并按连击记 diagnostic（不再整窗静默）
            self._track_total_miss()
            return []
        self._total_fail_streak = 0
        baseline = self._baseline
        pid_changed = (
            baseline is not None
            and baseline.pid is not None
            and snapshot.pid is not None
            and baseline.pid != snapshot.pid
        )
        self._baseline = snapshot
        if pid_changed:
            # 进程重启（pid 变化）：新进程 jiffies 从零计数、可能小于旧基线 → 视同 re-baseline，
            # 重建基线、该采样点不产出（整机/进程均跳过一个窗口）
            return []
        if baseline is None:
            return []
        delta_t_s = (ts_ms - baseline.ts_ms) / 1000.0
        if delta_t_s <= 0:
            return []
        # tags.samplingWindowMs 取实测差分窗口（net-2 修复：值为窗口差分派生，
        # 名义间隔标签会误导按窗口二次归一的消费方）
        window_ms = int(delta_t_s * 1000)

        records: list[SampleRecord] = []
        if ctx.package_name:
            proc_pct = self._proc_pct(baseline, snapshot, delta_t_s)
            if proc_pct is not None:
                records.append(self._record(ts_ms, PROC_METRIC_ID, proc_pct, window_ms=window_ms))
        records.extend(self._total_records(ts_ms, baseline, snapshot, window_ms=window_ms))
        return records

    def stop(self) -> list[SampleRecord]:
        # 协议 stop() 不携带 ctx，无法补一次设备读数；窗口缺口由"collect 前及时 stop"控制
        return []

    # --- 差分计算 ---

    def _proc_pct(self, baseline: CpuSnapshot, snapshot: CpuSnapshot, delta_t_s: float) -> float | None:
        if snapshot.proc is None:
            self._track_proc_miss()
            return None
        self._pid_fail_streak = 0  # 进程读数恢复，失败连击清零
        if baseline.proc is None:
            return None  # 上一帧无进程读数（pid 刚恢复）：本帧仅重建基线，不产出
        core_count = snapshot.core_count or baseline.core_count
        busy_delta = snapshot.proc.busy - baseline.proc.busy
        clk_tck = self.clk_tck
        if busy_delta < 0 or core_count <= 0 or delta_t_s <= 0:
            return None
        return (busy_delta / clk_tck) / delta_t_s / core_count * 100.0

    def _total_records(
        self,
        ts_ms: int,
        baseline: CpuSnapshot,
        snapshot: CpuSnapshot,
        *,
        window_ms: int,
    ) -> list[SampleRecord]:
        records: list[SampleRecord] = []
        if baseline.total is not None and snapshot.total is not None:
            pct = self._guard_pct(_busy_pct(baseline.total, snapshot.total), scope="total")
            if pct is not None:
                records.append(self._record(ts_ms, TOTAL_METRIC_ID, pct, window_ms=window_ms))
        if self.include_per_core:
            for index in sorted(snapshot.cores):
                base = baseline.cores.get(index)
                if base is None:
                    continue  # 新上线核无基线：跳过本窗口，下窗口起正常差分（下线核不迭代）
                pct = self._guard_pct(_busy_pct(base, snapshot.cores[index]), scope=f"core{index}")
                if pct is not None:
                    records.append(
                        self._record(ts_ms, TOTAL_METRIC_ID, pct, window_ms=window_ms,
                                     tags_extra={"core": index})
                    )
        return records

    def _guard_pct(self, pct: float | None, *, scope: str) -> float | None:
        """值域守卫：极端场景（计数器异常回退/窗口错位）的超界点（<0 或 >100）丢弃并记
        diagnostic，不做掩盖性 clamp。合法数据（Δidle ≤ Δtotal）数学上恒在值域内，不会误杀。"""
        if pct is None:
            return None
        if 0.0 <= pct <= 100.0:
            return pct
        self._diagnostics.append(
            f"sampler.{self.name}.out_of_range:{scope} dropped {round(pct, 4)} (outside [0, 100])"
        )
        return None

    def _track_proc_miss(self) -> None:
        self.skipped_proc_samples += 1
        self._pid_fail_streak += 1
        if self._pid_fail_streak == PID_FAILURE_DIAGNOSTIC_THRESHOLD:
            self._diagnostics.append(
                f"sampler.{self.name}.proc.unavailable:pid/process stat failed for "
                f"{PID_FAILURE_DIAGNOSTIC_THRESHOLD} consecutive samples"
            )

    def _track_total_miss(self) -> None:
        """整机 /proc/stat 读失败计数 + 连击 diagnostic（cpu-4 修复：与进程缺失
        路径对称，恢复成功即清零连击；skipped 计数保留全程累计）。"""
        self.skipped_total_samples += 1
        self._total_fail_streak += 1
        if self._total_fail_streak == PID_FAILURE_DIAGNOSTIC_THRESHOLD:
            self._diagnostics.append(
                f"sampler.{self.name}.stat.unavailable:/proc/stat read failed for "
                f"{PID_FAILURE_DIAGNOSTIC_THRESHOLD} consecutive samples"
            )

    # --- 设备读取 ---

    def _resolve_pid(self, ctx: DeviceContext) -> int | None:
        if not ctx.package_name:
            return None
        command = ctx.adb.command("shell", "pidof", ctx.package_name, serial=ctx.serial)
        try:
            completed = ctx.command_runner(command, _READ_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - 单次 pidof 失败按进程缺失处理
            return None
        tokens = completed.stdout.split()
        if completed.returncode != 0 or not tokens:
            return None
        pid_text = tokens[0]  # 多 pid（如 isolated 进程）取首个主进程
        return int(pid_text) if pid_text.isdigit() else None

    def _stat_command(self, ctx: DeviceContext, pid: int | None) -> list[str]:
        args = ["cat", "/proc/stat"]
        if pid is not None:
            args.append(f"/proc/{pid}/stat")
        return ctx.adb.command("shell", *args, serial=ctx.serial)

    def _read_stat(
        self,
        ctx: DeviceContext,
        *,
        pid: int | None,
    ) -> tuple[CpuTimes | None, dict[int, CpuTimes], ProcTimes | None]:
        completed = ctx.command_runner(self._stat_command(ctx, pid), _READ_TIMEOUT_S)
        return parse_stat_text(completed.stdout, pid=pid)

    def _read_snapshot(self, ctx: DeviceContext, pid: int | None, ts_ms: int) -> CpuSnapshot | None:
        try:
            total, cores, proc = self._read_stat(ctx, pid=pid)
        except Exception:  # noqa: BLE001 - 单次读取失败按无数据处理，由调度器继续
            return None
        if total is None:
            return None
        return CpuSnapshot(ts_ms=ts_ms, pid=pid, total=total, cores=cores, proc=proc, core_count=len(cores))

    def _probe_clk_tck(self, ctx: DeviceContext) -> int | None:
        command = ctx.adb.command("shell", "getconf", "CLK_TCK", serial=ctx.serial)
        try:
            completed = ctx.command_runner(command, _READ_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - 探测失败回退默认 CLK_TCK
            return None
        text = completed.stdout.strip()
        if completed.returncode != 0 or not text.isdigit():
            return None
        value = int(text)
        return value if value > 0 else None

    # --- 记录构造 ---

    def _record(
        self,
        ts_ms: int,
        metric_id: str,
        value: float,
        *,
        window_ms: int | None = None,
        tags_extra: Mapping[str, Any] | None = None,
    ) -> SampleRecord:
        tags: dict[str, Any] = {
            "unit": "%",
            "collector": f"sampler.{self.name}",
            # 采样窗口供 summary coverage 因子使用；tsUtc 墙钟供录屏/跨指标对齐（设计 §2）。
            # 差分记录传实测窗口（net-2 修复），缺省（如非差分调用方）回退名义间隔
            "samplingWindowMs": window_ms if window_ms and window_ms > 0 else self.interval_ms,
            "tsUtc": self._utc_now(),
        }
        if tags_extra:
            tags.update(tags_extra)
        return SampleRecord(metric_id=metric_id, value=float(value), ts_ms=ts_ms, tags=tags)


def _busy_pct(baseline: CpuTimes, current: CpuTimes) -> float | None:
    """两次快照间的 busy%（整机/每核同口径）：1 - Δ(idle+iowait)/Δtotal × 100。"""
    total_delta = current.total - baseline.total
    idle_delta = current.idle_all - baseline.idle_all
    if total_delta <= 0 or idle_delta < 0:
        return None  # 计数器回绕/无进展时不产出，避免负值与除零
    return (1.0 - idle_delta / total_delta) * 100.0
