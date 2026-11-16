"""会话管理（ARCH §4.4 两态状态机 + 契约 §6）。

状态机（合法迁移全表，非法迁移 409 session_state_conflict）：
  monitoring --record/start--> recording --record/stop--> stopped
  monitoring --save/discard--> saved / discarded
  stopped    --save/discard--> saved / discarded
  recording  --save/discard--> 409（必须先 record/stop）

monitoring 不落盘（内存有界缓冲默认 5 分钟）；recording 在线追加 samples.ndjson；
save 收口时按内存缓冲回补 monitoring 期样本（sim-1 修复，2026-11-04 体检：此前
走过 recording 的会话 samples.ndjson 只有录制窗样本，与 summary/timeline 的全程
覆盖窗口静默分叉；缓冲有界，超出缓冲窗的 monitoring 期样本不可回补——如实声明）。
"""

from __future__ import annotations

import itertools
import json
import logging
import shutil
import threading
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..config import BUFFER_WINDOW_MS, Settings
from ..errors import ApiError, validation_error
from ..registry import available_metric_ids, get_metric, is_registered, mode_available
from ..providers.base import DeviceProvider
from ..providers.composite import channel_platform, provider_device_mode
from ..sse import SseBus, now_ms
from ..services.log_stream import LOG_LEVELS
from perfeval.collectors.android.samples import SampleRecord, SamplesWriter
from perfeval.collectors.android.timebase import derive_clock_anchors

from . import custom_events  # R52：代码打点上限常量（无后端依赖，无循环导入）
from . import excel_export  # §13.2 save(exportExcel)：经模块引用调用，测试可注入失败
from .marker_service import sort_markers, write_markers_sidecar
from .screen_capture import broadcast_new_recorder_diagnostics

if TYPE_CHECKING:  # 避免运行时循环导入（case_index → statistics_service）
    from .case_index import CaseIndex

logger = logging.getLogger("pd-backend.session")

VALID_STATES = ("monitoring", "recording", "stopped", "saved", "discarded")

# M6（契约 §21.5）：fpsWindow 合法枚举与 auto 缺省策略映射
FPS_WINDOW_TYPES = ("activity", "surface_view", "auto")


def validate_case_name(case_name: Any) -> str | None:
    """Validate an optional save-time case name as one safe directory component.

    Save uses the case name as the run directory name.  Keep the API's optional
    semantics (``None`` means generate a timestamped name), but reject values
    that could escape ``runs_dir`` or cause an unhandled ``TypeError``/``ValueError``.
    """
    if case_name is None:
        return None
    if not isinstance(case_name, str):
        raise validation_error("caseName 必须是字符串", field="caseName")
    name = case_name.strip()
    if not name:
        raise validation_error("caseName 必须是非空字符串", field="caseName")
    if name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise validation_error(
            "caseName 只能是单一路径名称，不能包含路径分隔符或 ..",
            field="caseName",
        )
    return name


def resolve_fps_window(window_type: str, package_name: str | None) -> str:
    """auto 缺省策略（契约 §21.5）：游戏类 → surface_view，应用类 → activity。

    synthetic 按 packageName=…synthetic.game 演示映射；real 同一启发（官方
    [F§3.1] 默认策略——游戏/直播/视频默认 SurfaceView、传统应用/Web 默认 Activity）。
    """
    if window_type != "auto":
        return window_type
    name = (package_name or "").lower()
    if name.endswith(".game") or ".game." in name:
        return "surface_view"
    return "activity"

# M4 录屏器缺省工厂（延迟 import 避免模块层循环依赖；synthetic 不构造）
def _default_screen_recorder_factory(session: "Session", run_path: Path) -> Any:
    from .screen_capture import create_session_recorder

    if session.provider_mode == "simulator":
        # M-iOS §23.3：simctl recordVideo 单段录屏（无 rollover/watchdog，
        # 与 perfeval _ScreenRecordSession 不同源，如实声明）
        from .simulator_sampler import SimulatorScreenRecorder

        return SimulatorScreenRecorder(
            session=session,
            artifact_dir=run_path / "artifacts",
            case_name=run_path.name,
        )
    if (
        session.provider_mode == "real"
        and getattr(session, "device_platform", "android") == "ios"
    ):
        # 装配流 S4（契约 §26）：真机 iOS 录屏本期不接线——抛错由 start_record
        # 捕获降级为 screen.record.unavailable 诊断（不构造 Android screencap
        # 录屏器，不静默出假录屏）
        raise RuntimeError(
            "真机 iOS 录屏本期未接线（devicectl capture screen-record 命令面存在，"
            "可用性未勘察，契约 §26）"
        )
    return create_session_recorder(session, run_path)


# M1 会话内 mutex（单会话即可，多设备并发是 M6 [R§R22]）
_session_counter = itertools.count(1)


def _next_session_id() -> str:
    return f"sess-{next(_session_counter):02d}"


