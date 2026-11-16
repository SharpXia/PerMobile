"""dumpsys meminfo 明细段解析（M6 R17，契约 §21.3）。

解析风格对齐 perfeval meminfo.py（[R§2.7]：行标签归一 + kb→mb 换算），但不动
perfeval 解析器（App Summary 四项既有口径不变），明细段为 backend 侧新增消费面。
明细段指 ``dumpsys meminfo <pid|pkg>`` 输出的 PSS 明细表（"** MEMINFO in pid ..."
头之后、"App Summary" 段之前），行形态两种（Android 版本差异）：

- 多列表：``Native Heap    12,345    12,000    0    0    12,500    20480 ...``
  （首数值列 = PSS Total，即本模块取值）
- 单列 kB 表：``Native Heap:  12,345 kB``（perfeval App Summary 同款形态）

四项口径（[F§3.8] 官方 Memory Detail）：

- ``native_mb`` ← Native Heap（官方 NativePss）
- ``dalvik_mb`` ← Dalvik Heap + Dalvik Other（官方 Dalvik = 二者之和；Heap 行
  缺失则整项 None，Other 缺失按 0 参和）
- ``gfx_mb``    ← Gfx dev + EGL mtrack（"graphics" 为兼容别名保留；mem-1 修复
  2026-11-06：AOSP 6.0–16 及 main 的明细段从不打印 "Graphics" 行——图形三行恒为
  Gfx dev/EGL mtrack/GL mtrack，旧求和漏掉 EGL mtrack 系统性低估图形内存。
  官方 App Summary Graphics 口径另含 GL mtrack，本组 gl_mb 已单列，不重复计入，
  避免组内双计）。皆缺 → None，不填假零值
- ``gl_mb``     ← GL mtrack（部分机型无此行 → None）

行标签大小写/空白归一（``" ".join(line.lower().split())``，对齐 perfeval
``" ".join(match.group(1).lower().split())`` 先例）。契约 §21.3（2026-11-16 修订）：
整组可勾判据=明细段**通道存在**（detail_channel_available，native/dalvik 任一
可解析），明细段缺失整组 available=false + not_landed；单项行缺失由采样层逐项
不出点（原「四项全部可用」判据在实测机型上系统性误禁整组——图形池行只在有
GL 上下文的进程出现，SM_F9560 与模拟器 system 进程实测均缺 gfx 行）。

2026-11-10 扩展（gap memory-swap-vss-available，契约 §21.3 delta）：在四项之外
同次解析返回 ``swap_mb`` / ``vss_mb``——

- ``swap_mb`` ← App Summary 段 TOTAL 行的 swap 项。真机样本（SM_F9560，
  Android 16）行形态 ``TOTAL PSS: 107000 TOTAL RSS: 169084 TOTAL SWAP PSS: 93646``；
  旧机型 ``TOTAL SWAP (PSS): n`` 与 ``TOTAL SWAP: n`` 变体一并容忍（正则见
  _SWAP_PSS_RE）。缺行 → None（机型无 swap 行是常态，不填 0）。
- ``vss_mb`` ← 两级来源：①dumpsys 输出内 OEM 若打印 TOTAL 段 VSS 行（机型差异）
  直接取用；②常态（AOSP/OneUI 明细表无 VSS 列，SM_F9560 实测）由采样器读
  ``/proc/<pid>/status`` 的 ``VmSize``（parse_vm_size_mb）兜底。皆缺 → None。

另提供设备级可用内存解析 ``parse_proc_meminfo_available_mb``（/proc/meminfo 的
MemAvailable 行，SM_F9560 实测可读——``dumpsys meminfo`` 输出**不含** MemAvailable
行，设备级指标不走 dumpsys）；``parse_proc_memory_snapshot`` 消费采样器合并
``echo pd_memavail=...; echo pd_vss=...`` 单次 shell 输出。
"""

from __future__ import annotations

import re
from typing import Any

