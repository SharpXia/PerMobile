"""xctrace 客户端（真机 iOS Activity Monitor 主通道，R24 契约 §26.4）。

主通道链路（勘察推荐的已实测链路，契约 §26 章头注；2026-11-16 真机 E2E 修订）::

    devicectl 预热/lockState 检查（prewarm_device）
      → xcrun xctrace record --template 'Activity Monitor' --all-processes
        --time-limit Ns --no-prompt（build_record_command）
      → xcrun xctrace export --toc / --xpath（build_export_command）
      → 解析 activity-monitor-process-live 表（parse_process_live_xml）
        出 CPU% + physical footprint ~1s 双序列 → 按目标进程名过滤

诚实性口径（勘察+真机 E2E 实测，2026-11-16）：

- 隧道冷启动 xctrace record 报 exit 13 → 录制前必跑 devicectl 预热（勘察验证
  成功率 100%；本机复测预热命令面 exit 0）；
- **``--attach <进程名>`` 在真机 iOS 27.x 上不可用**（exit 19 "Cannot find
  process matching name"，任意进程、wired 与 WiFi 形态均复现）→ 改
  ``--all-processes`` 录制 + 解析后按进程名过滤（目标语义等价）；
  历史注记：``--attach`` 禁 PID 为勘察实测 exit 21；
- 并发 xcrun 命令会失败（勘察实测）→ 本模块 ``_XCRUN_DEVICE_LOCK`` 串行化——
  锁本体为 ``devicectl.XCRUN_FAMILY_LOCK``（进程级 RLock），devicectl 执行面
  同锁串行：采集窗内/外探测与聚合面枚举在窗期间排队，不与 record/export 并发；
  R22 DeviceCommandQueue（per-device）维持 Android 命令面（wifi/speedtest/
  sim-boot）串行，iOS xcrun 面不经它（机制与覆盖范围如实声明，契约 §26.4 已同步）；
- .trace bundle 大（4s≈11.7MB）→ export 解析后即删（run 目录不落 .trace，§26.4）；
- export XML 单元格语法（本机宿主 trace 真导出验证）：单元格按**列位**对齐表头
  ``<col>`` 序，``<X ref="N"/>`` 引用先行单元格、``<sentinel/>`` = 该格无数据；
  解析按列位取值不依赖单元格标签名（真机 cpu-percent 单元格标签未实录到——
  宿主录制该列恒 sentinel——fixture 以 ``system-cpu-percent`` 标签重构，
  README 存档）。

错误码（契约 §3 §26 新增行）：``xctrace_unavailable`` 503，
``details.reason`` ∈ xctrace_missing / devicectl_missing / prewarm_failed /
record_failed / export_failed / devicectl_probe_failed（末位为窗前探测重试耗尽，
realios._probe_with_retry 发出，2026-11-16 审读新增）。
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable

from ..errors import ApiError, ERROR_STATUS
from . import devicectl

# 契约 §3 §26 新增行：503 xctrace_unavailable。errors.py 正式行由契约流收编前
# 此处幂等登记（setdefault 不覆盖既有行；命名与 §3 冻结一致）。
XCTRACE_UNAVAILABLE = "xctrace_unavailable"
ERROR_STATUS.setdefault(XCTRACE_UNAVAILABLE, 503)

# Activity Monitor 模板（xcrun xctrace list templates 实测在场）+ 目标表
TEMPLATE = "Activity Monitor"
TABLE_SCHEMA = "activity-monitor-process-live"
XPATH = (
    f'/trace-toc/run[@number="1"]/data/table[@schema="{TABLE_SCHEMA}"]'
)

# 并发 xcrun 命令会失败（勘察实测）→ 设备命令全程串行（进程级；跨设备同样串行，
# 收敛换稳定）。锁本体在 devicectl.XCRUN_FAMILY_LOCK（RLock）：devicectl 执行面
# （采集窗内探测、聚合面枚举、launch）同锁串行，本模块别名沿用旧名；窗口链路
# 全程持锁 + 锁内 prewarm 同线程再入（RLock 重入语义）。
_XCRUN_DEVICE_LOCK = devicectl.XCRUN_FAMILY_LOCK

# record 退出后的窗口缓冲（export 两跳 + 退出开销；超时兜底，正常远小于此）
_RECORD_TIMEOUT_MARGIN_S = 30.0
# export 超时（勘察 4s trace export 秒级完成；10 倍余量）
_EXPORT_TIMEOUT_S = 60.0


class XctraceWindowError(Exception):
    """record/export 执行失败（非 xcrun 缺失）：reason ∈ record_failed/export_failed。

    采集层转 diagnostic(xctrace.window.failed)（契约 §26.5）；不进 HTTP 错误体
    （窗口批采样在采集线程，无请求上下文）。
    """

    def __init__(
        self, reason: str, message: str, exit_code: int | None = None
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.exit_code = exit_code


def run_xctrace(
    *args: str,
    timeout: float,
    runner: Callable[[list[str], float], Any] | None = None,
) -> subprocess.CompletedProcess[str] | None:
    """``xcrun xctrace <args>`` 文本执行。

    ``runner`` 注入面同 run_devicectl。xcrun 本体缺失（FileNotFoundError）
    **透传**（调用方归一 xctrace_unavailable + details.reason="xctrace_missing"，
    与命令非零退出区分）；超时/其余 SubprocessError → None（调用方按失败处置）。
    """
    command = ["xcrun", "xctrace", *args]
    # devicectl 执行面同一把家族锁（XCRUN_FAMILY_LOCK）：窗口链路已在外层持锁，
    # 此处 RLock 同线程重入；窗口外的 xctrace 调用（如未来 list templates 面）
    # 也与 devicectl 探测互斥（并发 xcrun 命令会失败，勘察实测）。
    with _XCRUN_DEVICE_LOCK:
        if runner is not None:
            return runner(command, timeout)
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
        except FileNotFoundError:
            raise
        except subprocess.SubprocessError:
            return None


# ---- 命令构造（纯函数；测试断言调用序列的锚点）----


def build_record_command(
    device_id: str, time_limit_s: float, output_path: str
) -> list[str]:
    """record 命令（契约 §26.4，2026-11-16 真机实测修订）：**--all-processes**。

    原 ``--attach <进程名>`` 在真机 iOS 27.x 上对任意进程 exit 19（"Cannot find
    process matching name"，wired 与 WiFi 形态均复现，SpringBoard/AgentTool/
    Runner 同录）——xctrace 侧 attach 枚举不可用；``--all-processes`` 实录正常且
    目标进程行在场（AgentTool 实测 4 行真值），目标过滤移到解析后按进程名做
    （语义等价：行数=目标进程行，空=进程不在场 → §26.5 进程消失语义）。

    ``--time-limit`` 接受 ``Ns`` 形态（--help 实测 ``<time[ms|s|m|h]>``）；
    ``--no-prompt`` 跳过隐私提示（无人值守必需）。
    """
    return [
        "record",
        "--template",
        TEMPLATE,
        "--device",
        device_id,
        "--all-processes",
        "--time-limit",
        f"{int(time_limit_s)}s",
        "--no-prompt",
        "--output",
        output_path,
    ]


def build_export_command(
    trace_path: str, *, toc: bool = False, output_path: str | None = None
) -> list[str]:
    """export 命令：``--toc``（表清单/完整性核验）或默认 ``--xpath``（目标表）。

    --toc/--xpath 是两种互斥模式（--help 实测 "Table of Contents and XPath
    query are two separate modes"）；toc=True 出表清单，否则出 XPATH 指定的
    activity-monitor-process-live 表。
    """
    command = ["export", "--input", trace_path]
    if toc:
        command.append("--toc")
    else:
        command.extend(["--xpath", XPATH])
    if output_path is not None:
        command.extend(["--output", output_path])
    return command


# ---- 预热（契约 §26.4：冷启动 exit 13 的前置修复 + lockState 前置检查）----


def prewarm_device(
    device_id: str,
    *,
    runner: Callable[[list[str], float], Any] | None = None,
    attempts: int = 3,
    timeout: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """devicectl lockState 预热 + 重试；重试耗尽 → 503 xctrace_unavailable。

    勘察实测：隧道冷启动使 xctrace record 报 exit 13，预热后成功率 100%；锁屏
    下个别 devicectl 子命令瞬时失败 → lockState 前置 + 重试（契约 §26.4）。
    重试耗尽 → ``ApiError(XCTRACE_UNAVAILABLE, details.reason="prewarm_failed")``；
    xcrun 本体缺失 → 同码 ``details.reason="devicectl_missing"``（§3 两态）。
    """
    last_detail = ""
    for attempt in range(max(1, attempts)):
        try:
            completed = devicectl.run_devicectl(
                "device", "info", "lockState", "--device", device_id,
                timeout=timeout, runner=runner,
            )
        except FileNotFoundError as exc:
            raise ApiError(
                XCTRACE_UNAVAILABLE,
                "xcrun devicectl 不可用（命令行工具缺失或非 macOS 环境）",
                details={"reason": "devicectl_missing"},
            ) from exc
        if completed is not None and completed.returncode == 0:
            return
        if completed is not None:
            last_detail = (completed.stderr or completed.stdout or "").strip()[:200]
        if attempt < attempts - 1:
            sleep(1.0)
    raise ApiError(
        XCTRACE_UNAVAILABLE,
        f"devicectl 预热/lockState 重试耗尽（device {device_id}）：{last_detail or '命令无输出'}",
        details={"reason": "prewarm_failed", "attempts": max(1, attempts)},
    )


# ---- activity-monitor-process-live 表解析（纯函数，离线可跑）----

# process 单元格 fmt 形态（本机真导出实测）："Runner (15436)"
_PROCESS_FMT_RE = re.compile(r"^(?P<name>.+) \((?P<pid>\d+)\)$")


def parse_process_live_xml(xml_text: str) -> list[dict[str, Any]]:
    """activity-monitor-process-live 表 XML → 行序列（窗口内 ~1s 粒度）。

    返回 ``[{offset_ms, pid, process, cpu_pct, footprint_mb}]``：

    - ``offset_ms``：表内 Start Time（ns）→ ms（窗口锚点偏移，§26.4：点 ts =
      窗口起始宿主时钟 + 本偏移）；
    - ``cpu_pct``：% CPU 列（勘察实测设备行 10ms 采样粒度聚合值）；单元格
      sentinel → None（该格无数据，不猜——宿主录制该列恒 sentinel 的真导出
      fixture 为负例锚点）；
    - ``footprint_mb``：Memory（physical footprint）列字节 → MiB 数值（MB 单位
      口径与 §23.2 parse_ps_stat 的 KB→MB 同约定 /1024，与 Instruments fmt
      显示一致的 29.89 MiB 形态）；sentinel → None；
    - ``pid``/``process``：目标进程（单元格 ``ref`` 引用先行单元格，先建
      id→元素索引再取值）。

    表头 ``<col>`` 驱动**按列位**取值（不依赖单元格标签名）；单元格数与列数
    不齐的行跳过（防格式漂移静默错位）。XML 不可解析 → []（不猜）。
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    schema = None
    for node in root.iter("schema"):
        if node.get("name") == TABLE_SCHEMA:
            schema = node
            break
    if schema is None:
        return []
    mnemonics = [col.findtext("mnemonic") or "" for col in schema.findall("col")]
    if "start" not in mnemonics:
        return []
    index = {name: i for i, name in enumerate(mnemonics)}

    rows: list[dict[str, Any]] = []
    # ref 索引必须**文档级**：真机 trace（iOS 27.x 实测）的单元格 ref 跨行共享
    # （如 `<start-time ref="1"/>` 引用首行定义的 id=1），按行内建索引会让绝大多数
    # 行 ref 解析失败整行跳过（2026-11-16 真机 E2E 实锤：1676 行只幸存 6 行自包含行
    # ——宿主录制恰为行内自包含形态，golden 未暴露）。ref 链式引用跟随至多 3 跳。
    doc_by_id: dict[str, Any] = {
        cell.get("id"): cell for cell in root.iter() if cell.get("id") is not None
    }
    for row in root.iter("row"):
        cells = list(row)
        if len(cells) != len(mnemonics):
            continue  # 列位不齐（格式漂移）→ 整行跳过，不猜

        def cell_value(position: int) -> str | None:
            cell = cells[position]
            for _ in range(3):  # ref 链式跟随（ref→ref→值），有界防环
                if "ref" not in cell.attrib:
                    break
                cell = doc_by_id.get(cell.get("ref"), cell)
            if cell.tag == "sentinel" or cell.text is None:
                return None
            return cell.text.strip()

        start_ns = _to_int(cell_value(index["start"]))
        if start_ns is None:
            continue
        # pid 列（单元格常为 ref 引用 process 单元格内 pid）
        pid = _to_int(cell_value(index["pid"]))
        process = None
        proc_cell = cells[index["process"]]
        if "ref" in proc_cell.attrib:
            for _ in range(3):  # 同 cell_value：文档级 ref 链式跟随
                if "ref" not in proc_cell.attrib:
                    break
                proc_cell = doc_by_id.get(proc_cell.get("ref"), proc_cell)
        fmt = proc_cell.get("fmt") or ""
        match = _PROCESS_FMT_RE.match(fmt)
        if match is not None:
            process = match.group("name")
            if pid is None:
                pid = int(match.group("pid"))
        cpu_pct = _to_float(cell_value(index["cpu-percent"]))
        footprint_bytes = _to_int(cell_value(index["memory-physical-footprint"]))
        rows.append(
            {
                "offset_ms": round(start_ns / 1_000_000),
                "pid": pid,
                "process": process,
                "cpu_pct": cpu_pct,
                "footprint_mb": (
                    round(footprint_bytes / 1048576.0, 1)
                    if footprint_bytes is not None
                    else None
                ),
            }
        )
    return rows