class Session:
    def __init__(
        self,
        *,
        session_id: str,
        device_id: str,
        package_name: str,
        metrics: list[dict[str, Any]],
        provider_mode: str,
        created_at: int,
        runs_dir: Path,
        record_screen: bool = False,
        fps_window: dict[str, str] | None = None,
        process_name: str | None = None,
        log_level: str | None = None,
        device_channel: str | None = None,
        device_platform: str | None = None,
    ) -> None:
        self.id = session_id
        self.state = "monitoring"
        self.device_id = device_id
        self.package_name = package_name
        self.metrics: dict[str, int] = {m["metricId"]: m["intervalMs"] for m in metrics}
        self.provider_mode = provider_mode
        # 装配流 S4（契约 §26 会话链路）：设备源通道与落盘平台。缺省按 provider
        # 模式推导（单源 provider 语义与改造前逐字段一致）；manager.create 以
        # 设备行落定（聚合面真机 iOS 会话 device_platform="ios"，落盘不错标
        # android；device_channel="simulator" 走模拟器判定通道）。
        self.device_channel = device_channel or provider_mode
        self.device_platform = device_platform or (
            "ios" if provider_mode == "simulator" else "android"
        )
        self.created_at = created_at
        self.bus = SseBus(session_id)
        # ---- M6 会话选项（契约 §21.5/§21.6/§21.7；detail() 始终回显，§11.1）----
        self.fps_window: dict[str, str] = dict(fps_window or {"type": "auto"})
        self.process_name: str | None = process_name
        self.log_level: str | None = log_level
        # auto 的解析结果（synthetic frame.fps.mean tags.fpsWindow 标注用）
        self.fps_window_resolved: str = resolve_fps_window(
            self.fps_window.get("type", "auto"), package_name
        )
        # 内存有界缓冲（契约 §1/ARCH §4.4）：SampleRecord 同构 {tsMs,metricId,value,tags}
        self._records: list[dict[str, Any]] = []
        self._stats: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self.collector: Any = None  # collector_service 注入，避免循环导入
        self.runs_dir = runs_dir
        self.run_path: Path | None = None
        self.writer: SamplesWriter | None = None
        # 录制起点（epoch ms，null = 尚未进入 recording）。前端刷新后接管会话时
        # 用它还原「记录 xx:xx」计时；monitoring 期恒为 null（契约 §6.2）。
        self.record_started_at: int | None = None
        # ---- M4（契约 §16.1）：录屏开关与录屏器（record/start~stop 生命周期持有）----
        self.record_screen = bool(record_screen)
        self.recorder: Any = None  # screen_capture.SessionScreenRecorder（或测试替身）
        # ---- M4（契约 §16.1 四锚点）：采样时钟锚点（B1 语义，RealCollector 采集）----
        self.sampler_clock_anchor: dict[str, Any] | None = None
        # ---- 契约 §15：会话内存标记（save 落盘 markers.ndjson，discard 即消失）----
        self._markers: list[dict[str, Any]] = []
        self._marker_counter = itertools.count(1)
        self._marker_lock = threading.RLock()
        # ---- R52（契约 §15.5）：代码打点产物 {name, epochMs}（save 收进
        # clockAnchors.markers，不写 markers.ndjson）+ t0 锚点用首行带 epoch 时戳
        # 的 logcat 文本（SCREEN_RECORD_SYNC §2 同语义，derive_clock_anchors 复算）----
        self._code_markers: list[dict[str, Any]] = []
        self.logcat_first_line: str | None = None
        # R52 自定义打点通道（real 会话 record/start 起，stop/save/discard 收尾）
        self.custom_events_channel: Any = None
        # ---- M6 R33：实时日志流通道（logLevel 提供即开启；与采集线程同生命周期，
        # record/stop 或会话收口时终止——契约 §21.7）----
        self.log_channel: Any = None
        # ---- M7 R38：深度分析定时截图循环（timed-screenshot POST 起，
        # record/stop 或会话收口自动终止——契约 §22.3）----
        self.timed_screenshot_loop: Any = None

    # ---- 采样接收（collector 线程调用）----

    def to_wall_ms(self, ts_ms: int) -> int:
        """时戳归一到 epoch ms（契约 §1）。

        perfeval 采样器打的是相对调度起点的 elapsed ms（DeviceContext.elapsed_ms），
        数值必然小于会话创建时刻的 epoch ms，据此判定并换算；synthetic 直出的
        epoch 时戳原样透传。
        """
        ts = int(ts_ms)
        if ts < self.created_at:
            return self.created_at + ts
        return ts

    def append_record(self, record: SampleRecord) -> None:
        ts_ms = self.to_wall_ms(record.ts_ms)
        if ts_ms != record.ts_ms:
            record = replace(record, ts_ms=ts_ms)
        with self._lock:
            self._records.append(
                {
                    "tsMs": ts_ms,
                    "metricId": record.metric_id,
                    "value": record.value,
                    "tags": dict(record.tags),
                }
            )
            self._prune_locked()
            stats = self._stats.setdefault(record.metric_id, {"latest": None, "count": 0})
            stats["latest"] = record.value
            stats["count"] += 1
            if self.state == "recording" and self.writer is not None:
                try:
                    self.writer.append(record)
                except OSError:
                    # 落盘失败不阻塞实时通道；stopped/save 时以缓冲补写兜底
                    self.writer = None

    def append_null_record(self, metric_id: str, tags: dict[str, Any] | None = None) -> None:
        """进程级指标进程不存在时出 value:null 点（契约 §6.1 real 路径）。"""
        with self._lock:
            self._records.append(
                {
                    "tsMs": now_ms(),
                    "metricId": metric_id,
                    "value": None,
                    "tags": dict(tags or {}),
                }
            )
            stats = self._stats.setdefault(metric_id, {"latest": None, "count": 0})
            stats["latest"] = None
            stats["count"] += 1

    def _prune_locked(self) -> None:
        if not self._records:
            return
        cutoff = now_ms() - BUFFER_WINDOW_MS
        self._records = [r for r in self._records if r["tsMs"] >= cutoff]

    # ---- 读取 ----

    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._records)

    def stats_snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                metric_id: {"latest": s["latest"], "count": s["count"]}
                for metric_id, s in self._stats.items()
            }

    def detail(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "state": self.state,
            "deviceId": self.device_id,
            "packageName": self.package_name,
            "metrics": [
                {"metricId": metric_id, "intervalMs": interval}
                for metric_id, interval in self.metrics.items()
            ],
            "createdAt": self.created_at,
            # 契约 §16.1 M4 扩展：recordScreen 回显字段（始终返回，缺省 false）
            "recordScreen": self.record_screen,
            # 契约 §21.5/§21.6/§21.7 M6 扩展：三回显字段（始终返回，§11.1）
            "fpsWindow": dict(self.fps_window),
            "processName": self.process_name,
            "logLevel": self.log_level,
            # 契约 §6.2：录制起点（epoch ms；null = 未进入过 recording）——
            # 页面刷新后接管会话时还原记录时长，不伪造 0
            "recordStartedAt": self.record_started_at,
            "stats": self.stats_snapshot(),
        }

    # ---- 标记（契约 §15.1/§15.2：save 前仅存会话内存，save 时落盘）----

    def add_marker(self, kind: str, element: dict[str, Any]) -> dict[str, Any]:
        """插入会话内存标记（tsMs 升序、同 tsMs 按创建序）；返回含 id 的回执元素。"""
        with self._marker_lock:
            marker = {
                "id": f"mkr-{next(self._marker_counter):02d}",
                **element,
                "_order": len(self._markers),
            }
            self._markers.append(marker)
            self._markers = sort_markers(self._markers)
            return self._marker_receipt(marker)

    @staticmethod
    def _marker_receipt(marker: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in marker.items() if key != "_order"}

    def markers(self) -> list[dict[str, Any]]:
        with self._marker_lock:
            return [self._marker_receipt(marker) for marker in self._markers]

    def remove_marker(self, marker_id: str) -> bool:
        """按 id 删除；未知 id 返回 False（端点保持 204 幂等，契约 §15.2）。"""
        with self._marker_lock:
            for index, marker in enumerate(self._markers):
                if marker["id"] == marker_id:
                    self._markers.pop(index)
                    return True
            return False

    def clear_markers(self) -> None:
        with self._marker_lock:
            self._markers.clear()

    # ---- R52 代码打点（契约 §15.5：save 收进 clockAnchors.markers）----

    def add_code_marker(self, entry: dict[str, Any]) -> None:
        """追加代码通道打点 ``{name, epochMs}``（有上限防打点洪泛，超出丢弃）。"""
        with self._marker_lock:
            if len(self._code_markers) >= custom_events.CODE_MARKERS_CAP:
                return
            self._code_markers.append(
                {"name": str(entry["name"]), "epochMs": int(entry["epochMs"])}
            )

    def code_markers(self) -> list[dict[str, Any]]:
        with self._marker_lock:
            return list(self._code_markers)

    def logcat_stream_ended(self) -> None:
        """通道流结束（adb 断开/耗尽）钩子；当前仅记日志，供诊断扩展。"""
        logger.info("会话 %s 自定义打点通道流结束", self.id)

    def log_stream_ended(self) -> None:
        """R33 实时日志流结束（adb 断开/耗尽/收口）钩子；仅记日志。"""
        logger.info("会话 %s 实时日志流结束", self.id)

    def close_writer(self) -> None:
        if self.writer is not None:
            try:
                self.writer.close()
            finally:
                self.writer = None

    def set_metrics(self, enabled: dict[str, int]) -> None:
        with self._lock:
            self.metrics = dict(enabled)