# 明细表行标签（归一小写形态）→ 内部 key；行首精确匹配（后随冒号或空白）
_LABEL_ALIASES: dict[str, str] = {
    "native heap": "native_heap",
    "dalvik heap": "dalvik_heap",
    "dalvik other": "dalvik_other",
    "graphics": "graphics",  # 兼容别名：AOSP 明细段不打印（App Summary 才有），
    # OEM 若在明细段输出则并入 gfx（mem-1 修复注：该行与 Gfx dev 同池异名时
    # 求和正确；真实 AOSP 输出无此行，不会双计）
    "gfx dev": "gfx_dev",
    "egl mtrack": "egl_mtrack",  # mem-1 修复：第二图形池（gralloc/图形缓冲），
    # 旧映射缺失导致真实设备上被静默丢弃、gfx_mb 系统性低估
    "gl mtrack": "gl_mtrack",
}

# 每个标签一条行首正则：标签后（可选冒号）的第一个数值列（PSS Total / 单列 kB 值），
# 数值后必须紧跟空白/行尾（多列行的后续数值列不误吞）
_ALIAS_RES: dict[str, re.Pattern[str]] = {
    label: re.compile(
        rf"^{re.escape(label)}\s*:?\s+([0-9][0-9,]*)(?:\s*kB)?(?:\s|$)"
    )
    for label in _LABEL_ALIASES
}

# 解析输出键（测试 golden 对拍项）
DETAIL_KEYS = ("native_mb", "dalvik_mb", "gfx_mb", "gl_mb")

# 扩展输出键（2026-11-10）：swap/vss 与四项同次解析返回，但不入 DETAIL_KEYS
# （detail_complete 四项门禁口径不变，契约 §21.3）
SWAP_KEY = "swap_mb"
VSS_KEY = "vss_mb"

# 扩展 metricId（注册表行见 registry.py 2026-11-10 段；available 为设备级指标）
METRIC_SWAP_MB = "memory.swap.mb"
METRIC_VSS_MB = "memory.vss.mb"
METRIC_AVAILABLE_MB = "memory.available.mb"

# App Summary TOTAL 行的 swap 项（机型行名差异）：真机 SM_F9560 形态
# "TOTAL SWAP PSS: 93646"；旧机型 "TOTAL SWAP (PSS): n" / "TOTAL SWAP: n" 变体容忍。
# 数值紧跟可选冒号；负值（异常读数）不匹配按缺行处理。
_SWAP_PSS_RE = re.compile(r"swap\s*(?:\(\s*pss\s*\))?(?:\s*pss)?\s*:?\s*([0-9][0-9,]*)")
# OEM 在明细段 TOTAL 行/行名携带 VSS 的变体（AOSP 明细表无 VSS 列，SM_F9560 实测）：
# "total" 打头且行内含 vss 词，其后首个数值列
_VSS_TOKEN_RE = re.compile(r"vss[^0-9]*([0-9][0-9,]*)")


def _kb_to_mb(value: str) -> float:
    return round(float(value.replace(",", "")) / 1024.0, 3)


def _match_row(normalized: str) -> tuple[str, float] | None:
    for label, pattern in _ALIAS_RES.items():
        match = pattern.match(normalized)
        if match is not None:
            return _LABEL_ALIASES[label], _kb_to_mb(match.group(1))
    return None


def _match_swap_pss(normalized: str) -> float | None:
    """App Summary TOTAL 行 swap 项 → MB；非 swap 行/缺数值 → None。"""
    if "swap" not in normalized:
        return None
    match = _SWAP_PSS_RE.search(normalized)
    if match is None:
        return None
    return _kb_to_mb(match.group(1))


def _match_vss_total(normalized: str) -> float | None:
    """TOTAL 段 VSS 行（OEM 变体）→ MB；AOSP TOTAL 行（无 vss 词）恒 None。"""
    if not normalized.startswith("total") or "vss" not in normalized:
        return None
    match = _VSS_TOKEN_RE.search(normalized)
    if match is None:
        return None
    return _kb_to_mb(match.group(1))


