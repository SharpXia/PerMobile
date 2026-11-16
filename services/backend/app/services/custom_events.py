"""R52 自定义数据打点通道（logcat ``PERFEVAL_EVENT`` → 会话 markers/事件流）。

通道协议与解析**逐字复用** perfeval 既有解析器（perfeval/collectors/android/
logcat.py:57-71：``PERFEVAL_EVENT\\s+(\\{.*\\})`` 行内检索 + JSON 对象 + 行首
``logcat -v epoch`` 时戳回填 ``tsEpochMs``，事件自带 tsEpochMs 时以事件为准）——
本模块只消费 ``parse_logcat`` 的产出，不改解析器契约。

M5 分派语义（契约 §15.5 M5 注记；DEVELOPMENT_PLAN R52「setLabel/addNote 语义
对齐 R28/R29」）：

- ``type=label`` → **setLabel 等价**：场景标签**段**起点，语义持续到下一个
  label（R28；§15.4 Excel Label 列与 R32 ``byLabelSegment`` 分段均按此口径），
  走 §15 标记管线（会话内存 → SSE ``marker`` 广播 → save 落盘 markers.ndjson）；
- ``type=note`` → **addNote 等价**：点事件批注，同管线（R29）；
- ``type=metric`` → **事件流**：既有通道契约产物（perfeval parse_logcat_events
  → timeline metric 事件的会话侧等价物），经 SSE 新 type ``customEvent`` 广播
  （§8.1 兼容：消费方忽略未知 type），并按 §15.5 既有规则收进 manifest
  ``context.clockAnchors.markers``（``{name, epochMs}`` 形状不变，会话侧与
  perfeval 离线 runner 的 marker_events_from_timeline 产物同形）；
- 其他/未知 ``type``、缺 ``tsEpochMs``、字段非法：跳过并记日志（不进流、不伪造
  时戳——行首无 epoch 时戳且事件未自带 tsEpochMs 的事件按缺锚处理）。

采集窗口：record/start → record/stop（SessionManager.start_record 起线程，
stop_record/save/discard 收尾——与会话 recording 生命周期一致）；monitoring
不采集；synthetic 模式无 logcat 通道（不起线程，无诊断）。使用文档见
custom-events 指南（保留于开发仓库）。
"""

from __future__ import annotations

import logging
import math
import subprocess
import threading
from typing import Any, Callable, Iterator

from perfeval.collectors.android.logcat import parse_logcat

logger = logging.getLogger("pd-backend.custom-events")

# type=metric 事件收进 clockAnchors.markers 的上限（防打点洪泛撑爆 manifest；
# 超出部分丢弃并告警——如实截断，不静默全收）
CODE_MARKERS_CAP = 1000

# 通道不可用诊断（§8.3 diagnostic 消息；synthetic 模式无 logcat 通道，不起线程
# 不出诊断——能力缺失是合成模式的常态而非事件）
CHANNEL_UNAVAILABLE_CODE = "custom.channel.unavailable"


