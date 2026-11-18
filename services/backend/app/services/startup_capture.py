"""startup_capture：run 级启动旁路测量作业（契约 §21.4 实现期编排口落地，S3）。

契约 §21.4 原文明示「测量触发与编排留实现期定：该指标会重新拉起 APP，默认不在
实时监控会话内自动执行」——本模块是 run 级**旁路**测量作业，不碰 session_manager
与实时采样链（与 S2 完全解耦）：

1. `adb -s <serial> logcat -c`（清缓冲，尽力而为）+ `adb_logcat_time_source` 尾随
   （log_stream.py 既有纯函数复用，既有类零改动）；
2. `am start -W -n <component>`（perfeval adb.py 同款命令构造；launcher 组件解析
   失败回退 `am start -W <package>`）→ TotalTime/LaunchState 解析正则与 perfeval
   runner `_derive_startup_event` 同源（runner.py:52-53 逐字 import，不复制口径）；
3. 并行解析 logcat 行：『Displayed pkg/… +Nms』→ TTID（ActivityTaskManager/
   ActivityManager 两种 tag 同解析）；『Fully drawn pkg/… +Nms』→ TTFD（仅对调用
   reportFullyDrawn() 的 App 产出，未观测 → null 而非 0，[R§8.6]）；
4. 结果追加落 run 目录 `startup.ndjson`（契约 §10 文件清单 delta 待 S1 落笔；
   `perfdog.startup.v1`），GET /runs/{runId}/startup 读回回填 ttidMs/ttfdMs。

行解析与等待循环均为纯逻辑（`parse_displayed_line`/`parse_fully_drawn_line`/
`_wait_for_lines`），fake line source/命令执行器直测；默认实现用真实 adb 子进程。
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from perfeval.collectors.android.adb import (
    AdbClient,
    launch_command,
    parse_launcher_activity_component,
    query_launcher_activities_command,
)
from perfeval.collectors.android.runner import (
    _AM_START_LAUNCH_STATE_RE,
    _AM_START_TOTAL_TIME_RE,
)

from ..errors import ApiError
from .log_stream import adb_logcat_time_source
from .statistics_service import resolve_run_dir

#: startup.ndjson 行 schema（契约 §10 delta：新增文件 perfdog.startup.v1）
STARTUP_NDJSON_SCHEMA = "perfdog.startup.v1"

# logcat『Displayed』/『Fully drawn』行（-v threadtime/tag 均适配）：message 形如
# `Displayed com.pkg/.MainActivity: +1s234ms`（秒可缺省：`+842ms` 亚秒形态同样合法）；
# 多用户设备（真机回归 2026-11-11，小米 2405CPX3DC/Android 16）为
# `Displayed com.pkg/.Act for user 0: +301ms`——组件与时长之间可有 `for user <N>` 尾巴
_DISPLAYED_RE = re.compile(
    r"^Displayed\s+(\S+?)(?:\s+for\s+user\s+\d+)?:\s+\+(?:(\d+)s)?(\d+)ms"
)
_FULLY_DRAWN_RE = re.compile(
    r"^Fully drawn\s+(\S+?)(?:\s+for\s+user\s+\d+)?:\s+\+(?:(\d+)s)?(\d+)ms"
)


def parse_displayed_line(message: str) -> float | None:
    """『Displayed pkg/.Act: +Ns NNNms』message → TTID 毫秒；非该形态 → None。"""
    return _parse_logcat_duration(_DISPLAYED_RE, message)


def parse_fully_drawn_line(message: str) -> float | None:
    """『Fully drawn pkg/.Act: +Ns NNNms』message → TTFD 毫秒；非该形态 → None。"""
    return _parse_logcat_duration(_FULLY_DRAWN_RE, message)


def _parse_logcat_duration(pattern: re.Pattern[str], message: str) -> float | None:
    match = pattern.match(message.strip())
    if match is None:
        return None
    _component, seconds, millis = match.groups()
    return float((int(seconds) if seconds else 0) * 1000 + int(millis))


def read_startup_ndjson(run_path: Path) -> tuple[int | None, int | None]:
    """run 目录 startup.ndjson → 最近一次 state=completed 记录的 (ttidMs, ttfdMs)。

    无该文件 / 不可解析 / 无 completed 记录 → (None, None)（调用方以 null 承载，
    不填 0）；多行按行序取**最后一条** completed（追加式，最新捕获胜出）。
    """
    path = run_path / "startup.ndjson"
    if not path.is_file():
        return None, None
    ttid: int | None = None
    ttfd: int | None = None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("state") != "completed":
            continue
        ttid = _ms_or_none(record.get("ttidMs"))
        ttfd = _ms_or_none(record.get("ttfdMs"))
    return ttid, ttfd


def _ms_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _manifest_package_name(run_path: Path) -> str | None:
    path = run_path / "manifest.json"
    if not path.is_file():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    name = manifest.get("packageName") if isinstance(manifest, dict) else None
    return name if isinstance(name, str) and name else None


def _feed_queue(source: Iterator[str], queue: "Queue[str]") -> None:
    """行源 → 队列（守护线程；生成器结束/异常均终止入队）。"""
    try:
        for line in source:
            queue.put(line)
    except Exception:  # noqa: BLE001 —— 流异常按耗尽处理，主循环按 deadline 收口
        return


def capture_startup(
    runs_dir: Path,
    run_id: str,
    device_serial: str,
    *,
    package_name: str | None = None,
    adb_path: str | None = None,
    am_start_timeout_s: float = 30.0,
    total_deadline_s: float = 20.0,
    ttfd_grace_s: float = 5.0,
    line_source_factory: Callable[[str], Callable[[], Iterator[str] | None]] | None = None,
    command_runner: Callable[..., Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
    now_epoch: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """执行启动捕获并追加落盘 startup.ndjson；返回本次记录（含 note）。

    注入 seam（测试 fake adb）：``line_source_factory``（缺省 log_stream.
    adb_logcat_time_source 纯函数复用）、``command_runner``（缺省 AdbClient.run）、
    ``clock``/``now_epoch``。步骤失败语义：
    - adb 缺失 / logcat 流不可启动 → ApiError adb_not_found（503）；
    - am start 执行失败或输出不可解析 → 返回 state=failed 记录（200 如实申报，
      **不落盘**——无测量产物不伪造）；
    - am start 成功但未观测到对应 logcat 行 → state=completed + 对应值 null
      + note（落盘；无 Fully drawn 行 → ttfdMs null，不伪造）。
    """
    run_path = resolve_run_dir(runs_dir, run_id)
    package = package_name or _manifest_package_name(run_path)
    adb = AdbClient(adb_path)
    runner = command_runner or adb.run

    captured_at = int(now_epoch() * 1000)
    base_record: dict[str, Any] = {
        "schemaVersion": STARTUP_NDJSON_SCHEMA,
        "runId": f"run-{run_path.name}",
        "capturedAt": captured_at,
        "deviceSerial": device_serial,
        "packageName": package,
    }

    # 1) 清 logcat 缓冲（尽力而为：失败不阻断，记录如实标注）
    logcat_cleared = False
    try:
        completed = runner("logcat", "-c", serial=device_serial, timeout=10.0)
        logcat_cleared = completed.returncode == 0
    except OSError as exc:
        raise ApiError("adb_not_found", f"adb 不可用：{exc}") from None
    except Exception:  # noqa: BLE001 —— 清缓冲超时等失败不阻断主流程（如实标注）
        logcat_cleared = False

    # 2) logcat 尾随先行（Displayed 行与 am start 几乎同时产出，先起流防漏）
    line_source = (
        line_source_factory(device_serial) if line_source_factory is not None
        else adb_logcat_time_source(device_serial, adb_path=adb.adb_path)
    )
    lines = line_source()
    if lines is None:
        raise ApiError("adb_not_found", "logcat 流未能启动（adb 缺失或设备不可达）")

    # 3) am start -W（组件解析失败回退包名形态；TotalTime 解析与 runner 同源）
    am_error: str | None = None
    total_ms: int | None = None
    launch_state: str | None = None
    component: str | None = None
    try:
        component = _resolve_component(runner, device_serial, package)
        if component is not None:
            completed = runner(
                *launch_command(adb, component)[1:],
                serial=device_serial,
                timeout=am_start_timeout_s,
            )
        else:
            completed = runner(
                "shell", "am", "start", "-W", package or "",
                serial=device_serial, timeout=am_start_timeout_s,
            )
        if completed.returncode != 0 or "Error" in (completed.stdout or ""):
            am_error = (completed.stdout or completed.stderr or "").strip()[:200] or "am start failed"
        else:
            total_ms = _parse_total_time(completed.stdout or "")
            launch_state = _parse_launch_state(completed.stdout or "")
            if total_ms is None:
                am_error = "am start -W 输出无 TotalTime（不可解析）"
    except Exception as exc:  # noqa: BLE001 —— am start 执行失败（OSError/超时/命令构造）→ failed 申报
        am_error = f"am start 执行失败：{exc}"

    if am_error is not None:
        _close_lines(lines)
        # 未产出测量产物：不落盘、不伪造（§1 空值原则），HTTP 200 如实申报
        return {
            **base_record,
            "state": "failed",
            "component": component,
            "totalTimeMs": None,
            "launchState": None,
            "ttidMs": None,
            "ttfdMs": None,
            "logcatCleared": logcat_cleared,
            "error": am_error,
            "note": "启动捕获失败：" + am_error + "（未写入 startup.ndjson，无测量产物不伪造）",
        }

    # 4) 行等待循环（队列 + deadline；TTID 后给 TTFD 宽限期；package 前缀防污染）
    ttid_ms, ttfd_ms = _wait_for_lines(
        lines,
        deadline_s=total_deadline_s,
        ttfd_grace_s=ttfd_grace_s,
        clock=clock,
        package=package,
    )

    note = _capture_note(ttid_ms, ttfd_ms)
    record: dict[str, Any] = {
        **base_record,
        "state": "completed",
        "component": component,
        "totalTimeMs": total_ms,
        "launchState": launch_state,
        "ttidMs": None if ttid_ms is None else int(ttid_ms),
        "ttfdMs": None if ttfd_ms is None else int(ttfd_ms),
        "logcatCleared": logcat_cleared,
        "sources": {
            "totalTime": "am_start -W TotalTime（perfeval runner _derive_startup_event 同源口径）",
            "ttid": "logcat『Displayed』行（ActivityTaskManager/ActivityManager 同解析）",
            "ttfd": "logcat『Fully drawn』行（需应用调用 reportFullyDrawn()）",
        },
        "note": note,
    }
    _append_startup_ndjson(run_path, record)
    return record


def _resolve_component(
    runner: Callable[..., Any], serial: str, package: str | None
) -> str | None:
    """query-activities → launcher 组件（perfeval adb.py 同款解析）；失败 → None。"""
    if not package:
        return None
    try:
        completed = runner(
            *query_launcher_activities_command(AdbClient(), package)[1:],
            serial=serial,
            timeout=15.0,
        )
    except (OSError, ValueError):
        return None
    if completed.returncode != 0:
        return None
    return parse_launcher_activity_component(completed.stdout or "", package_name=package)


def _parse_total_time(stdout: str) -> int | None:
    """am start -W stdout → TotalTime（runner._derive_startup_event 同源正则）。"""
    match = _AM_START_TOTAL_TIME_RE.search(stdout)
    return int(match.group(1)) if match is not None else None


def _parse_launch_state(stdout: str) -> str | None:
    match = _AM_START_LAUNCH_STATE_RE.search(stdout)
    return match.group(1).lower() if match is not None else None


def _wait_for_lines(
    lines: Iterator[str],
    *,
    deadline_s: float,
    ttfd_grace_s: float,
    clock: Callable[[], float],
    package: str | None = None,
) -> tuple[float | None, float | None]:
    """logcat 行流 → (TTID ms, TTFD ms)；deadline/宽限期由 clock 注入（可测）。

    行剥离复用 log_stream.parse_logcat_time_line（§21.7 同一解析器，threadtime
    列式）→ message 按『Displayed』/『Fully drawn』行匹配；``package`` 给出时
    仅接受该包的行（logcat -c 失败时的旧行污染防护），缺省接受任意包。
    收口条件：两值齐 / deadline 到 / TTID 已得且宽限期过（TTFD 仅对调用
    reportFullyDrawn() 的 App 产出，未观测 → None 不伪造）。行源在守护线程入队，
    主循环带超时取行——真实 adb 静默期不会阻塞 deadline 收口。
    """
    from .log_stream import parse_logcat_time_line

    def _message_of(raw_line: str) -> str | None:
        parsed = parse_logcat_time_line(raw_line)
        return None if parsed is None else parsed[2]

    prefix = f"{package}/" if package else None

    queue: "Queue[str]" = Queue()
    feeder = threading.Thread(target=_feed_queue, args=(lines, queue), daemon=True)
    feeder.start()

    deadline = clock() + deadline_s
    ttid_ms: float | None = None
    ttfd_ms: float | None = None
    ttid_seen_at: float | None = None

    def _accept(message: str, pattern: re.Pattern[str]) -> bool:
        match = pattern.match(message.strip())
        if match is None:
            return False
        return prefix is None or match.group(1).startswith(prefix)

    try:
        while clock() < deadline:
            remaining = deadline - clock()
            try:
                line = queue.get(timeout=min(0.5, max(0.01, remaining)))
            except Empty:
                if ttid_seen_at is not None and clock() - ttid_seen_at >= ttfd_grace_s:
                    break
                continue
            message = _message_of(line)
            if message is None:
                continue  # 续行/无级别可判行：跳过（不伪造）
            if ttid_ms is None and _accept(message, _DISPLAYED_RE):
                ttid_ms = parse_displayed_line(message)
                ttid_seen_at = clock()
                continue
            if ttfd_ms is None and _accept(message, _FULLY_DRAWN_RE):
                ttfd_ms = parse_fully_drawn_line(message)
                if ttid_ms is not None:
                    break
    finally:
        _close_lines(lines)
    return ttid_ms, ttfd_ms


def _close_lines(lines: Iterator[str]) -> None:
    close = getattr(lines, "close", None)
    if close is not None:
        try:
            close()
        except Exception:  # noqa: BLE001 —— 清理失败不掩盖主流程
            pass


def _capture_note(ttid_ms: float | None, ttfd_ms: float | None) -> str:
    """捕获记录 note（缺失值如实声明口径，不填 0）。"""
    parts: list[str] = []
    parts.append("TTID=logcat『Displayed』行解析（等价采集已落地）")
    if ttfd_ms is None:
        parts.append(
            "TTFD=null：本次捕获未观测到『Fully drawn』行（需应用调用 "
            "reportFullyDrawn() 才产出，[F§3.11]；不填 0）"
        )
    else:
        parts.append("TTFD=logcat『Fully drawn』行解析")
    if ttid_ms is None:
        parts.append("TTID=null：本次捕获未观测到『Displayed』行（不填 0）")
    return "；".join(parts)


def _append_startup_ndjson(run_path: Path, record: dict[str, Any]) -> None:
    """追加一行 JSON（UTF-8；追加式——多轮捕获共存，读回取最新 completed）。"""
    path = run_path / "startup.ndjson"
    with path.open("a", encoding="utf-8") as sink:
        sink.write(json.dumps(record, ensure_ascii=False) + "\n")


__all__ = [
    "STARTUP_NDJSON_SCHEMA",
    "capture_startup",
    "parse_displayed_line",
    "parse_fully_drawn_line",
    "read_startup_ndjson",
]