def parse_meminfo_detail(text: str) -> dict[str, Any] | None:
    """明细段四项 + swap/vss 解析；返回 ``{key: MB | None}``，全缺 → None（不伪造）。

    单项缺失落 None（部分机型无 GL mtrack/Graphics 行是常态，契约 §21.3）；
    "App Summary" 段起为 perfeval 既有解析域——四项明细行**只**在明细段取值
    （App Summary 的 Native Heap/Code 等标签是 Private Dirty 口径，不跨段误取，
    mem-0 对拍先例），但 swap/vss 两行跨全文本扫描（App Summary TOTAL 行的
    swap 项与 OEM TOTAL VSS 行是本模块新增消费面，正则含 "swap"/"vss" 词约束，
    明细段常规行/表头不会误命中）。
    """
    rows: dict[str, float] = {}
    swap_mb: float | None = None
    vss_mb: float | None = None
    in_app_summary = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        normalized = " ".join(line.lower().split())
        if not in_app_summary:
            if normalized.startswith("app summary"):
                in_app_summary = True
                continue
            if normalized.startswith("total"):
                # 总量行非明细项（明细段内 TOTAL 行忽略）；OEM 若在 TOTAL 行携带
                # VSS 词（机型差异）则取 VSS 列，AOSP 原样行恒不命中
                if vss_mb is None:
                    vss_mb = _match_vss_total(normalized)
                continue
            matched = _match_row(normalized)
            if matched is not None:
                key, value_mb = matched
                rows.setdefault(key, value_mb)
        if swap_mb is None:
            swap_mb = _match_swap_pss(normalized)
        if vss_mb is None and in_app_summary:
            vss_mb = _match_vss_total(normalized)

    if not rows and swap_mb is None and vss_mb is None:
        return None
    detail: dict[str, Any] = {}
    detail["native_mb"] = rows.get("native_heap")
    if "dalvik_heap" in rows:
        detail["dalvik_mb"] = round(
            rows["dalvik_heap"] + rows.get("dalvik_other", 0.0), 3
        )
    else:
        detail["dalvik_mb"] = None
    gfx_parts = [rows[k] for k in ("graphics", "gfx_dev", "egl_mtrack") if k in rows]
    detail["gfx_mb"] = round(sum(gfx_parts), 3) if gfx_parts else None
    detail["gl_mb"] = rows.get("gl_mtrack")
    detail[SWAP_KEY] = swap_mb
    detail[VSS_KEY] = vss_mb
    return detail


def detail_complete(detail: dict[str, Any] | None) -> bool:
    """四项全部可解析（采样层 all-缺失判定用；门禁判据 2026-11-16 起不再用它）。"""
    if detail is None:
        return False
    return all(isinstance(detail.get(key), (int, float)) for key in DETAIL_KEYS)


def detail_channel_available(detail: dict[str, Any] | None) -> bool:
    """明细段**通道存在**判据（契约 §21.3 门禁，2026-11-16 修订）：native/dalvik
    任一可解析即认 ``dumpsys meminfo`` 明细表落地。

    为什么不再是 detail_complete（四项全齐）：图形池行（Gfx dev/EGL/GL mtrack）
    只在有 GL 上下文的进程出现——探测目标（system 常驻进程）缺图形行不代表被测
    应用缺（SM_F9560 与模拟器实测 system 均无 gfx 行，native/dalvik 却可解析，
    旧判据系统性误禁整组）。单项行缺失由采样层逐项不出点兜住（不写假零值语义
    不变）；被测应用有图形池时 gfx/gl 照常出点。
    """
    if detail is None:
        return False
    return detail.get("native_mb") is not None or detail.get("dalvik_mb") is not None


# ---- 设备级 /proc 解析（2026-11-10 扩展：memory.available.mb / memory.vss.mb）----
#
# 真机事实（SM_F9560，Android 16，2026-11-10 实测）：``dumpsys meminfo <pkg>`` 输出
# **不含** MemAvailable 行也不含 VSS 列——设备级可用内存取 /proc/meminfo 的
# MemAvailable；VSS 取 /proc/<pid>/status 的 VmSize。golden 以真机样本固化
# （perfeval/fixtures/android/golden/proc_meminfo_smf9560.txt、proc_status_smf9560.txt）。


def _parse_kb_value(text: str) -> float | None:
    """文本中的首个数值（kB，千分位容忍）→ MB；无数值 → None。"""
    match = re.search(r"([0-9][0-9,]*)", text)
    if match is None:
        return None
    return _kb_to_mb(match.group(1))


def parse_proc_meminfo_available_mb(text: str) -> float | None:
    """/proc/meminfo → MemAvailable 行（kB）→ MB（memory.available.mb 设备级口径）。"""
    for raw_line in text.splitlines():
        normalized = " ".join(raw_line.strip().lower().split())
        if normalized.startswith("memavailable"):
            return _parse_kb_value(normalized.removeprefix("memavailable:"))
    return None