def adb_logcat_line_source(
    session: Any, serial: str
) -> Callable[[], Iterator[str] | None]:
    """``adb -s <serial> logcat -v epoch`` 流式行源工厂（R52 通道设备侧入口）。

    - Popen 失败（adb 不在 PATH 等 OSError）→ 工厂返回 None，``_run`` 出
      ``custom.channel.unavailable`` 诊断（docstring 约定：不可用由调用方出诊断）；
    - 进程秒退（设备不在线等非零退出）→ 行迭代器耗尽，耗尽处按 stderr 尾部
      出同一诊断——秒退与正常结束对消费方不可区分，须在源头说明；
    - 正常长流被 stop() 中断 → 生成器 close() 触发 finally 终止进程（0/None
      退出码不出诊断）。

    ``session`` 仅用于诊断广播；命令构造与 perfeval plan.py:219 collect_logcat
    同形（``logcat -v epoch``，去 ``-d`` 改为流式跟随）。

    实机验证修复（2026-11-03）：流式跟随加 ``-T 1``——perfeval 原命令是
    ``logcat -c`` 清缓冲 + ``-d`` dump（首行必新）；流式不带 ``-T`` 时首行是
    logcat 缓冲区最旧条目（真机实测 t0 锚点比会话创建早 ~18.8h，落进
    manifest context.clockAnchors.t0EpochMs 即错），``-T 1`` 使流从最新行起、
    首 epoch 行 ≈ 会话开始时刻。
    """

    def source() -> Iterator[str] | None:
        try:
            process = subprocess.Popen(
                ["adb", "-s", serial, "logcat", "-T", "1", "-v", "epoch"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, ValueError):
            return None

        def lines() -> Iterator[str]:
            try:
                if process.stdout is not None:
                    for line in process.stdout:
                        yield line
            finally:
                # 实机验证修复（2026-11-03）：会话收口经生成器 close() 触发本
                # finally，由我们 terminate 的进程 rc=-15（SIGTERM）是正常终止
                # ——docstring 既有口径「正常终止退出码不出诊断」；此前实现把
                # 自终止也当异常广播 custom.channel.unavailable（真机
                # record/stop 实测复现误报）。
                terminated_by_us = False
                if process.poll() is None:
                    terminated_by_us = True
                    process.terminate()
                    try:
                        process.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                if not terminated_by_us and process.returncode not in (0, None):
                    stderr_tail = ""
                    if process.stderr is not None:
                        try:
                            stderr_tail = (process.stderr.read() or "").strip()[-200:]
                            process.stderr.close()
                        except OSError:
                            stderr_tail = ""
                    session.bus.publish(
                        "diagnostic",
                        code=CHANNEL_UNAVAILABLE_CODE,
                        severity="warn",
                        message=(
                            f"自定义打点通道 logcat 流异常退出（code={process.returncode}）"
                            f"{'：' + stderr_tail if stderr_tail else ''}"
                        ),
                        detail=None,
                    )
                elif process.stderr is not None:
                    try:
                        process.stderr.close()
                    except OSError:
                        pass

        return lines()

    return source


def normalize_epoch_ms(value: Any) -> int | None:
    """事件自带 tsEpochMs → 整数 epoch ms；非法（bool/非数/负）→ None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return int(number)


class LogcatEventChannel:
    """logcat 行流 → PERFEVAL_EVENT 分派（线程驱动；``handle_line`` 为纯逻辑可直测）。

    ``line_source``：``() -> Iterator[str] | None``——None 表示通道不可用
    （adb 缺失/启动失败由调用方出诊断），迭代器按行产出 ``logcat -v epoch``
    输出；耗尽即线程退出（adb 进程退出，如设备断开）。
    """

    def __init__(
        self,
        session: Any,
        line_source: Callable[[], Iterator[str] | None],
    ) -> None:
        self.session = session
        self._line_source = line_source
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- 生命周期 ----

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="logcat-custom-events", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def _run(self) -> None:
        try:
            source = self._line_source()
        except Exception as exc:  # noqa: BLE001 —— adb 启动失败降级为诊断
            self.session.bus.publish(
                "diagnostic",
                code=CHANNEL_UNAVAILABLE_CODE,
                severity="warn",
                message=f"自定义打点通道启动失败：{exc}",
                detail=None,
            )
            return
        if source is None:
            # 行源工厂明确返回 None = 通道不可用（docstring 约定：由调用方出诊断）
            self.session.bus.publish(
                "diagnostic",
                code=CHANNEL_UNAVAILABLE_CODE,
                severity="warn",
                message="自定义打点通道不可用（logcat 流未能启动）",
                detail=None,
            )
            return
        try:
            for line in source:
                if self._stop_event.is_set():
                    break
                try:
                    self.handle_line(line)
                except Exception as exc:  # noqa: BLE001 —— 单行分派异常不杀通道
                    logger.warning("PERFEVAL_EVENT 行分派失败：%s", exc)
        finally:
            close = getattr(source, "close", None)
            if close is not None:
                # 显式 close 生成器：行源生成器的 finally 负责终止 adb 进程
                # （仅 break 不会立即触发生成器清理，进程会悬挂）
                try:
                    close()
                except Exception:  # noqa: BLE001 —— 清理失败不掩盖主流程
                    logger.warning("logcat 行源关闭失败", exc_info=True)
            self.session.logcat_stream_ended()

    # ---- 行处理（纯逻辑，测试直入）----

    def handle_line(self, line: str) -> None:
        """单行解析分派：复用 perfeval parse_logcat 逐行产出（不改解析器契约）。

        逐行调用 parse_logcat 与离线整段调用对 custom_events/tsEpochMs 回填的
        结果一致（行独立）；t0 锚点对齐 SCREEN_RECORD_SYNC §2「logcat 首条 epoch
        时间戳」——流式侧取首个带 epoch 前缀的行（不限 PERFEVAL_EVENT 行）；
        crash/ANR/strict 计数是离线 runner 的诊断域，会话侧 M5 只分派
        custom_events，计数结果丢弃不使用。
        """
        parsed = parse_logcat(line)
        if (
            self.session.logcat_first_line is None
            and parsed.get("first_epoch_ms") is not None
        ):
            # t0 锚点只需行首 epoch 时戳前缀；PERFEVAL_EVENT 行裁掉 JSON 载荷，
            # 避免 derive_clock_anchors 复析首行时把该事件再收一遍 markers
            self.session.logcat_first_line = (
                line.split("PERFEVAL_EVENT", 1)[0]
                if "PERFEVAL_EVENT" in line
                else line
            )
        for event in parsed["custom_events"]:
            self._dispatch(event)

    def _dispatch(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        ts_ms = normalize_epoch_ms(event.get("tsEpochMs"))
        if ts_ms is None:
            logger.warning("PERFEVAL_EVENT 缺可用 tsEpochMs，跳过：%r", event)
            return
        if event_type == "label":
            self._dispatch_marker("label", event, ts_ms, "name")
        elif event_type == "note":
            self._dispatch_marker("note", event, ts_ms, "text")
        elif event_type == "metric":
            self._dispatch_metric(event, ts_ms)
        else:
            logger.debug("PERFEVAL_EVENT 未知 type %r，忽略", event_type)

    def _dispatch_marker(self, kind: str, event: dict[str, Any], ts_ms: int, field: str) -> None:
        """label/note → §15 标记管线（会话内存 + SSE marker 广播；save 落盘 sidecar）。"""
        value = event.get(field)
        if not isinstance(value, str) or not value.strip():
            logger.warning("PERFEVAL_EVENT %s 缺非空 %s，跳过：%r", kind, field, event)
            return
        # kind 必须在元素内（§15.1 落盘字段；UI 路径由 validate_marker_body 注入，
        # 通道直入 add_marker 时在此对齐——缺失会使 save 落盘 sidecar 失败）
        element: dict[str, Any] = {"kind": kind, "tsMs": ts_ms, field: value}
        color = event.get("color")
        if kind == "label" and isinstance(color, str):
            element["color"] = color
        marker = self.session.add_marker(kind, element)
        # 契约 §15.2 SSE type ``marker``（同 UI 打标通道；消费方按 marker.kind 渲染色带/批注）
        self.session.bus.publish("marker", action="add", marker=marker)
        logger.info("PERFEVAL_EVENT %s 已入标记（%s@%s）", kind, value, ts_ms)

    def _dispatch_metric(self, event: dict[str, Any], ts_ms: int) -> None:
        """type=metric → SSE 事件流 + clockAnchors.markers 代码打点（§15.5 既有产物）。"""
        name = event.get("name")
        value = event.get("value")
        if not isinstance(name, str) or not name:
            logger.warning("PERFEVAL_EVENT metric 缺 name，跳过：%r", event)
            return
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            logger.warning("PERFEVAL_EVENT metric value 非有限数值，跳过：%r", event)
            return
        unit = event.get("unit")
        # SSE 新 type（§8.1/§11.1 兼容）：自定义数据点进事件流；metricId 未注册，
        # 故不复用 sample 信封（消费方按 name 消费，不查指标注册表）
        self.session.bus.publish(
            "customEvent", name=name, value=value, unit=str(unit or ""), tsMs=ts_ms
        )
        self.session.add_code_marker({"name": name, "epochMs": ts_ms})