class SessionManager:
    def __init__(
        self,
        provider: DeviceProvider,
        settings: Settings,
        case_index: "CaseIndex | None" = None,
        screen_recorder_factory: Callable[[Session, Path], Any] | None = None,
    ) -> None:
        self._provider = provider
        self._settings = settings
        self._case_index = case_index  # M3：save 成功自动登记 case 索引（契约 §14）
        # M4 录屏器工厂（契约 §16.1）：缺省 real 会话用 SessionScreenRecorder；
        # 测试以 fixture 工厂注入（真机路径无设备可测时验证落盘/映射/锚点逻辑）
        self._screen_recorder_factory = screen_recorder_factory or _default_screen_recorder_factory
        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()

    # ---- 查询 ----

    def get(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise ApiError("session_not_found", f"session {session_id} 不存在")
        return session

    def list(self) -> list[Session]:
        return list(self._sessions.values())

    def _active_on_device(self, device_id: str) -> Session | None:
        for session in self._sessions.values():
            if session.device_id == device_id and session.state in ("monitoring", "recording"):
                return session
        return None

    # ---- 创建（契约 §6 POST /api/sessions）----

    def create(
        self,
        *,
        device_id: str,
        package_name: str,
        metric_entries: list[dict[str, Any]] | None,
        record_screen: bool = False,
        fps_window: Any = None,
        process_name: Any = None,
        log_level: Any = None,
        launch_on_create: Any = None,
    ) -> Session:
        if not isinstance(record_screen, bool):
            raise validation_error("recordScreen 必须是布尔值", field="recordScreen")
        # 契约 §6.1：launchOnCreate 缺省 false（boolean）
        if launch_on_create is None:
            launch_on_create = False
        if not isinstance(launch_on_create, bool):
            raise validation_error(
                "launchOnCreate 必须是布尔值", field="launchOnCreate"
            )
        # ---- M6 会话选项校验（契约 §21.5/§21.6/§21.7；先于设备占用判定）----
        fps_window_value, fps_window_explicit = self._validate_fps_window(fps_window)
        log_level_value = self._validate_log_level(log_level)
        device = self._provider.get_device(device_id)
        if device is None:
            raise ApiError("device_not_found", f"device {device_id} 不存在")
        if device.get("state") != "online":
            raise ApiError(
                "device_not_online",
                f"device {device_id} 不在线（{device.get('state')}）",
                details={"state": device.get("state")},
            )
        if not isinstance(package_name, str) or not package_name:
            raise validation_error("packageName 必填且为字符串", field="packageName")
        if self._provider.mode == "synthetic" and package_name not in (
            "com.example.synthetic.game",
            "com.example.synthetic.app",
        ):
            raise validation_error(
                "synthetic 模式 packageName 限 com.example.synthetic.game|com.example.synthetic.app",
                field="packageName",
            )
        process_name_value = self._validate_process_name(
            process_name, device_id, package_name
        )
        busy = self._active_on_device(device_id)
        if busy is not None:
            raise ApiError(
                "device_busy",
                f"device {device_id} 已被会话 {busy.id} 占用",
                details={"sessionId": busy.id},
            )

        # M6 R21：available 判定 = 模式可用性 ∩ 设备 profile 交集（契约 §20.3
        # 「POST /api/sessions 落点时仍受 metric_unavailable 拦截」）。
        # 装配流 S4（§20.3 判定 mode 来源改造）：判定 mode 按设备源通道驱动——
        # ios+simulator 设备 → simulator 通道、其余 → real（聚合面
        # provider.mode 恒 "real"，不能再用装配模式判定）；单源 provider 无
        # device_mode duck 方法 → 回退 provider.mode，逐字段不变。
        channel = provider_device_mode(self._provider, device_id)
        profile = self._provider.get_profile(device_id)
        metrics: dict[str, int] = {}
        if metric_entries is None:
            # 缺省启用全部 available 指标、默认间隔（通道模式下的模式级全集）
            mode_ids = (
                self._provider.list_metric_ids()
                if channel == self._provider.mode
                else available_metric_ids(channel)
            )
            for metric_id in mode_ids:
                if not self._metric_available_on_device(metric_id, profile, channel):
                    continue
                metrics[metric_id] = get_metric(metric_id).default_interval_ms
        else:
            if not isinstance(metric_entries, list):
                raise validation_error("metrics 必须是数组", field="metrics")
            for entry in metric_entries:
                if not isinstance(entry, dict) or "metricId" not in entry:
                    raise validation_error(
                        "metrics 元素必须含 metricId", field="metrics"
                    )
                metric_id = entry["metricId"]
                if not is_registered(metric_id):
                    raise validation_error(f"未注册的 metricId: {metric_id}", field="metrics")
                spec = get_metric(metric_id)
                # 模式可用性（M-iOS：mode ∈ real|synthetic|simulator，§23.2；
                # S4：mode=设备源通道，聚合面下模拟器设备走 simulator 通道）
                available = mode_available(spec, channel)
                if not available:
                    # 契约 §3 metric_unavailable：对 available=false 指标请求启停
                    #（create 与 PATCH /metrics 同码，details.metricId）
                    raise ApiError(
                        "metric_unavailable",
                        f"指标 {metric_id} 在 {channel} 通道不可用"
                        "（available=false）",
                        details={"metricId": metric_id},
                    )
                if not self._metric_available_on_device(metric_id, profile, channel):
                    raise ApiError(
                        "metric_unavailable",
                        f"指标 {metric_id} 对设备 {device_id} 不可用"
                        "（capabilities ∩ profile 交集判定，契约 §20.3）",
                        details={"metricId": metric_id, "deviceId": device_id},
                    )
                interval = entry.get("intervalMs", spec.default_interval_ms)
                if not isinstance(interval, int) or isinstance(interval, bool) or interval <= 0:
                    raise validation_error(
                        "intervalMs 必须为正整数", field="intervalMs", metricId=metric_id
                    )
                metrics[metric_id] = interval

        # 落盘平台按设备源通道推导（S4：聚合面真机 iOS 会话 → "ios"，
        # provider_mode="real" 不再错标 android；单源逐字段不变）
        session = Session(
            session_id=_next_session_id(),
            device_id=device_id,
            package_name=package_name,
            metrics=[{"metricId": k, "intervalMs": v} for k, v in metrics.items()],
            provider_mode=self._provider.mode,
            created_at=now_ms(),
            runs_dir=self._settings.runs_dir,
            record_screen=record_screen,
            fps_window=fps_window_value,
            process_name=process_name_value,
            log_level=log_level_value,
            device_channel=channel,
            device_platform=channel_platform(channel, device.get("platform")),
        )
        with self._lock:
            self._sessions[session.id] = session
        # phase 事件（契约 §8.3）：collection 生命周期（synthetic 亦广播，供前端对齐）
        session.bus.publish(
            "phase", phase="collection", state="started", label="monitoring", detail=None
        )
        # 契约 §16.1：synthetic 模式 recordScreen=true → 会话照常创建，创建时广播
        # 诊断（能力缺失走诊断事件、不报错不填假零值，[R§8.6]）。
        # M-iOS：simulator 模式录屏由 simctl recordVideo 承载（§23.3），不再广播
        # unavailable；仅 synthetic 走该诊断分支。
        if record_screen and self._provider.mode == "synthetic":
            session.bus.publish(
                "diagnostic",
                code="screenrecord.unavailable",
                severity="info",
                message="synthetic 模式无真机录屏（real-only），会话其余能力不受影响",
                detail=None,
            )
        # 装配流 S4（契约 §26）：真机 iOS 录屏「本期不接线」（devicectl capture
        # screen-record 命令面存在、可用性未勘察，不冻结为可承诺能力）——创建时
        # 如实广播诊断，record/start 不构造 Android 录屏器（factory 层拒绝），
        # 不静默无录屏。
        if (
            record_screen
            and session.provider_mode == "real"
            and session.device_platform == "ios"
        ):
            session.bus.publish(
                "diagnostic",
                code="screenrecord.unavailable",
                severity="info",
                message="真机 iOS 录屏本期未接线（devicectl capture 命令面存在，"
                "可用性未勘察，契约 §26），会话其余能力不受影响",
                detail=None,
            )
        # 契约 §21.5（S2 窗口接线 2026-11-10，语义 delta 由 S1 落笔契约 §8/§21.5）：
        # real 模式窗口级帧率选择已接线到 FPS 采样层——resolve_fps_window 结果经
        # create_collector(fps_window_resolved=) 送达 FpsSampler（activity→gfxinfo
        # 按窗口（包/activity）过滤；surface_view→SF 图层发现/选层与 timestats
        # 图层过滤）。按实际接线结果广播：
        # - 窗口可解析且通道适用（real 且 frame.fps.mean 已启用）→ 不预广播
        #   not_applied，实际应用结果由采样层 diagnostic（fps.window.*）报告；
        # - 不可用（未启用 frame.fps.mean，窗口无处应用）→ 保留 not_applied 语义
        #   （warn 诊断，不静默忽略，[R§8.6]）。
        if fps_window_explicit and self._provider.mode == "real":
            if "frame.fps.mean" not in session.metrics:
                session.bus.publish(
                    "diagnostic",
                    code="fps_window.not_applied",
                    severity="warn",
                    message=(
                        f"fpsWindow={session.fps_window.get('type')} 已接受并回显，但会话"
                        "未启用 frame.fps.mean，窗口级帧率选择无处应用（不静默忽略）"
                    ),
                    detail=None,
                )
        # 契约 §21.7：logLevel 提供即开启实时日志流（与采集线程同生命周期）
        if log_level_value is not None:
            self._start_log_stream(session)
        # 契约 §6.1：launchOnCreate=true → 创建即执行 app_prepare（resolve_activity
        # → launch；M-iOS simulator 模式为 simctl launch，§23.1），phase 事件经
        # SSE 广播；synthetic 模式忽略该字段。
        # 实机验证修复（2026-11-03）：此前 router 未传该字段、manager 未实现——
        # launchOnCreate=true 静默无效（真机/模拟器实测踩中，fixture 无法拉起）。
        if launch_on_create and self._provider.mode in ("real", "simulator"):
            self._run_app_prepare(session)
        return session

    def _run_app_prepare(self, session: Session) -> None:
        """app_prepare（契约 §6.1）：resolve_activity → launch，phase 事件广播。

        复用 provider.launch_app（perfeval plan 层命令构造）；失败广播
        phase(failed) + diagnostic(error)、会话保持 monitoring（不自动销毁）。
        """
        session.bus.publish(
            "phase", phase="app_prepare", state="started", label="launch", detail=None
        )
        launcher = getattr(self._provider, "launch_app", None)
        if not callable(launcher):
            session.bus.publish(
                "phase",
                phase="app_prepare", state="failed", label="launch",
                detail="provider 不支持应用拉起",
            )
            return
        result = launcher(session.device_id, session.package_name)
        error = result.get("error")
        if error:
            session.bus.publish(
                "phase",
                phase="app_prepare", state="failed", label="launch", detail=error,
            )
            session.bus.publish(
                "diagnostic",
                code="app.launch.failed",
                severity="error",
                message=f"launchOnCreate 拉起失败：{error}",
                detail=None,
            )
            return
        session.bus.publish(
            "phase",
            phase="app_prepare", state="completed", label="launch",
            detail=result.get("component"),
        )

    # ---- M6 会话选项校验/判定辅助 ----

    def _metric_available_on_device(
        self, metric_id: str, profile: dict[str, Any] | None, channel: str | None = None
    ) -> bool:
        """交集判定（契约 §20.3）：无 profile 时退化为模式级判定（§5 权威）。

        S4：判定 mode 按设备源通道（``channel``，缺省回退 provider.mode——单源
        调用点语义不变；create/PATCH 显式传通道）。"""
        from ..services.capability_matrix import metric_available_on_device

        mode_ok, _reason = metric_available_on_device(
            metric_id, channel or self._provider.mode, profile
        )
        return mode_ok

    @staticmethod
    def _validate_fps_window(fps_window: Any) -> tuple[dict[str, str], bool]:
        """契约 §21.5：fpsWindow 为 {type: activity|surface_view|auto}，缺省 auto。

        返回 (回显值, 是否显式提供)；非法 type → 400 validation_error
        （details.field="fpsWindow.type"）。
        """
        if fps_window is None:
            return {"type": "auto"}, False
        if not isinstance(fps_window, dict):
            raise validation_error(
                "fpsWindow 必须是对象 {type}", field="fpsWindow.type"
            )
        window_type = fps_window.get("type")
        if window_type not in FPS_WINDOW_TYPES:
            raise validation_error(
                f"fpsWindow.type 必须是 {'|'.join(FPS_WINDOW_TYPES)} 之一",
                field="fpsWindow.type",
            )
        return {"type": window_type}, True

    @staticmethod
    def _validate_log_level(log_level: Any) -> str | None:
        """契约 §21.7：logLevel ∈ verbose|debug|info|warn|error；缺省（未提供）
        不发 log 消息；非法值 → 400 validation_error（details.field="logLevel"）。"""
        if log_level is None:
            return None
        if log_level not in LOG_LEVELS:
            raise validation_error(
                f"logLevel 必须是 {'|'.join(LOG_LEVELS)} 之一",
                field="logLevel",
            )
        return log_level

    def _validate_process_name(
        self, process_name: Any, device_id: str, package_name: str
    ) -> str | None:
        """契约 §21.6：processName 必须属于该 app 的 processes[].name；主进程名
        等价缺省（None）。非法 → 400 validation_error（details.field="processName"）。"""
        if process_name is None:
            return None
        if not isinstance(process_name, str) or not process_name.strip():
            raise validation_error(
                "processName 必须是非空字符串", field="processName"
            )
        body = self._provider.list_apps(device_id)
        known: set[str] = set()
        for app in (body or {}).get("apps", []):
            if app.get("packageName") == package_name:
                known = {
                    proc.get("name") for proc in app.get("processes", []) if proc.get("name")
                }
                break
        if process_name not in known:
            raise validation_error(
                f"processName {process_name!r} 不属于 {package_name} 的 processes[]"
                "（校验来源 GET /api/devices/{deviceId}/apps，契约 §21.6）",
                field="processName",
            )
        return process_name

    # ---- M6 R33 日志流生命周期（与采集线程同生命周期，契约 §21.7）----

    def _start_log_stream(self, session: Session) -> None:
        from .log_stream import (
            LogStreamChannel,
            SyntheticLogSource,
            adb_logcat_time_source,
        )

        if self._provider.mode == "real":
            if getattr(session, "device_platform", "android") == "ios":
                # 装配流 S4（契约 §26）：真机 iOS 日志流本期不接线（无实时流式
                # 命令证据；xctrace Logging 模板为批式形态且导出未勘察）——
                # logcat 行源对 iOS 设备无意义，如实广播诊断不静默。
                session.bus.publish(
                    "diagnostic",
                    code="logstream.unavailable",
                    severity="info",
                    message="真机 iOS 实时日志流本期未接线（契约 §26），"
                    "会话其余能力不受影响",
                    detail=None,
                )
                return
            source = adb_logcat_time_source(session.device_id)
            channel = LogStreamChannel(session, source)
        elif self._provider.mode == "simulator":
            # M-iOS §23.4：simctl log show 轮询行源（compact 行级别解析）
            from .simulator_sampler import parse_simctl_log_line, simctl_log_source

            channel = LogStreamChannel(
                session, simctl_log_source(session.device_id),
                line_parser=parse_simctl_log_line,
            )
        else:
            stop_event = threading.Event()
            source_factory = SyntheticLogSource(
                pace_s=self._settings.synthetic_log_pace_s, stop_event=stop_event
            )
            channel = LogStreamChannel(
                session, source_factory.lines, sleep=lambda seconds: None
            )
            channel._stop_event = stop_event  # 同一停止事件（生成器循环判停）
        channel.start()
        session.log_channel = channel

    def _stop_log_stream(self, session: Session) -> None:
        """日志流收尾（幂等）：monitoring 未开启时为 None，直接跳过。"""
        channel = session.log_channel
        if channel is None:
            return
        session.log_channel = None
        try:
            channel.stop()
        except Exception as exc:  # noqa: BLE001 —— 收尾失败只记日志
            logger.warning("会话 %s 日志流收尾失败：%s", session.id, exc)

    # ---- M7 R38 定时截图循环生命周期（契约 §22.3）----

    def start_timed_screenshot(
        self,
        session: Session,
        interval_ms: int,
        *,
        capture: Callable[[], bytes] | None = None,
    ) -> Any:
        """启动（或以新 intervalMs 幂等重启）定时截图循环（契约 §22.3）。

        重复 POST 先停旧循环再起新循环（不叠加多线程）；状态约束（仅 recording）
        由 router 校验后进入。``capture`` 注入面测试可替换。首帧同步截取（设备
        故障当场以契约错误码冒泡，如 artifact_pull_failed → 502），后续帧异步。
        """
        self._stop_timed_screenshot(session)
        from .timed_screenshot import TimedScreenshotLoop

        run_path = session.run_path
        if run_path is None:
            raise ApiError(
                "session_state_conflict",
                f"session {session.id} 无 run 目录可落定时截图",
                details={"state": session.state},
            )
        loop = TimedScreenshotLoop(
            session,
            interval_ms=interval_ms,
            artifact_dir=run_path / "artifacts",
            capture=capture,
        )
        loop.capture_first()  # 首帧同步：失败按契约错误码冒泡（502 等）
        loop.start()
        session.timed_screenshot_loop = loop
        return loop

    def _stop_timed_screenshot(self, session: Session) -> None:
        """定时截图循环收尾（幂等）：未启动时为 None，直接跳过。"""
        loop = getattr(session, "timed_screenshot_loop", None)
        if loop is None:
            return
        session.timed_screenshot_loop = None
        try:
            loop.stop()
        except Exception as exc:  # noqa: BLE001 —— 收尾失败只记日志
            logger.warning("会话 %s 定时截图循环收尾失败：%s", session.id, exc)

    # ---- 状态机迁移 ----

    def start_record(self, session: Session) -> Session:
        if session.state != "monitoring":
            raise ApiError(
                "session_state_conflict",
                f"session {session.id} is not monitoring",
                details={"state": session.state},
            )
        case_name = datetime.now().strftime("PD_%Y%m%d_%H%M%S")
        run_path = self._settings.runs_dir / case_name
        # 多设备矩阵实测发现（2026-11-06，Pixel_9/Pixel_9_2 双模拟器并发录制）：
        # 临时目录名只到秒级，两台设备同秒 record/start 会 mkdir(exist_ok=True)
        # 共享同一临时目录——双方 writer 追加同一 samples.ndjson（跨设备样本
        # 串写：一方 run 混入另一方样本），且先 save 的一方 rename 目录后，
        # 另一方 session.run_path 失效，save 时新建空目录 → samples.ndjson
        # 整体丢失。修复：临时目录重名加序号（与 _write_run_dir 收口逻辑同
        # 形状），保证并发录制会话各自独立目录。
        # 复核补强（2026-11-06）：record/start 端点为同步 def 走线程池可真并发，
        # 先 exists 后 mkdir 的 check-then-act 存在竞态窗口——改为以
        # mkdir(exist_ok=False) 为原子占位，FileExistsError 顺延序号重试；
        # 两请求同过 exists 检查时后到者拿到 FileExistsError 落 _2，不再可能
        # 共享目录或以未捕获异常 500。
        suffix = 2
        while True:
            try:
                run_path.mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                run_path = self._settings.runs_dir / f"{case_name}_{suffix}"
                suffix += 1
        session.run_path = run_path
        session.writer = SamplesWriter(run_path / "samples.ndjson")
        session.record_started_at = now_ms()
        # 契约 §16.1：real/simulator + recordScreen → record/start 起会话录屏器
        #（real=perfeval screenrecord；simulator=simctl recordVideo §23.3；
        # probe 失败/启动失败走诊断降级，不阻断 recording 状态机）
        if session.record_screen and self._provider.mode in ("real", "simulator"):
            try:
                recorder = self._screen_recorder_factory(session, run_path)
                recorder.start()
                session.recorder = recorder
            except Exception as exc:  # noqa: BLE001 —— 录屏故障降级为诊断，不影响采集
                logger.warning("会话 %s 录屏启动失败：%s", session.id, exc)
                session.recorder = None
                session.bus.publish(
                    "diagnostic",
                    code="screen.record.unavailable",
                    severity="warn",
                    message=f"录屏启动失败：{exc}",
                    detail=None,
                )
            else:
                broadcast_new_recorder_diagnostics(session, recorder)
        session.state = "recording"
        # R52（契约 §15.5）：real 会话 record/start 起 logcat PERFEVAL_EVENT 打点
        # 通道（label/note → §15 标记管线，metric → SSE customEvent +
        # clockAnchors.markers）；启动/流故障降级为诊断，不阻断 recording 状态机
        #（同 §16.1 录屏开关哲学）。synthetic 模式无 logcat 通道，不起线程。
        self._start_custom_events_channel(session)
        session.bus.publish("session.state", state="recording")
        return session

    def _start_custom_events_channel(self, session: Session) -> None:
        if self._provider.mode != "real":
            return
        if getattr(session, "device_platform", "android") == "ios":
            # 装配流 S4（契约 §26）：logcat PERFEVAL_EVENT 打点通道为 Android
            # 链路，真机 iOS 无 logcat——record/start 起如实广播诊断（同 §16.1
            # 能力缺失走诊断哲学），不静默、不起无效线程。
            session.bus.publish(
                "diagnostic",
                code=custom_events.CHANNEL_UNAVAILABLE_CODE,
                severity="info",
                message="真机 iOS 自定义打点通道本期未接线（logcat 为 Android 链路）",
                detail=None,
            )
            return
        try:
            channel = custom_events.LogcatEventChannel(
                session,
                custom_events.adb_logcat_line_source(session, session.device_id),
            )
            channel.start()
            session.custom_events_channel = channel
        except Exception as exc:  # noqa: BLE001 —— 通道构造失败降级为诊断
            logger.warning("会话 %s 打点通道启动失败：%s", session.id, exc)
            session.bus.publish(
                "diagnostic",
                code=custom_events.CHANNEL_UNAVAILABLE_CODE,
                severity="warn",
                message=f"自定义打点通道启动失败：{exc}",
                detail=None,
            )

    def _stop_custom_events_channel(self, session: Session) -> None:
        """通道收尾（幂等）：monitoring 保存/从未起通道时为 None，直接跳过。"""
        channel = session.custom_events_channel
        if channel is None:
            return
        session.custom_events_channel = None
        try:
            channel.stop()
        except Exception as exc:  # noqa: BLE001 —— 收尾失败只记日志
            logger.warning("会话 %s 打点通道收尾失败：%s", session.id, exc)

    def stop_record(self, session: Session) -> Session:
        if session.state == "stopped":
            return session  # 重复 stop 幂等（契约 §1）
        if session.state != "recording":
            raise ApiError(
                "session_state_conflict",
                f"session {session.id} is not recording",
                details={"state": session.state},
            )
        self._stop_collector(session)
        self._stop_recorder(session)
        self._stop_custom_events_channel(session)  # R52 采集窗口收口（record/stop）
        self._stop_log_stream(session)  # R33 日志流与采集线程同生命周期（契约 §21.7）
        self._stop_timed_screenshot(session)  # M7 R38 定时截图随 record/stop 终止（§22.3）
        session.close_writer()
        session.state = "stopped"
        session.bus.publish(
            "phase", phase="collection", state="completed", label="monitoring", detail=None
        )
        session.bus.publish("session.state", state="stopped")
        return session

    def _stop_recorder(self, session: Session) -> None:
        """record/stop 收尾：pkill -INT 封装 + pull + <1KB 判废（契约 §16.1）。"""
        recorder = session.recorder
        if recorder is None:
            return
        try:
            recorder.stop()
        except Exception as exc:  # noqa: BLE001 —— pull/收尾故障降级为诊断
            logger.warning("会话 %s 录屏收尾失败：%s", session.id, exc)
        finally:
            broadcast_new_recorder_diagnostics(session, recorder)

    def save(
        self, session: Session, case_name: str | None, export_excel: bool = False
    ) -> dict[str, Any]:
        case_name = validate_case_name(case_name)
        if session.state == "recording":
            raise ApiError(
                "session_state_conflict",
                f"session {session.id} is recording; record/stop first",
                details={"state": session.state},
            )
        if session.state == "saved":
            raise ApiError(
                "session_state_conflict",
                f"session {session.id} already saved",
                details={"state": session.state},
            )
        if session.state not in ("monitoring", "stopped"):
            raise ApiError(
                "session_state_conflict",
                f"session {session.id} cannot save from {session.state}",
                details={"state": session.state},
            )
        was_monitoring = session.state == "monitoring"
        self._stop_collector(session)
        self._stop_custom_events_channel(session)  # R52：stopped 已收口，幂等
        self._stop_log_stream(session)  # R33：stopped 已收口，幂等（会话收口）
        self._stop_timed_screenshot(session)  # M7 R38：recording 不可 save，防御幂等
        session.close_writer()

        run_path = self._write_run_dir(session, case_name)
        session.run_path = run_path
        session.state = "saved"
        if was_monitoring:
            session.bus.publish(
                "phase", phase="collection", state="completed", label="monitoring", detail=None
            )
        session.bus.publish("session.state", state="saved")
        run_id = f"run-{run_path.name}"
        result: dict[str, Any] = {
            "sessionId": session.id,
            "runId": run_id,
            "state": session.state,
            "runPath": str(run_path.resolve()),
        }
        self._register_case_index(run_id)
        if export_excel:
            # 契约 §13.2：xlsx 是派生产物，导出失败不使 save 失败（run 目录已落盘即
            # 成功；失败经 exportError 如实上报，可经 §13.1 重试）。
            try:
                export_path = excel_export.write_run_export_xlsx(self._settings.runs_dir, run_id)
                result["exportPath"] = str(export_path.resolve())
            except Exception as exc:  # noqa: BLE001 —— 任何导出异常都降级为 exportError
                result["exportError"] = f"excel 导出失败：{exc}"
        return result

    def _register_case_index(self, run_id: str) -> None:
        """save 成功自动登记 case 索引（契约 §14：登记失败不使 save 失败，
        rebuild 兜底——与 §13.2 导出失败同哲学）。"""
        if self._case_index is None:
            return
        try:
            from .case_index import register_run

            register_run(self._settings.runs_dir, self._case_index, run_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("case 索引自动登记失败（run %s）：%s；可经 rebuild 修复", run_id, exc)

    def discard(self, session: Session) -> None:
        if session.state == "discarded":
            return  # 幂等
        if session.state not in ("monitoring", "stopped"):
            raise ApiError(
                "session_state_conflict",
                f"session {session.id} cannot discard from {session.state}",
                details={"state": session.state},
            )
        was_monitoring = session.state == "monitoring"
        self._stop_collector(session)
        self._stop_custom_events_channel(session)  # R52：discard 一并收口通道
        self._stop_log_stream(session)  # R33：discard 一并收口日志流
        self._stop_timed_screenshot(session)  # M7 R38：discard 一并收口定时截图循环
        # 录屏器已在 record/stop 收尾（discard 仅可从 monitoring|stopped 进入），
        # 此处仅解除引用避免重复 pull；watchdog 为 daemon 线程且生命周期已停
        session.recorder = None
        session.close_writer()
        if session.run_path is not None:
            shutil.rmtree(session.run_path, ignore_errors=True)
            session.run_path = None
        session.state = "discarded"
        if was_monitoring:
            session.bus.publish(
                "phase", phase="collection", state="completed", label="monitoring", detail=None
            )
        session.bus.publish("session.state", state="discarded")

    def delete(self, session: Session) -> None:
        if session.state not in ("stopped", "discarded"):
            raise ApiError(
                "session_state_conflict",
                f"session {session.id} only deletable in stopped|discarded",
                details={"state": session.state},
            )
        self._stop_collector(session)
        self._stop_log_stream(session)  # R33：删除兜底收口（stopped 已收，幂等）
        self._stop_timed_screenshot(session)  # M7 R38：删除兜底收口（幂等）
        self._sessions.pop(session.id, None)

    def close_all(self) -> None:
        """后端进程退出兜底：停掉全部会话的采集线程与子进程（不留孤儿采集）。

        页面刷新不属于本路径——会话继续运行并可被前端重新接管（见 §6.2）；
        这里只处理后端进程自身退出（SIGTERM/正常关停）：逐会话尽力收尾，单个
        会话失败只记日志，不阻断其余会话与进程退出。
        """
        for session in list(self._sessions.values()):
            for stop in (
                self._stop_timed_screenshot,
                self._stop_custom_events_channel,
                self._stop_log_stream,
                self._stop_recorder,
                self._stop_collector,
            ):
                try:
                    stop(session)
                except Exception as exc:  # noqa: BLE001 —— 退出路径不抛异常
                    logger.warning("会话 %s 退出收尾失败：%s", session.id, exc)
            try:
                session.close_writer()
            except Exception as exc:  # noqa: BLE001
                logger.warning("会话 %s writer 关闭失败：%s", session.id, exc)

    def _stop_collector(self, session: Session) -> None:
        collector = session.collector
        if collector is not None:
            try:
                collector.stop()
            finally:
                session.collector = None

    # ---- 落盘（ARCH §4.5）----

    def _write_run_dir(self, session: Session, case_name: str | None) -> Path:
        final_name = case_name or (
            session.run_path.name if session.run_path else
            datetime.now().strftime("PD_%Y%m%d_%H%M%S")
        )
        if session.run_path is not None and session.run_path.name == final_name:
            run_path = session.run_path
        else:
            run_path = self._settings.runs_dir / final_name
            if session.run_path is not None and session.run_path.exists():
                # recording 阶段临时目录改名收口；重名时加序号
                target = run_path
                suffix = 2
                while target.exists():
                    target = self._settings.runs_dir / f"{final_name}_{suffix}"
                    suffix += 1
                session.run_path.rename(target)
                run_path = target
            elif session.run_path is None or not session.run_path.exists():
                # monitoring 直存没有 start_record 预先占用临时目录；显式
                # caseName 与已有 run 冲突时必须分配新目录，避免把历史事实源
                # 静默覆盖。mkdir(exist_ok=False) 同时把占位变成原子操作。
                suffix = 2
                while True:
                    try:
                        run_path.mkdir(parents=True, exist_ok=False)
                        break
                    except FileExistsError:
                        run_path = self._settings.runs_dir / f"{final_name}_{suffix}"
                        suffix += 1
        run_path.mkdir(parents=True, exist_ok=True)

        # ---- 契约 §16.1：perfeval 采集命名 screen*.mp4 → 契约命名 video*.mp4
        #（save 时统一落位；<1KB 判废件已由 runner pull 删除，不参与映射）
        self._rename_video_artifacts(run_path)

        records = session.records()
        samples_path = run_path / "samples.ndjson"
        if session.state == "monitoring":
            # 监控态直接收口：把内存缓冲整体写盘（writer 未开）
            writer = SamplesWriter(samples_path)
            try:
                writer.append([_to_record(r) for r in records if r["value"] is not None])
            finally:
                writer.close()
        elif session.state == "stopped" and samples_path.exists():
            # sim-1 修复（2026-11-06）：走过 recording 的会话 save 时回补缓冲——
            # recording 期在线追加只落了录制窗样本，monitoring 期样本仅在内存
            # 缓冲（有界，超窗不可回补）；summary/timeline 一直取全量缓冲，三个
            # 持久化产物的覆盖窗口曾静默分叉（统计侧按 samples.ndjson 计数会与
            # summary 口径不一致）。按 (metricId, tsMs, value) 去重合并：盘上已有
            # （含缓冲已裁剪的录制期样本）保留，缓冲新增（monitoring 期）按
            # 时戳序补写，全程单一覆盖窗口。
            self._backfill_samples_ndjson(samples_path, records)

        # summary.json：perfeval.summary.v1 原样（有数据才算，空数据不伪造）
        from perfeval.collectors.android.samples import parse_samples_events
        from perfeval.core.summary import aggregate_summary
        from perfeval.core.timeline import write_timeline

        run_id = f"run-{run_path.name}"
        numeric = [r for r in records if isinstance(r["value"], (int, float))]
        # save 时应用版本探测（S2 version-metadata 接线 2026-11-10）：provider 无
        # 探测面（synthetic/simulator）或探测失败 → None（不伪造，manifest 字段保留）
        app_version = self._probe_app_version(session)
        # 会话级功耗互核（S2 power-energy-crosscheck，契约 summary 顶层可选块由
        # S1 落笔）：主链 Σ电流积分 vs 降级链 charge_full×|Δcapacity|，输入取
        # 全量缓冲 energy 记录 tags（power_sampler capacityPct/chargeFullMah 留痕）
        power_crosscheck = _power_crosscheck(records)
        summary_context: dict[str, Any] = {}
        if power_crosscheck is not None:
            summary_context["powerCrosscheck"] = power_crosscheck
        if app_version:
            # 对比引擎身份字段（perfeval compare engine._identity_from_summary 读
            # summary["app"]["versionName"/"versionCode"]）——context 合并进
            # summary 顶层（engine.aggregate_summary context 形参，perfeval 零改动）
            summary_context["app"] = {
                "packageName": session.package_name,
                **app_version,
            }
        if numeric:
            events = parse_samples_events(
                [_to_record(r) for r in numeric],
                run_id=run_id,
                # 装配流 S4：落盘平台按设备源通道落定的 session.device_platform
                # （真机 iOS 会话 → "ios"，聚合面 provider_mode="real" 不错标
                # android；单源 simulator/android 语义不变）
                platform=session.device_platform,
                ts_origin_ms=session.created_at,
            )
            timeline_path = run_path / "timeline.ndjson"
            with timeline_path.open("w", encoding="utf-8") as sink:
                write_timeline(events, sink)
            summary_body = aggregate_summary(
                run_id, timeline_events=events, context=summary_context or None
            )
            (run_path / "summary.json").write_text(
                json.dumps(summary_body, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            # Web run 离线 HTML 报告（S2 web-runs-no-offline-html-report，契约 §10
            # 新增可选文件 report.html 由 S1 落笔）：summary.json 即 perfeval.
            # summary.v1 原样，与 CLI 链同一消费面（write_summary_report，summary_
            # report.py 零改动）；无数据 run 不产 report.html（同「不伪造」原则）。
            # 生成失败降级为日志（save 不因此失败——与 §13.2 导出失败同哲学）。
            try:
                from perfeval.core.report.summary_report import write_summary_report

                write_summary_report(summary_body, run_path / "report.html")
            except Exception as exc:  # noqa: BLE001 —— 报告生成失败不阻塞 save
                logger.warning("report.html 生成失败（run %s）：%s", run_id, exc)

        manifest = {
            "schemaVersion": "perfeval.manifest.v1",
            "runId": run_id,
            "caseName": run_path.name,
            "sessionId": session.id,
            "device": session.device_id,
            "packageName": session.package_name,
            # S2 version-metadata（2026-11-10）：save 时应用版本快照；探测面缺失/
            # 探测失败 → None（字段保留，不伪造——契约 manifest 字段集扩展由 S1 落笔）
            "appVersion": app_version,
            "metrics": [
                {"metricId": k, "intervalMs": v} for k, v in session.metrics.items()
            ],
            "createdAt": session.created_at,
            "savedAt": now_ms(),
            # 契约 §16.1 四锚点：derive_clock_anchors（timebase.py:79-128）产出原样
            # 落盘；无任何锚点 → None（形状保留，[R§2.9] 回放可复算前提）
            "context": {"clockAnchors": self._derive_clock_anchors(session)},
        }
        (run_path / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        (run_path / "artifacts").mkdir(exist_ok=True)
        # 契约 §15.1：save 时统一写 markers.ndjson（无标记不写文件）
        write_markers_sidecar(run_path, session.markers())
        return run_path

    @staticmethod
    def _backfill_samples_ndjson(samples_path: Path, records: list[dict[str, Any]]) -> None:
        """sim-1 修复：samples.ndjson 按内存缓冲回补（monitoring 期样本）。

        盘上已有行（recording 期在线追加，含缓冲已裁剪的录制期样本）保留；缓冲中
        不在盘上的数值样本按 (metricId, tsMs, value) 去重后并入，整体按 tsMs 升序
        重写——与 summary/timeline 的全量缓冲口径对齐。内存缓冲有界（默认 5 分钟），
        超出缓冲窗的 monitoring 期样本已不可回补（模块 docstring 如实声明）。
        重写失败降级为保留原文件（落盘失败不阻塞 save）。
        """
        from perfeval.collectors.android.samples import read_samples

        try:
            existing = read_samples(samples_path)
            seen = {(record.metric_id, record.ts_ms, record.value) for record in existing}
            backfill: list[SampleRecord] = []
            for row in records:
                value = row["value"]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                record = _to_record(row)
                if (record.metric_id, record.ts_ms, record.value) not in seen:
                    backfill.append(record)
            merged = sorted(existing + backfill, key=lambda record: record.ts_ms)
            samples_path.write_text(
                "".join(json.dumps(record.to_dict(), sort_keys=True) + "\n" for record in merged),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("samples.ndjson 回补失败（%s）：%s", samples_path, exc)

    # ---- M4（契约 §16.1）----

    @staticmethod
    def _rename_video_artifacts(run_path: Path) -> None:
        """perfeval 采集命名 → 契约命名（save 时统一落位）。

        ``screen.mp4``→``video.mp4``、``screen-segNNN.mp4``→``video-segNNN.mp4``；
        仅前缀 screen→video 一次替换。改名失败只记日志（save 不因此失败，工件
        以原名保留——索引按契约命名匹配不到 video 时回放自然降级）。
        """
        artifacts_dir = run_path / "artifacts"
        if not artifacts_dir.is_dir():
            return
        for path in sorted(artifacts_dir.glob("screen*.mp4")):
            if not path.is_file():
                continue
            target = path.with_name(path.name.replace("screen", "video", 1))
            try:
                path.rename(target)
            except OSError as exc:
                logger.warning("录屏产物改名失败（%s）：%s", path.name, exc)

    @staticmethod
    def _derive_clock_anchors(session: "Session") -> dict[str, Any] | None:
        """汇总会话可用锚点（契约 §16.1，形状由 perfeval derive_clock_anchors 决定）。

        - video：SessionScreenRecorder 产出的 startEpochMs/firstFrameEpochMs/
          method/timeLimitS/segments/clockSource（全部段判废时为 None）；
        - samplers：RealCollector 在 SamplingScheduler.start() 同刻读的设备时钟
          （B1 语义，经 _normalize_samplers_anchor 映射为 startEpochMs/source/
          boottimeNs/method）；
        - t0/markers 段（R52，M5 起可选）：record 窗口内自定义打点通道
          （logcat PERFEVAL_EVENT）采集到的首行带 epoch 时戳文本 → t0，通道
          ``type=metric`` 打点 → markers（``{name, epochMs}``，与 perfeval 离线
          runner 的 marker_events_from_timeline 产物同形）；通道未启用/无打点时
          与此前一致——derive_clock_anchors 自身省略缺数据段，不伪造。
        """
        recorder = session.recorder
        video_anchors = recorder.video_anchors if recorder is not None else None
        return derive_clock_anchors(
            logcat_text=session.logcat_first_line,
            video_anchors=video_anchors,
            markers=session.code_markers(),
            samplers_anchor=session.sampler_clock_anchor,
        )

    def _probe_app_version(self, session: Session) -> dict[str, Any] | None:
        """save 时应用版本探测（S2 version-metadata 接线 2026-11-10）。

        provider 有 ``app_version(device_id, package_name)`` 只读探测面（real，
        duck-typing 同 launch_app 注入哲学，不扩 base 抽象）→ 调用；探测面缺失
        （synthetic/simulator）或失败/抛错 → None（不伪造，manifest.appVersion
        字段保留为 null——save 时点快照，仅用于 manifest 与 summary 身份字段）。
        """
        probe = getattr(self._provider, "app_version", None)
        if not callable(probe):
            return None
        try:
            result = probe(session.device_id, session.package_name)
        except Exception as exc:  # noqa: BLE001 —— 探测失败降级为 null（不猜）
            logger.warning("会话 %s 应用版本探测失败：%s", session.id, exc)
            return None
        if not isinstance(result, dict):
            return None
        version_name = result.get("versionName")
        version_code = result.get("versionCode")
        if version_name is None and version_code is None:
            return None
        return {"versionName": version_name, "versionCode": version_code}


def _power_crosscheck(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """会话级功耗双链互核（S2 power-energy-crosscheck，2026-11-10）。

    save 时以全量缓冲 energy 记录（power.energy.mah）为输入：

    - 主链：tags.source == "current_integral"（电流积分，Σ mean(current)×Δt/3600，
      即 Σ(current×V×Δt) 的 mAh 口径——电流为有功项，电压仅上下文）累计
      primaryMah；
    - 降级链：tags.source == "level_slope"（charge_full×|Δcapacity| 差分，放电段
      口径——方向信息由 tags.currentRawMa 承载）累计 degradedMah；
    - 双链齐备 → basis="dual_chain" + deviationPct（|primary−degraded|/degraded）；
      仅主链 → basis="primary_only"（degradedMah/deviationPct=null，不伪造降级
      值）；仅降级链 → basis="degraded_only"。

    输入由 power_sampler 能量记录 tags（capacityPct/chargeFullMah 留痕）在会话内
    齐备；单链/无 energy 记录不产块（None）。验收注：SM_F9560 电池 sysfs 被
    SELinux 屏蔽（设备池约束），live 真值复验挂起——合成 fixture 验收
    （test_run_save_extras.py）。
    """
    primary: list[float] = []
    degraded: list[float] = []
    for row in records:
        if row["metricId"] != "power.energy.mah":
            continue
        value = row.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        source = (row.get("tags") or {}).get("source")
        if source == "current_integral":
            primary.append(float(value))
        elif source == "level_slope":
            degraded.append(float(value))
    primary_mah = round(sum(primary), 4) if primary else None
    degraded_mah = round(sum(degraded), 4) if degraded else None
    if primary_mah is None and degraded_mah is None:
        return None
    if primary_mah is not None and degraded_mah is not None:
        basis = "dual_chain"
        deviation_pct = (
            round(100.0 * abs(primary_mah - degraded_mah) / degraded_mah, 2)
            if degraded_mah > 0
            else None
        )
        note = (
            "会话级互核：主链=电流积分（Σ mean(current)×Δt/3600，mAh 口径）；"
            "降级链=charge_full×|Δcapacity| 差分累计（放电段口径，方向信息见"
            " tags.currentRawMa）；deviationPct=|主链−降级链|/降级链。电池 sysfs"
            " 真值 live 复验挂起（SM_F9560 被 SELinux 屏蔽，设备池约束）"
        )
    elif primary_mah is not None:
        basis = "primary_only"
        deviation_pct = None
        note = (
            "降级链输入不可用（capacity/charge_full 未出现在任何能量记录），"
            "互核仅主链——不伪造降级值"
        )
    else:
        basis = "degraded_only"
        deviation_pct = None
        note = (
            "主链输入不可用（current_now 缺失，无电流积分能量记录），"
            "互核仅降级链——不伪造主链值"
        )
    return {
        "primaryMah": primary_mah,
        "degradedMah": degraded_mah,
        "deviationPct": deviation_pct,
        "basis": basis,
        "note": note,
    }


def _to_record(row: dict[str, Any]) -> SampleRecord:
    return SampleRecord(
        metric_id=row["metricId"],
        value=float(row["value"]) if row["value"] is not None else 0.0,
        ts_ms=int(row["tsMs"]),
        tags=dict(row.get("tags") or {}),
    )