def parse_vm_size_mb(text: str) -> float | None:
    """/proc/<pid>/status → VmSize 行（kB）→ MB（VSS 口径）；缺行 → None。"""
    for raw_line in text.splitlines():
        normalized = " ".join(raw_line.strip().lower().split())
        if normalized.startswith("vmsize"):
            return _parse_kb_value(normalized.removeprefix("vmsize:"))
    return None


# 采样器合并单次 shell 输出（echo key=value；缺失节点输出空值由解析端按缺项处理）
PROC_MEMORY_COMMAND_TEMPLATE = (
    "echo pd_memavail=$(grep -E '^MemAvailable:' /proc/meminfo 2>/dev/null); "
    "echo pd_vss=$(grep -E '^VmSize:' /proc/$(pidof {package} 2>/dev/null "
    "| cut -d' ' -f1)/status 2>/dev/null)"
)

_PROC_MEMORY_KEY_MAP = {"pd_memavail": "memavail_mb", "pd_vss": "vss_mb"}


def parse_proc_memory_snapshot(stdout: str) -> dict[str, float | None]:
    """``PROC_MEMORY_COMMAND`` 输出 → {"memavail_mb": MB | None, "vss_mb": MB | None}。

    行形态 ``pd_memavail=MemAvailable:    4800560 kB`` / ``pd_vss=VmSize:  20472344 kB``；
    节点缺失/SELinux 拒绝时 `$(...)` 输出空串 → None（不填 0）。
    """
    snapshot: dict[str, float | None] = {"memavail_mb": None, "vss_mb": None}
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        target = _PROC_MEMORY_KEY_MAP.get(key.strip().lower())
        if target is None:
            continue
        snapshot[target] = _parse_kb_value(value)
    return snapshot


# ---- 采样器（real 路径；复用 perfeval MemSampler 的命令面/探测面，不复制粘贴）----


