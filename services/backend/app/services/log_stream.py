"""实时日志流（M6 R33，契约 §21.7）：SSE ``log`` type 实装。

- real：``adb -s <serial> logcat -v time`` 持续流，行内解析优先级字母
  （V/D/I/W/E/F）按会话 ``logLevel`` 过滤（选中级别及更高级别放行，标准 logcat
  语义）；行解析失败的续行（logcat 多行消息的非首行）丢弃——信封 ``level`` 为
  契约字段，无级别可判的行不伪造；
- synthetic：确定性合成日志行（固定脚本循环，各级别样本齐全），同一过滤口径；
- 生命周期：会话创建（logLevel 提供）起线程，``record/stop`` 或会话收口
  （save/discard）终止——与采集线程同生命周期（SessionManager 接线）；
- 命令构造与 custom_events.adb_logcat_line_source 同形（Popen 行流；启动失败
  → None → 通道诊断；正常 stop 经生成器 close() 终止进程）。
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Iterator
from typing import Any

from .adb_logcat import AdbLogcatLines, open_adb_logcat

# 级别序（契约 §21.7 logLevel 枚举；index 越大级别越高）
LOG_LEVELS = ("verbose", "debug", "info", "warn", "error")
_LEVEL_INDEX = {name: index for index, name in enumerate(LOG_LEVELS)}

# logcat -v time 行：`11-01 15:04:05.123  1234  5678 D Tag   : message`
_LOGCAT_TIME_RE = re.compile(
    r"^\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3}\s+\d+\s+\d+\s+([VDIWEF])\s+(\S+)\s*:\s(.*)$"
)
_PRIORITY_BY_LETTER = {"V": "verbose", "D": "debug", "I": "info", "W": "warn", "E": "error"}


def parse_logcat_time_line(line: str) -> tuple[str, str, str] | None:
    """logcat -v time 行 → (level, source/tag, message)；无法解析（多行续行等）
    返回 None（不伪造级别）。"""
    match = _LOGCAT_TIME_RE.match(line.rstrip())
    if match is None:
        return None
    letter, tag, message = match.groups()
    level = _PRIORITY_BY_LETTER.get(letter)
    if level is None:
        return None
    return level, tag, message


def level_passes(line_level: str, selected: str) -> bool:
    """级别过滤：行级别 ≥ 选中级别放行（选中 debug 时 D/I/W/E 均放行）。"""
    line_index = _LEVEL_INDEX.get(line_level)
    selected_index = _LEVEL_INDEX.get(selected)
    if line_index is None or selected_index is None:
        return False
    return line_index >= selected_index


def adb_logcat_time_source(
    serial: str, adb_path: str | None = None
) -> Callable[[], Iterator[str] | None]:
    """``adb -s <serial> logcat -v threadtime`` 行源工厂（契约 §21.7 real 通道）。

    实机验证修复（2026-11-03）：原 `-v time` 在 Android 16（SM_F9560 与
    emulator-5554 实测一致）输出 `MM-DD HH:MM:SS.mmm L/TAG(PID): msg` 简要格式，
    与解析正则（threadtime 列式 `PID TID L TAG: msg`）不匹配 → 整流静默丢弃、
    log 面板永无输出。改 `-v threadtime`（两设备实测输出标准列式；该格式
    Android 4.x 起即为 logcat 缺省格式，旧设备同样适用）。

    返回 ``() -> Iterator[str] | None``：None = 通道不可用（adb 缺失/启动失败，
    由 LogStreamChannel 出诊断）；行流耗尽 = adb 进程退出（设备断开等）。
    """
    import logging

    logger = logging.getLogger("pd-backend.log-stream")

    def on_failure(returncode: int, _stderr_tail: str) -> None:
        logger.info("logcat 流退出（code=%s）", returncode)

    def source() -> Iterator[str] | None:
        return open_adb_logcat(serial, "threadtime", on_failure, adb_path=adb_path)

    return source


class LogStreamChannel:
    """行流 → log 级别过滤 → SSE ``log`` 发布（线程驱动；``handle_line`` 纯逻辑直测）。

    ``line_parser``（M-iOS §23.4，§11.1 兼容扩展）：行解析器注入——缺省 logcat
    threadtime 解析（real 通道）；simulator 通道传 ``parse_simctl_log_line``
    （``log show --style compact`` 行）。解析器契约同形：
    ``line -> (level, source, message) | None``（None=无级别可判，不伪造）。
    """

    def __init__(
        self,
        session: Any,
        line_source: Callable[[], Iterator[str] | None],
        *,
        pace_s: float = 0.0,
        sleep: Callable[[float], None] | None = None,
        line_parser: Callable[[str], tuple[str, str, str] | None] | None = None,
    ) -> None:
        self.session = session
        self._line_source = line_source
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._adb_lines: AdbLogcatLines | None = None
        self._pace_s = max(0.0, float(pace_s))
        self._sleep = sleep if sleep is not None else self._stop_event.wait
        self._line_parser = line_parser or parse_logcat_time_line

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"log-stream-{self.session.id}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        adb_lines = self._adb_lines
        if adb_lines is not None:
            adb_lines.stop()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def _run(self) -> None:
        source = self._line_source()
        if source is None:
            self.session.bus.publish(
                "diagnostic",
                code="log.stream.unavailable",
                severity="warn",
                message="实时日志流不可用（logcat 流未能启动）",
                detail=None,
            )
            return
        if isinstance(source, AdbLogcatLines):
            self._adb_lines = source
        try:
            if self._stop_event.is_set():
                return
            for line in source:
                if self._stop_event.is_set():
                    break
                self.handle_line(line)
                if self._pace_s > 0:
                    self._sleep(self._pace_s)
        finally:
            self._adb_lines = None
            close = getattr(source, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:  # noqa: BLE001 —— 清理失败不掩盖主流程
                    pass
            self.session.log_stream_ended()

    def handle_line(self, line: str) -> None:
        """单行解析过滤发布（纯逻辑，测试直入）。"""
        parsed = self._line_parser(line)
        if parsed is None:
            return
        level, tag, message = parsed
        selected = getattr(self.session, "log_level", None)
        if not selected or not level_passes(level, selected):
            return
        self.session.bus.publish("log", level=level, source=tag, line=message)


# ---- synthetic 确定性日志行（R33：合成日志行，各级别样本齐全）----

# 固定脚本循环：(level, source, message)；含契约 §21.7 golden 同源样本
_SYNTHETIC_SCRIPT: tuple[tuple[str, str, str], ...] = (
    ("info", "ActivityManager", "Start proc 12345:com.example.synthetic.game/u0a87 for activity"),
    ("debug", "SyntheticGame", "onResume rendered frame budget=8.3ms"),
    ("warn", "ActivityManager", "Background start not allowed"),
    ("verbose", "OpenGLRenderer", "Cache debug: 84% of 12MB"),
    ("error", "SyntheticGame", "Failed to load optional texture pack"),
    ("info", "Choreographer", "Skipped 1 frames! The application may be doing too much work"),
    ("debug", "CameraDevice", "Preview stream configured 1920x1080@60"),
    ("warn", "ThermalService", "Throttling status changed: mild"),
)


def synthetic_log_line(index: int) -> tuple[str, str, str]:
    """确定性合成日志行（按序号循环脚本；测试可复现）。"""
    return _SYNTHETIC_SCRIPT[index % len(_SYNTHETIC_SCRIPT)]


class SyntheticLogSource:
    """合成日志行源：pace 节拍产出（线程内迭代；stop 后耗尽）。

    缺省 ``sleep=time.sleep``（会话线程真实节拍）；测试注入 sleep 控制节奏。
    """

    def __init__(self, *, pace_s: float = 0.5, sleep: Callable[[float], None] | None = None,
                 stop_event: threading.Event | None = None) -> None:
        import time

        self._pace_s = max(0.01, float(pace_s))
        self._sleep = sleep if sleep is not None else time.sleep
        self._stop_event = stop_event or threading.Event()

    def lines(self) -> Iterator[str]:
        index = 0
        while not self._stop_event.is_set():
            level, source, message = synthetic_log_line(index)
            # logcat -v time 同形态（LogStreamChannel 复用同一解析器）
            yield (
                f"11-01 10:00:{index % 60:02d}.000  12345  5678 "
                f"{level[0].upper()} {source}: {message}\n"
            )
            index += 1
            self._sleep(self._pace_s)
