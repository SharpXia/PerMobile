"""CPU 高阶采样器（M6 R16，契约 §21.2）：cpufreq 每核频率 / 规范化进程占用 /
线程级占用。全部经 DeviceContext 的可注入 command_runner 读设备（perfeval
SamplingScheduler 同款协议），单次 adb 调用批量读、按标记行对位解析。

复用面（不复制粘贴）：
- 差分协议/记录构造对齐 perfeval cpu_sampler.CpuSampler（SampleRecord/Sampler
  协议、samplingWindowMs/tsUtc 标签同款）；
- pid/CLK_TCK 探测直接调用 CpuSampler 实例方法（``_resolve_pid``/
  ``_probe_clk_tck``），不改 perfeval 源；
- 规范化口径的未规范化项复用内嵌 CpuSampler 的 ``cpu.proc.pct`` 差分结果。

**规范化公式**（契约 §21.2，[F§3.6] 官方公开口径，实现与 note 同式）::

    cpu.proc.normalized.pct = cpu.proc.pct（未规范化） × (Σ当前频率 / Σ最大频率)

  Σ 仅计入 cur/max 皆可读的核；Σmax=0 或无核可读时不产出该点（不猜）。

**线程级口径**（对齐 cpu_sampler 进程差分）::

    thread.pct = (Δ(utime+stime)/CLK_TCK) / Δt_s / 核数 × 100   （Σ线程 ≈ 进程占用）

**开销评估（R16 契约要求）**：每 tick 开销 = 1 次 pidof + 1 次批量 shell 读（频率
或线程），即 2 次 adb 往返（USB 实测典型 5–20ms/次，WiFi 略高）；设备侧批量 cat
N 个文件为一次 shell 进程内的顺序小读（每文件 ≲0.1ms，游戏 ~100-300 线程 → 设备
侧 ≲30ms）。相对 cpu.freq.mhz/cpu.proc.thread.pct 的 2000ms 默认采样间隔，窗口
占用 ≈1–2.5%；与 intervalMs=500 的最小合理间隔同跑时需留意（单次 adb 超时/失败
按无数据处理，读失败按连击计入采样器诊断通道如实上报——sampler.cpu_adv.freq.
unavailable / sampler.cpu_thread.task.unavailable / sampler.cpu.stat.unavailable，
2026-11-04 体检修复）。vs 采样间隔结论：默认间隔下可忽略，亚秒级间隔下建议降低
线程/频率采样频率。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from perfeval.collectors.android.samplers.base import (
    DeviceContext,
    ProbeResult,
    SampleRecord,
)
from perfeval.collectors.android.samplers.cpu_sampler import (
    CpuSampler,
    DEFAULT_CLK_TCK,
    PROC_METRIC_ID,
)

# 指标 ID（契约 §21.2 注册表，全链路固定）
FREQ_METRIC_ID = "cpu.freq.mhz"
NORMALIZED_METRIC_ID = "cpu.proc.normalized.pct"
THREAD_METRIC_ID = "cpu.proc.thread.pct"

# cpufreq sysfs 节点（R21 profile.gpuSource 同源的内核接口面）
_CUR_FREQ_FMT = "/sys/devices/system/cpu/cpu{core}/cpufreq/scaling_cur_freq"
_MAX_FREQ_FMT = "/sys/devices/system/cpu/cpu{core}/cpufreq/scaling_max_freq"

# 批量读的行标记（解析按标记对位，容忍单文件缺失——cat 失败只走 stderr）
_FREQ_MARKER = "__PDCUR__"
_TASK_MARKER = "__PDTASK__"
# 线程批量读同批携带的 /proc/stat 段标记（核数自取：cpu-1/6 修复，同
# CpuSampler 快照 cpuN 行数口径）
_STAT_MARKER = "__PDSTAT__"

# /proc/stat 每核行 "cpuN ..."（聚合行 "cpu ..." 不匹配）
_STAT_CORE_RE = re.compile(r"^cpu(\d+)\s+")

# proc stat utime/stime 列偏移（第 14/15 列；')' 后首字段为 state，同
# cpu_sampler._parse_pid_stat_line 的偏移注释——线程 stat 行格式与进程一致）
_UTIME_OFFSET = 11
_STIME_OFFSET = 12

# 读失败连击诊断阈值（cpu-4 修复，2026-11-04 体检：整机/频率/线程读失败不再
# 整窗静默——连击计数 + 达阈值记一条通道 diagnostic，恢复即清零，与进程缺失
# 路径 _track_proc_miss 对称）
_FREQ_FAILURE_DIAGNOSTIC_THRESHOLD = 3
_TASK_FAILURE_DIAGNOSTIC_THRESHOLD = 3


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---- 解析（纯函数，fixture 直测）----


def build_cpufreq_command(adb_command: Callable[..., list[str]], serial: str | None,
                          core_count: int) -> list[str]:
    """单次批量读全部核的 cur/max 频率（标记行对位；缺文件只出 stderr）。"""
    script_parts: list[str] = []
    for core in range(core_count):
        script_parts.append(f"echo {_FREQ_MARKER} cpu{core}")
        script_parts.append(f"cat {_CUR_FREQ_FMT.format(core=core)} 2>/dev/null")
        script_parts.append(f"echo {_FREQ_MARKER} cpu{core} max")
        script_parts.append(f"cat {_MAX_FREQ_FMT.format(core=core)} 2>/dev/null")
    # 实机验证修复（2026-11-03）：原 `sh -c "…"` 经 adb 空格拼接后引号丢失，设备侧
    # `sh -c echo M1 cpu0 && …` 只把首词当脚本 —— 首个 marker echo 变空行、核 0 数据
    # 错位。改为单 argv 元素直传，设备 shell 正确解析 `&&` 与重定向（真机实测通过）。
    return adb_command("shell", " && ".join(script_parts), serial=serial)


def parse_cpufreq_text(text: str) -> dict[str, dict[str, int | None]]:
    """标记行输出 → {core: {"cur_khz": int|None, "max_khz": int|None}}。

    每个标记行后紧跟一条 cat 输出（数字 kHz）或空；解析容错：值行缺失/非数字
    落 None（不猜）。
    """
    result: dict[str, dict[str, int | None]] = {}
    pending_core: str | None = None
    pending_kind = "cur"
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(_FREQ_MARKER):
            tokens = line.split()
            pending_core = tokens[1] if len(tokens) > 1 else None
            pending_kind = "max" if "max" in tokens[2:] else "cur"
            if pending_core is not None:
                # 标记即建项：cat 失败（无值行）落 None，不猜
                result.setdefault(pending_core, {"cur_khz": None, "max_khz": None})
            continue
        if pending_core is None:
            continue
        entry = result.setdefault(pending_core, {"cur_khz": None, "max_khz": None})
        if line.isdigit() and entry.get(f"{pending_kind}_khz") is None:
            entry[f"{pending_kind}_khz"] = int(line)
        pending_core = None
    return result


def parse_stat_core_count(text: str) -> int | None:
    """同批输出中 /proc/stat 的 cpuN 行数（= 内核配置核数，热插拔口径同
    cpu_sampler 快照 core_count=len(cores)）；``__PDSTAT__`` 标记缺失或无 cpuN
    行 → None（不猜，调用方回退构造参数/上一快照）。"""
    count = 0
    seen_marker = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith(_STAT_MARKER):
            seen_marker = True
            continue
        if line.startswith(_TASK_MARKER):
            break  # /proc/stat 段结束，进入线程段
        if seen_marker and _STAT_CORE_RE.match(line):
            count += 1
    return count or None


def count_proc_stat_cores(text: str) -> int | None:
    """裸 `cat /proc/stat` 输出的 cpuN 行数（无标记门控版，probe 读窗展开用）。"""
    count = sum(
        1 for raw_line in text.splitlines() if _STAT_CORE_RE.match(raw_line.strip())
    )
    return count or None


def freq_ratio_mhz(frequencies: dict[str, dict[str, int | None]]) -> float | None:
    """规范化比值 = Σ当前频率 / Σ最大频率（MHz 与 kHz 同比例，直接用 kHz）。

    Σ 仅计入 cur/max 皆可读的核（模块 docstring 声明的交集口径，2026-11-06
    修复：此前 cur/max 各自独立求和，单核单侧缺读会静默污染比值且可能 >1）；
    Σmax=0 或无核可读 → None（不产出该点，不猜）。
    """
    pairs = [
        (entry["cur_khz"], entry["max_khz"])
        for entry in frequencies.values()
        if entry.get("cur_khz") is not None and entry.get("max_khz") is not None
    ]
    cur_sum = sum(cur for cur, _ in pairs)
    max_sum = sum(max_ for _, max_ in pairs)
    if cur_sum <= 0 or max_sum <= 0:
        return None
    return cur_sum / max_sum


@dataclass(frozen=True)
class ThreadSample:
    """单线程一次读数（tid + 线程名 + utime/stime jiffies）。"""

    tid: int
    name: str
    utime: int
    stime: int

    @property
    def busy(self) -> int:
        return self.utime + self.stime


_TASK_STAT_RE = re.compile(r"^__PDTASK__ /proc/\d+/task/(\d+)/stat$")


def build_task_stat_command(adb_command: Callable[..., list[str]], serial: str | None,
                            pid: int) -> list[str]:
    """批量读 /proc/stat（核数自取，cpu-1 同批口径）+ 进程全部线程 stat。

    for 循环 + 标记行，线程退出时该标记下无值行；``__PDSTAT__`` 段的 cpuN 行数
    即核数（``parse_stat_core_count``），与 CpuSampler 快照 ``len(cores)`` 同源
    同批，不再硬编码 8（cpu-1 修复：非 8 核设备线程 CPU% 曾系统性失真）。
    """
    task_glob = f"/proc/{pid}/task/*/stat"
    script = (
        f"echo {_STAT_MARKER}; cat /proc/stat 2>/dev/null; "
        f"for f in {task_glob}; do echo {_TASK_MARKER} $f; "
        'cat "$f" 2>/dev/null; done'
    )
    # 实机验证修复（2026-11-03）：原 `sh -c <for 循环>` 经 adb 拼接后设备侧报
    # "syntax error: unexpected 'do'"，线程 CPU 探测整条不可用（真机复现）；改单
    # argv 元素直传后 for 循环由设备 shell 正确解析（真机实测通过）。
    return adb_command("shell", script, serial=serial)


def parse_task_stat_text(text: str) -> dict[int, ThreadSample]:
    """标记行输出 → {tid: ThreadSample}；stat 行缺 tid 标记的孤儿行忽略。

    stat 行解析同 cpu_sampler._parse_pid_stat_line 的列口径：')' 后首字段为
    state（第 3 列），utime/stime 为第 14/15 列（偏移 11/12）；comm 可含空格，
    rfind(')') 取真正的分隔符。
    """
    result: dict[int, ThreadSample] = {}
    pending_tid: int | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        marker = _TASK_STAT_RE.match(line)
        if marker is not None:
            try:
                pending_tid = int(marker.group(1))
            except ValueError:
                pending_tid = None
            continue
        if pending_tid is None:
            continue
        sample = _parse_thread_stat_line(line, pending_tid)
        if sample is not None and sample.tid not in result:
            result[sample.tid] = sample
        pending_tid = None
    return result


def _parse_thread_stat_line(line: str, tid: int) -> ThreadSample | None:
    open_paren = line.find("(")
    close = line.rfind(")")
    if open_paren == -1 or close == -1 or close < open_paren:
        return None
    name = line[open_paren + 1 : close]
    fields = line[close + 1 :].split()
    if len(fields) < _STIME_OFFSET + 1:
        return None
    try:
        return ThreadSample(
            tid=tid,
            name=name,
            utime=int(fields[_UTIME_OFFSET]),
            stime=int(fields[_STIME_OFFSET]),
        )
    except ValueError:
        return None


# ---- 采样器 ----


class CpuFreqNormalizedSampler:
    """cpu.freq.mhz（每核当前频率）+ cpu.proc.normalized.pct（规范化进程占用）。

    - 频率：每 tick 一次批量 sysfs 读；每核一条记录（tags.core = "cpu0"...，
      契约 §21.2 tags 口径），值 MHz = kHz/1000（保留 3 位小数）；
    - 规范化：内嵌 CpuSampler（构造为进程维度）每 tick 跑一次差分，取其
      ``cpu.proc.pct`` 未规范化值 × 频率比值（公式见模块 docstring）；比值不可得
      （Σmax=0/无核可读）或无进程读数时不产出该点；
    - cpufreq 全部不可读 → probe unavailable（调度器剔除 + diagnostic，既有机制）。
    """

    name = "cpu_adv"
    metric_ids = (FREQ_METRIC_ID, NORMALIZED_METRIC_ID)

    def __init__(
        self,
        *,
        interval_ms: int = 1000,
        core_count: int | None = None,
        target_name: str | None = None,
        utc_now: Callable[[], str] | None = None,
    ) -> None:
        self.interval_ms = max(1, int(interval_ms))
        # 核数缺省按 8；probe 时按 /proc/stat cpuN 行数展开实际读窗（cpu-6 修复：
        # >8 核设备 cpu8+ 曾静默缺 freq 记录；多读不存在的核只多几条空标记，解析
        # 落 None 不产记录）
        self.core_count = max(1, int(core_count or 8))
        self._core_count_explicit = core_count is not None
        # R20 子进程目标：进程差分按 processName 定位（缺省 packageName）
        self.target_name = target_name
        self._utc_now = utc_now or _utc_now_iso
        # 内嵌 CpuSampler：仅用于进程差分与 pid/CLK_TCK 探测（复用，不重写）
        self._proc_sampler: CpuSampler = self._make_proc_sampler()
        self._diagnostics: list[str] = []
        self._freq_fail_streak = 0

    def _make_proc_sampler(self) -> CpuSampler:
        if self.target_name:
            return ProcessTargetCpuSampler(
                interval_ms=self.interval_ms, target_name=self.target_name
            )
        return CpuSampler(interval_ms=self.interval_ms)

    @property
    def clk_tck(self) -> int:
        return self._proc_sampler.clk_tck

    @property
    def diagnostics(self) -> list[str]:
        return list(self._diagnostics)

    def drain_diagnostics(self) -> list[str]:
        drained = list(self._diagnostics)
        self._diagnostics.clear()
        return drained

    def _read_frequencies(self, ctx: DeviceContext):
        command = build_cpufreq_command(ctx.adb.command, ctx.serial, self.core_count)
        try:
            completed = ctx.command_runner(command, 10)
        except Exception:  # noqa: BLE001 - 单次读取失败按无数据处理
            return None
        return parse_cpufreq_text(completed.stdout or "")

    def probe(self, ctx: DeviceContext) -> ProbeResult:
        frequencies = self._read_frequencies(ctx)
        if not frequencies or all(
            entry.get("cur_khz") is None for entry in frequencies.values()
        ):
            return ProbeResult.unavailable("cpufreq scaling_cur_freq unavailable")
        self._detect_core_window(ctx)
        return ProbeResult.ok()

    def _detect_core_window(self, ctx: DeviceContext) -> None:
        """cpu-6 修复：批量 cpufreq 读窗按设备实际核数展开（cpu-1 同根因）。

        /proc/stat 的 cpuN 行 = 内核配置的全部（可上线）核——一次 cat 计数后作为
        读窗，>8 核设备的 cpu8+ 不再静默缺 cpu.freq.mhz 记录；显式传入
        core_count 构造参数时不覆盖（显式声明优先）。读失败保留原窗口（不猜）。
        """
        if self._core_count_explicit:
            return
        command = ctx.adb.command("shell", "cat", "/proc/stat", serial=ctx.serial)
        try:
            completed = ctx.command_runner(command, 10)
        except Exception:  # noqa: BLE001 - 探测失败保留缺省窗口
            return
        count = count_proc_stat_cores(completed.stdout or "") if completed.returncode == 0 else None
        if count:
            self.core_count = count

    def start(self, ctx: DeviceContext) -> None:
        self._proc_sampler.start(ctx)

    def sample(self, ctx: DeviceContext) -> list[SampleRecord]:
        ts_ms = ctx.elapsed_ms()
        records: list[SampleRecord] = []
        frequencies = self._read_frequencies(ctx) or {}
        if not frequencies:
            # cpu-4 修复：频率读失败不再整窗静默——连击计数 + 达阈值记一条
            # 通道 diagnostic（恢复即清零），与进程缺失路径对称
            self._track_freq_miss()
        else:
            self._freq_fail_streak = 0
        for core_name in sorted(frequencies):
            cur_khz = frequencies[core_name].get("cur_khz")
            if cur_khz is None:
                continue  # 单核读数缺失：该核不出点（不填假值）
            records.append(
                SampleRecord(
                    metric_id=FREQ_METRIC_ID,
                    value=round(cur_khz / 1000.0, 3),
                    ts_ms=ts_ms,
                    tags=self._tags("MHz", core=core_name),
                )
            )
        # 规范化 = 未规范化 × (Σ当前频率/Σ最大频率)（公式与 note 同式，见模块头）
        ratio = freq_ratio_mhz(frequencies)
        if ratio is not None:
            for record in self._proc_sampler.sample(ctx):
                if record.metric_id != PROC_METRIC_ID:
                    continue
                normalized = round(float(record.value) * ratio, 3)
                if 0.0 <= normalized <= 100.0:
                    # 采样窗口取内嵌进程差分的实测窗口（net-2 同修：该值是窗口
                    # 差分派生，名义间隔标签会误导按窗口二次归一的消费方）
                    tags = self._tags("%")
                    proc_window = record.tags.get("samplingWindowMs")
                    if (
                        isinstance(proc_window, (int, float))
                        and not isinstance(proc_window, bool)
                        and proc_window > 0
                    ):
                        tags["samplingWindowMs"] = int(proc_window)
                    records.append(
                        SampleRecord(
                            metric_id=NORMALIZED_METRIC_ID,
                            value=normalized,
                            ts_ms=ts_ms,
                            tags={**tags, "basis": "normalized",
                                  "freqRatio": round(ratio, 4)},
                        )
                    )
                else:
                    self._diagnostics.append(
                        f"sampler.{self.name}.out_of_range:normalized dropped "
                        f"{normalized} (outside [0, 100])"
                    )
        return records

    def _track_freq_miss(self) -> None:
        self._freq_fail_streak += 1
        if self._freq_fail_streak == _FREQ_FAILURE_DIAGNOSTIC_THRESHOLD:
            self._diagnostics.append(
                f"sampler.{self.name}.freq.unavailable:cpufreq read failed for "
                f"{_FREQ_FAILURE_DIAGNOSTIC_THRESHOLD} consecutive samples"
            )

    def stop(self) -> list[SampleRecord]:
        return self._proc_sampler.stop()

    def _tags(self, unit: str, **extra: Any) -> dict[str, Any]:
        tags: dict[str, Any] = {
            "unit": unit,
            "collector": f"sampler.{self.name}",
            "samplingWindowMs": self.interval_ms,
            "tsUtc": self._utc_now(),
        }
        tags.update(extra)
        return tags


class CpuThreadSampler:
    """cpu.proc.thread.pct：/proc/<pid>/task 遍历的线程级 CPU 使用率（契约 §21.2）。

    - 每 tick：pidof（复用 CpuSampler._resolve_pid，processName 目标由
      collector_service._retarget_sampler 换装内嵌探测器实现）→ 一次批量 shell
      读 /proc/stat + 全部线程 stat（标记行对位）→ 与基线逐 tid 差分；
    - 口径：(Δ(utime+stime)/CLK_TCK)/Δt_s/核数×100（Σ线程 ≈ 进程未规范化占用，
      同 cpu_sampler 进程口径除以核数）；**核数 = 同批 /proc/stat cpuN 行数**
      （cpu-1 修复，2026-11-04 体检：此前硬编码 8，4 核模拟器上线程 CPU% 系统性
      低约一半），缺读回退构造参数/8；CLK_TCK 复用 CpuSampler._probe_clk_tck；
    - 新线程（无基线）本窗口不产出；消失线程不产出；进程消失不产出；
    - 开销评估见模块 docstring（2 次 adb 往返/tick，默认 2000ms 间隔窗口占用
      ≈1–2.5%）。
    """

    name = "cpu_thread"
    metric_ids = (THREAD_METRIC_ID,)

    def __init__(
        self,
        *,
        interval_ms: int = 2000,
        core_count: int | None = None,
        utc_now: Callable[[], str] | None = None,
    ) -> None:
        self.interval_ms = max(1, int(interval_ms))
        # 核数回退链：同批快照 cpuN 行数（主口径，见类 docstring）→ 构造参数 → 8
        self.core_count = max(1, int(core_count or 8))
        self._utc_now = utc_now or _utc_now_iso
        self._pid_probe = CpuSampler(interval_ms=self.interval_ms)
        self._clk_tck: int | None = None
        # 快照 = (pid, ts_ms, {tid: ThreadSample}, 核数|None)；核数为同批 /proc/stat
        # cpuN 行数（缺标记/读失败 None，差分时回退上一快照/构造参数）
        self._baseline: tuple[int | None, int, dict[int, ThreadSample], int | None] | None = None
        self._baseline_ts: int | None = None
        self._task_fail_streak = 0
        self._diagnostics: list[str] = []

    @property
    def diagnostics(self) -> list[str]:
        return list(self._diagnostics)

    def drain_diagnostics(self) -> list[str]:
        drained = list(self._diagnostics)
        self._diagnostics.clear()
        return drained

    @property
    def clk_tck(self) -> int:
        return self._clk_tck if self._clk_tck and self._clk_tck > 0 else DEFAULT_CLK_TCK

    def probe(self, ctx: DeviceContext) -> ProbeResult:
        pid = self._resolve_pid(ctx)
        if pid is None:
            # 进程未起（probe 先于 launch 的 runner 生命周期）不判死：只探测命令面。
            # 与 mem_sampler.probe 同哲学——探测通道可用性而非目标进程存活。
            command = build_task_stat_command(ctx.adb.command, ctx.serial, 1)
            try:
                completed = ctx.command_runner(command, 10)
            except Exception as exc:  # noqa: BLE001 - 命令面失败按能力缺失剔除
                return ProbeResult.unavailable(f"/proc task read failed: {exc}")
            if completed.returncode != 0 and not (completed.stdout or "").strip():
                return ProbeResult.unavailable(
                    f"/proc task read rc={completed.returncode}"
                )
            return ProbeResult.ok()
        return ProbeResult.ok()

    def start(self, ctx: DeviceContext) -> None:
        self._clk_tck = self._pid_probe._probe_clk_tck(ctx)
        pid = self._resolve_pid(ctx)
        self._baseline = self._read_threads(ctx, pid, ctx.elapsed_ms())
        if self._baseline is not None:
            # start 读即基线：不产首窗口差分（与 cpu_sampler.start 同语义）
            self._baseline_ts = self._baseline[1]

    def sample(self, ctx: DeviceContext) -> list[SampleRecord]:
        ts_ms = ctx.elapsed_ms()
        pid = self._resolve_pid(ctx)
        snapshot = self._read_threads(ctx, pid, ts_ms)
        if snapshot is None:
            # cpu-4 修复：线程批量读失败不再整窗静默——连击计数 + 达阈值记一条
            # 通道 diagnostic（恢复即清零）；进程消失（pid None）不属读失败，
            # 由 RealCollector._check_process 的 null 机制承载
            self._track_task_miss()
            return []
        self._task_fail_streak = 0
        baseline = self._baseline
        self._baseline = snapshot
        if baseline is None:
            return []
        if snapshot[0] != baseline[0]:
            # 进程重启（pid 变化）：新线程 jiffies 从零计数，与旧基线不可比
            # → re-baseline，本窗口不产出（cpu_sampler 同语义）
            return []
        delta_t_s = (ts_ms - baseline[1]) / 1000.0
        if delta_t_s <= 0:
            return []
        # 核数回退链：同批快照 → 上一快照（基线）→ 构造参数（cpu_sampler
        # `snapshot.core_count or baseline.core_count` 同式）
        core_count = snapshot[3] or baseline[3] or self.core_count
        records: list[SampleRecord] = []
        base_threads = baseline[2]
        for tid in sorted(snapshot[2]):
            base = base_threads.get(tid)
            if base is None:
                continue  # 新线程无基线：本窗口不产出
            busy_delta = snapshot[2][tid].busy - base.busy
            if busy_delta < 0:
                continue
            pct = (
                (busy_delta / self.clk_tck) / delta_t_s / core_count * 100.0
            )
            records.append(
                SampleRecord(
                    metric_id=THREAD_METRIC_ID,
                    value=round(pct, 3),
                    ts_ms=ts_ms,
                    tags=self._tags(
                        thread=snapshot[2][tid].name or f"tid-{tid}",
                        tid=tid,
                        samplingWindowMs=int(delta_t_s * 1000),
                    ),
                )
            )
        return records

    def stop(self) -> list[SampleRecord]:
        return []

    def _resolve_pid(self, ctx: DeviceContext) -> int | None:
        # 复用 CpuSampler 的 pidof 解析（进程名/包名同径；processName 会话由
        # _retarget_sampler 换装 ProcessTargetCpuSampler，cpu-3 修复）
        return self._pid_probe._resolve_pid(ctx)

    def _read_threads(
        self, ctx: DeviceContext, pid: int | None, ts_ms: int
    ) -> tuple[int | None, int, dict[int, ThreadSample], int | None] | None:
        """快照 = (pid, ts_ms, {tid: ThreadSample}, 核数|None)；pid 为进程身份
        （re-baseline 判据），核数 = 同批 /proc/stat cpuN 行数（cpu-1 修复）。"""
        if pid is None:
            return (None, ts_ms, {}, None)
        command = build_task_stat_command(ctx.adb.command, ctx.serial, pid)
        try:
            completed = ctx.command_runner(command, 10)
        except Exception:  # noqa: BLE001 - 单次读取失败按无数据处理
            return None
        text = completed.stdout or ""
        return (pid, ts_ms, parse_task_stat_text(text), parse_stat_core_count(text))

    def _track_task_miss(self) -> None:
        self._task_fail_streak += 1
        if self._task_fail_streak == _TASK_FAILURE_DIAGNOSTIC_THRESHOLD:
            self._diagnostics.append(
                f"sampler.{self.name}.task.unavailable:task stat read failed for "
                f"{_TASK_FAILURE_DIAGNOSTIC_THRESHOLD} consecutive samples"
            )

    def _tags(self, **extra: Any) -> dict[str, Any]:
        tags: dict[str, Any] = {
            "unit": "%",
            "collector": f"sampler.{self.name}",
            "samplingWindowMs": self.interval_ms,
            "tsUtc": self._utc_now(),
        }
        tags.update(extra)
        return tags


# ---- 进程目标改道（R20 processName：cpu/mem 维度按 pid 定位子进程）----


class ProcessTargetCpuSampler(CpuSampler):
    """CpuSampler 子类：pidof 目标改道 processName（缺省回落 packageName）。

    pidof 接受完整进程名（如 ``com.example:sub``），perfeval 采样器以
    ctx.package_name 解析 pid——子进程采样经 ``replace(ctx, package_name=target)``
    复用同一解析路径，不重写差分逻辑。
    """

    def __init__(self, *args: Any, target_name: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.target_name = target_name

    def _resolve_pid(self, ctx: DeviceContext) -> int | None:
        if self.target_name:
            from dataclasses import replace

            return super()._resolve_pid(replace(ctx, package_name=self.target_name))
        return super()._resolve_pid(ctx)


class ProcessTargetMemSampler:
    """MemSampler 子类：dumpsys meminfo 目标改道 processName（pid/进程名同径）。

    以组合而非继承实现（MemSampler 的差分状态无进程目标耦合），sample 复用父类
    解析面（parse_meminfo → pss_mb）。
    """

    name = "mem"
    metric_ids = ("memory.pss.peak_mb",)

    def __init__(self, inner: Any, target_name: str | None = None) -> None:
        self._inner = inner
        self.target_name = target_name
        self.interval_ms = inner.interval_ms

    @property
    def diagnostics(self) -> list[str]:
        return self._inner.diagnostics

    def drain_diagnostics(self) -> list[str]:
        return self._inner.drain_diagnostics()

    def probe(self, ctx: DeviceContext) -> ProbeResult:
        return self._with_target(self._inner.probe, ctx)

    def start(self, ctx: DeviceContext) -> None:
        self._with_target(self._inner.start, ctx)

    def sample(self, ctx: DeviceContext) -> list[SampleRecord]:
        return self._with_target(self._inner.sample, ctx)

    def stop(self) -> list[SampleRecord]:
        return self._inner.stop()

    def _with_target(self, method: Callable[[DeviceContext], Any], ctx: DeviceContext):
        if not self.target_name:
            return method(ctx)
        from dataclasses import replace

        return method(replace(ctx, package_name=self.target_name))