class MemDetailSampler:
    """memory.detail.* 四项 + memory.swap/vss/available 三项采样（R17 + 2026-11-10）。

    - 组合 MemSampler（probe/命令构造/失败计数全复用），sample 改为解析明细段；
    - 单项缺失不出点（不填假值）；四项全缺按连续失败计数（阈值 2 记 diagnostic，
      与 MemSampler.pss 同款口径）；
    - probe 语义与 MemSampler 相同（探测通道可用性而非目标进程存活，smoke-fix2
      先例）；明细段整组可用性由设备 profile 探测（RealProvider._probe_meminfo_
      detail，R17 勾选组硬性依赖）在注册表交集判定承载；
    - swap/vss/available 三项：缺行出 null 点（value=None，契约 §6.1 append_null_
      record 同款语义——不填 0 不伪造；VSS 优先取 dumpsys 内 OEM VSS 行，
      缺则 /proc/<pid>/status VmSize 兜底）。
    """

    name = "mem_detail"
    metric_ids = (
        "memory.detail.native_mb",
        "memory.detail.dalvik_mb",
        "memory.detail.gfx_mb",
        "memory.detail.gl_mb",
        METRIC_SWAP_MB,
        METRIC_VSS_MB,
        METRIC_AVAILABLE_MB,
    )

    # 输出键 → metricId（解析键即注册表后缀：native_mb → memory.detail.native_mb）
    _KEY_BY_SUFFIX = {key: key for key in DETAIL_KEYS}

    def __init__(
        self,
        *,
        interval_ms: int = 2000,
        utc_now: Any = None,
    ) -> None:
        from perfeval.collectors.android.samplers.mem_sampler import MemSampler

        self._inner = MemSampler(interval_ms=interval_ms)
        self.interval_ms = self._inner.interval_ms
        self._diagnostics: list[str] = []
        self._fail_streak = 0

    @property
    def diagnostics(self) -> list[str]:
        return list(self._diagnostics)

    def drain_diagnostics(self) -> list[str]:
        drained = list(self._diagnostics)
        self._diagnostics.clear()
        return drained

    def probe(self, ctx: Any) -> Any:
        return self._inner.probe(ctx)

    def start(self, ctx: Any) -> None:
        return None

    def sample(self, ctx: Any) -> list[Any]:
        command = self._inner._meminfo_command(ctx)
        if not command:
            return []
        try:
            completed = ctx.command_runner(command, 30)
        except Exception:  # noqa: BLE001 - 单次读取失败按无数据处理，由调度器继续
            return self._track_miss()
        if completed.returncode != 0:
            return self._track_miss()
        detail = parse_meminfo_detail(completed.stdout)
        if not detail_complete(detail):
            # 整组缺失按失败计数；单项缺失时其余项仍出点（部分机型无 GL mtrack
            # 是常态，契约 §21.3 单项不填假值）
            if detail is None or all(detail.get(key) is None for key in DETAIL_KEYS):
                return self._track_miss()
        self._fail_streak = 0
        records = []
        from perfeval.collectors.android.samplers.base import SampleRecord

        tags = {
            "unit": "MB",
            "collector": "sampler.mem_detail",
            "samplingWindowMs": self.interval_ms,
            "tsUtc": self._inner._utc_now(),
        }
        for key in DETAIL_KEYS:
            value = (detail or {}).get(key)
            if not isinstance(value, (int, float)):
                continue
            records.append(
                SampleRecord(
                    metric_id=f"memory.detail.{self._KEY_BY_SUFFIX[key]}",
                    value=float(value),
                    ts_ms=ctx.elapsed_ms(),
                    tags=dict(tags),
                )
            )
        records.extend(self._memory_extra_records(detail, ctx, tags))
        return records

    def _memory_extra_records(
        self, detail: dict[str, Any] | None, ctx: Any, tags: dict[str, Any]
    ) -> list[Any]:
        """memory.swap/vss/available 三项（2026-11-10 扩展）。

        数值来源：swap ← dumpsys App Summary TOTAL SWAP PSS；vss ← dumpsys OEM VSS
        行优先、缺则 /proc/<pid>/status VmSize；available ← /proc/meminfo
        MemAvailable（设备级）。缺行出 null 点（value=None，append_null_record 同款
        语义——SSE/缓冲按 §6.1 null 消费，read_samples 丢弃非数值行，统计面不变）。
        """
        from perfeval.collectors.android.samplers.base import SampleRecord

        dumpsys_swap = (detail or {}).get(SWAP_KEY)
        dumpsys_vss = (detail or {}).get(VSS_KEY)
        proc = self._read_proc_memory(ctx)
        values = {
            METRIC_SWAP_MB: dumpsys_swap if isinstance(dumpsys_swap, (int, float)) else None,
            METRIC_VSS_MB: (
                dumpsys_vss
                if isinstance(dumpsys_vss, (int, float))
                else (proc or {}).get("vss_mb")
            ),
            METRIC_AVAILABLE_MB: (proc or {}).get("memavail_mb"),
        }
        ts_ms = ctx.elapsed_ms()
        return [
            SampleRecord(
                metric_id=metric_id,
                # value=None：null 点语义（契约 §6.1），缺行不伪造 0
                value=float(value) if isinstance(value, (int, float)) else None,  # type: ignore[arg-type]
                ts_ms=ts_ms,
                tags=dict(tags),
            )
            for metric_id, value in values.items()
        ]

    def _read_proc_memory(self, ctx: Any) -> dict[str, float | None] | None:
        """合并单次 shell 读取 MemAvailable + VmSize；命令失败 → None（缺项按 null）。"""
        if not ctx.package_name:
            return None
        command = ctx.adb.command(
            "shell",
            PROC_MEMORY_COMMAND_TEMPLATE.format(package=ctx.package_name),
            serial=ctx.serial,
        )
        try:
            completed = ctx.command_runner(command, 10)
        except Exception:  # noqa: BLE001 - 设备异常按无数据处理，出 null 点
            return None
        if completed.returncode != 0:
            return None
        return parse_proc_memory_snapshot(completed.stdout)

    def stop(self) -> list:
        return []

    def _track_miss(self) -> list:
        self._fail_streak += 1
        if self._fail_streak == 2:
            self._diagnostics.append(
                "sampler.mem_detail.detail.unavailable:meminfo detail section "
                "missing for 2 consecutive samples"
            )
        return []