# ---- 窗口批采样（预热 → record → export → 解析 → 清理；全程串行锁内）----


def run_sample_window(
    device_id: str,
    process_name: str,
    window_s: float,
    *,
    work_dir: str,
    devicectl_runner: Callable[[list[str], float], Any] | None = None,
    xctrace_runner: Callable[[list[str], float], Any] | None = None,
    epoch_clock: Callable[[], float] = time.time,
    prewarm_attempts: int = 3,
) -> dict[str, Any]:
    """执行一个采样窗（契约 §26.4 窗口批模型），返回行序列与窗口锚点。

    链路：预热（lockState 重试）→ record（--all-processes，解析后按进程名过滤）→
    export --toc →
    export --xpath → 解析 → 清理 .trace/.toc（run 目录不落 .trace 工件）。
    全程持有 ``_XCRUN_DEVICE_LOCK``（并发 xcrun 命令会失败，勘察实测）。

    - 窗口锚点：``windowStartEpochMs = record 退出时刻宿主时钟 - time-limit``
      （§26.4「窗口 ts 取 record 进程退出时刻回推 time-limit」；首窗预热时延
      不均摊进数据点）；行 ts = 锚点 + 行 offset_ms（parse 层产出）；epoch
      在 record 退出判定后**立即采样**（进入 export 前）——export 两跳的秒级
      耗时不推后窗口时戳（README 实测 export ~4s/跳，若在链路末尾取钟会把
      每个数据点系统性推后 export 耗时）；
    - xcrun 缺失 → ``ApiError(XCTRACE_UNAVAILABLE, reason="xctrace_missing")``；
    - 预热失败 → ``ApiError(..., reason="prewarm_failed"/"devicectl_missing")``；
    - record 非零退出 / export 失败 → ``XctraceWindowError``（reason =
      record_failed / export_failed，携带 exit code）——调用方转
      diagnostic(xctrace.window.failed)，该窗不出点（§26.5）；
    - record 成功但行空（进程录制中消失等）→ rows=[] 返回，语义归调用方
      （§26.5 目标进程消失：diagnostic + null 点）。
    """
    with _XCRUN_DEVICE_LOCK:
        prewarm_device(
            device_id, runner=devicectl_runner, attempts=prewarm_attempts
        )
        os.makedirs(work_dir, exist_ok=True)  # 直调面兜底（采集层经 mkdtemp 已存在）
        trace_path = os.path.join(work_dir, "window.trace")
        toc_path = os.path.join(work_dir, "toc.xml")
        table_path = os.path.join(work_dir, "table.xml")
        record_exit: int | None = None
        try:
            try:
                completed = run_xctrace(
                    *build_record_command(device_id, window_s, trace_path),
                    timeout=window_s + _RECORD_TIMEOUT_MARGIN_S,
                    runner=xctrace_runner,
                )
            except FileNotFoundError as exc:
                raise ApiError(
                    XCTRACE_UNAVAILABLE,
                    "xcrun xctrace 不可用（命令行工具缺失或非 macOS 环境）",
                    details={"reason": "xctrace_missing"},
                ) from exc
            if completed is None:
                raise XctraceWindowError(
                    "record_failed", "xctrace record 执行失败（超时/无法启动）"
                )
            record_exit = completed.returncode
            if record_exit != 0:
                detail = (completed.stderr or completed.stdout or "").strip()[-300:]
                raise XctraceWindowError(
                    "record_failed",
                    f"xctrace record 退出码 {record_exit}: {detail}",
                    exit_code=record_exit,
                )
            # 窗口锚点在 record 退出即采样（§26.4：record 退出时刻回推 time-limit；
            # export 两跳秒级耗时不计入——链路末尾取钟会把全部点时戳推后 export 耗时）
            record_exit_epoch = epoch_clock()
            # export --toc：trace 完整性核验（§26.4 export --toc/--xpath 链路）
            _export_or_raise(
                build_export_command(trace_path, toc=True, output_path=toc_path),
                xctrace_runner,
            )
            # export --xpath：目标表导出
            table_completed = _export_or_raise(
                build_export_command(trace_path, output_path=table_path),
                xctrace_runner,
            )
            table_xml = _read_text(table_path)
            if table_xml is None and table_completed.stdout:
                table_xml = table_completed.stdout
            rows = parse_process_live_xml(table_xml or "")
            # --all-processes 录制 → 按目标进程名过滤（§26.4 修订：attach 枚举
            # 不可用）；过滤后空 = 目标进程不在场 → 调用方走 §26.5 进程消失语义
            rows = [row for row in rows if row.get("process") == process_name]
        finally:
            # .trace bundle 大（4s≈11.7MB）→ 即删（§26.4：run 目录不落 .trace）
            for path in (trace_path, toc_path, table_path):
                try:
                    if os.path.exists(path):
                        os.unlink(path)
                except OSError:
                    pass
    return {
        "rows": rows,
        "windowStartEpochMs": int(record_exit_epoch * 1000) - int(window_s * 1000),
        "recordExitCode": record_exit,
    }


def _export_or_raise(
    command: list[str],
    xctrace_runner: Callable[[list[str], float], Any] | None,
) -> Any:
    try:
        completed = run_xctrace(*command, timeout=_EXPORT_TIMEOUT_S, runner=xctrace_runner)
    except FileNotFoundError as exc:
        raise ApiError(
            XCTRACE_UNAVAILABLE,
            "xcrun xctrace 不可用（命令行工具缺失或非 macOS 环境）",
            details={"reason": "xctrace_missing"},
        ) from exc
    if completed is None or completed.returncode != 0:
        detail = (
            (completed.stderr or completed.stdout or "") if completed else ""
        ).strip()[-300:]
        raise XctraceWindowError(
            "export_failed",
            f"xctrace export 失败: {detail or '命令无输出'}",
            exit_code=completed.returncode if completed else None,
        )
    return completed


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        return text or None
    except OSError:
        return None


def _to_int(text: str | None) -> int | None:
    if text is None:
        return None
    try:
        return int(text.replace(",", ""))
    except ValueError:
        return None


def _to_float(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None
