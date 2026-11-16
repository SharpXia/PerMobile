"""真机 iOS 采集执行（R24 契约 §26.4/§26.5）：窗口批采样 RealIOSCollector。

真机 iOS 无周期轮询命令面——每个采样窗执行一轮完整链路（§26.4 窗口批模型）::

    devicectl 预热/lockState（xctrace.prewarm_device）
      → 进程名解析（bundle → 主进程名；xctrace 禁 PID attach，exit 21 实测）
      → xctrace record --attach <进程名> --time-limit Ns --no-prompt
      → export --toc/--xpath → 解析 activity-monitor-process-live
      → 窗口内批出点（~1s 粒度，cpu.proc.xctrace.pct + memory.footprint.mb）

出点语义（§26.4/§26.5，与 Android adb 周期轮询不同源，如实声明）：

- 批出点：窗口结束才出该窗点列；行 ts = 窗口起始宿主时钟 + 表内 Start Time
  偏移；窗口锚点 = record 退出时刻回推 time-limit（host_epoch）——首窗预热
  时延不均摊进数据点；
- tags 恒带 ``{"basis": "xctrace"}``（§26.3：真机 Instruments 口径标注）；
- 目标进程消失（探测成功但未安装/未运行；或窗口无数据行）→
  ``diagnostic(app.process.not_found)``（锁存防刷屏）+ 双指标 value:null 点
  （§6.1 real 路径同语义）——null 点仅属**权威负读数**（探测命令成功为前提）；
- 窗前探测（在线检查/apps/processes）命令失败重试耗尽 →
  ``diagnostic(xctrace_unavailable, details.reason="devicectl_probe_failed")``，
  该窗不出点不造数（无 null 点——探测失败≠进程不存在，§26.5 无数据语义）；
- record/export 失败 → ``diagnostic(xctrace.window.failed)``（severity error，
  details 带 reason/exit code），该窗不出点不造数（[R§8.6]）；
- 预热/lockState 重试耗尽或 xcrun 缺失 → ``diagnostic(xctrace_unavailable)``
  （severity error，details.reason 与 §3 错误体同枚举）；
- 会话中途失联（tunnelState 离线）→ ``diagnostic(device.tunnel.offline)``；
- .trace 即删（run 目录不落 .trace 工件，§26.4）。

并发约束（§26.4，机制如实声明）：xcrun 家族命令（xctrace 与 devicectl）经
进程级可重入锁 ``devicectl.XCRUN_FAMILY_LOCK`` 全局串行——采集窗全程持锁
（``xctrace.run_sample_window``），本采集器的窗内探测（在线检查/进程解析）与
聚合面枚举同锁排队，不与 record/export 并发（并发 xcrun 命令会失败，勘察实测）；
R22 DeviceCommandQueue（per-device）维持 Android 命令面（wifi/speedtest/sim-boot）
串行，iOS xcrun 面不经它。
"""

from __future__ import annotations

import shutil
import tempfile
import time
from typing import Any, Callable

from ..errors import ApiError
from ..providers import xctrace
from ..providers.realios import RealIOSProvider
from ..sse import now_ms
from .collector_service import BaseCollector

# §26.3 冻结 metricId（注册表行 S2 已落地，registry.METRICS 单一事实源）
CPU_METRIC_ID = "cpu.proc.xctrace.pct"
MEM_METRIC_ID = "memory.footprint.mb"
IOS_PROCESS_METRICS = (CPU_METRIC_ID, MEM_METRIC_ID)
BASIS = "xctrace"

# 窗口长度（s）：勘察实测 4s trace ≈11.7MB、双序列 ~1s 粒度 4 点/窗；
# 更长窗口体积线性膨胀（§26.4 控 time-limit）——缺省 4s，S4 装配可覆写
DEFAULT_WINDOW_S = 4.0
# 相邻窗口间隔（s）：串行链路本身有开销，留小缓冲避免背靠背打满设备
DEFAULT_WINDOW_GAP_S = 1.0
# 失败窗退避上限（s）：真机 E2E 实测（2026-11-16）——record 失败仍按固定 5s 连发
# 会把 CoreDevice 隧道打进 "Timed out waiting for device to boot" 死态（14 连发
# 实锤）；隧道恢复需 15s+ 间隔，退避 gap 翻倍封顶 60s 覆盖恢复窗，成功即复位
_FAILURE_BACKOFF_CAP_S = 60.0


def _sample_record(metric_id: str, value: float, ts_ms: int) -> Any:
    from perfeval.collectors.android.samplers.base import SampleRecord

    return SampleRecord(
        metric_id=metric_id,
        value=float(value),
        ts_ms=int(ts_ms),  # 窗口锚点+offset 的 epoch ms（to_wall_ms 原样透传）
        tags={"basis": BASIS},
    )


class RealIOSCollector(BaseCollector):
    """真机 iOS 窗口批采样（§26.4）：record 窗 → export → 解析 → 批发点。"""

    def __init__(
        self,
        session: Any,
        bus: Any,
        *,
        ping_s: float = 60.0,
        provider: RealIOSProvider | None = None,
        window_s: float = DEFAULT_WINDOW_S,
        window_gap_s: float = DEFAULT_WINDOW_GAP_S,
        devicectl_runner: Callable[[list[str], float], Any] | None = None,
        xctrace_runner: Callable[[list[str], float], Any] | None = None,
        work_dir_factory: Callable[..., str] = tempfile.mkdtemp,
        epoch_clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(session, bus, ping_s=ping_s)
        self._provider = provider
        self._window_s = max(1.0, float(window_s))
        self._window_gap_s = max(0.0, float(window_gap_s))
        self._devicectl_runner = devicectl_runner
        self._xctrace_runner = xctrace_runner
        self._work_dir_factory = work_dir_factory
        # 宿主时钟注入面（窗口锚点 = record 退出时刻回推 time-limit；测试可控）
        self._epoch_clock = epoch_clock
        # 进程名解析注入面（测试替换；生产走 provider.resolve_process）
        self._process_resolver: Callable[[], str | None] = self._resolve_attach_name
        # 目标进程消失诊断锁存（§26.5 防刷屏，SimulatorCollector 同哲学）
        self._process_missing_notified = False
        self._next_window_at = 0.0  # start 即刻首窗（预热吸收冷启动时延）
        # 连续失败计数（失败窗退避用；成功窗复位——见 _FAILURE_BACKOFF_CAP_S 注）
        self._window_fail_streak = 0

    def set_enabled(self, metric_ids: dict[str, int]) -> None:
        self.session.set_metrics(dict(metric_ids))

    # -- 生命周期 --

    def _on_start(self) -> None:
        # 时钟锚点（§26.4）：host_epoch + method 声明窗口批语义与时延口径
        #（形状经 derive_clock_anchors._normalize_samplers_anchor 落盘）
        self.session.sampler_clock_anchor = {
            "epochMs": now_ms(),
            "source": "host_epoch",
            "method": (
                "xctrace window batch（窗口 ts=record 退出时刻回推 time-limit，"
                "行 ts=窗口起始+表内 Start Time 偏移；首窗预热时延不均摊，契约 §26.4）"
            ),
        }

    def _emit_due(self, now: float) -> None:
        if now < self._next_window_at:
            return
        if self._run_window():
            self._window_fail_streak = 0
            self._next_window_at = now + self._window_s + self._window_gap_s
        else:
            # 失败窗退避（gap 翻倍封顶）：record 连发会把 CoreDevice 隧道打挂
            # （自伤模式），拉到 15s+ 给隧道恢复窗；成功一窗即复位原节奏
            self._window_fail_streak += 1
            backoff = min(
                self._window_gap_s * (2 ** self._window_fail_streak),
                _FAILURE_BACKOFF_CAP_S,
            )
            self._next_window_at = now + self._window_s + backoff

    # -- 窗口批（一次完整链路）--

    def _run_window(self) -> bool:
        """执行一个采样窗；全失败路径该窗不出数值点（§26.5），不抛异常。

        返回 True=本窗健康收场（出点/进程缺失等语义态/未勾指标 no-op——维持原
        节奏）；False=设备/命令级失败（在线探测失败、探测重试耗尽、预热耗尽、
        record/export 失败——失败退避的计数源，防 record 连发打挂隧道）。"""
        metrics = [m for m in list(self.session.metrics) if m in IOS_PROCESS_METRICS]
        if not metrics:
            return True  # 会话未勾双指标：不起 xctrace 链路（无命令面承诺）
        if not self._device_online():
            return False  # 探测失败 → xctrace_unavailable / 权威离线 → tunnel.offline（已发）
        try:
            process_name = self._process_resolver()
        except ApiError as exc:
            # apps/processes 探测重试耗尽（§26.5 无数据语义）：该窗不出点不造数，
            # 不消费成 app.process.not_found/null 点（2026-11-16 审读修订）
            self.emit_diagnostic(exc.code, "error", exc.message, dict(exc.details))
            return False
        if process_name is None:
            self._notify_process_missing()
            return True
        self._process_missing_notified = False
        work_dir = self._work_dir_factory(prefix="pd-xctrace-")
        try:
            result = xctrace.run_sample_window(
                self.session.device_id,
                process_name,
                self._window_s,
                work_dir=work_dir,
                devicectl_runner=self._devicectl_runner,
                xctrace_runner=self._xctrace_runner,
                epoch_clock=self._epoch_clock,
            )
        except ApiError as exc:
            # 预热重试耗尽 / xcrun 缺失（§26.5：503 错误码 + SSE diagnostic）
            self.emit_diagnostic(exc.code, "error", exc.message, dict(exc.details))
            return False
        except xctrace.XctraceWindowError as exc:
            # record/export 失败（§26.5：该窗不出点 + xctrace.window.failed）
            self.emit_diagnostic(
                "xctrace.window.failed",
                "error",
                exc.message,
                {"reason": exc.reason, "exitCode": exc.exit_code},
            )
            return False
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
        rows = result["rows"]
        if not rows:
            # record 成功但零数据行：目标进程录制中消失（§26.5 同语义窗口）
            self._notify_process_missing()
            return True
        window_start_ms = result["windowStartEpochMs"]
        for row in rows:
            ts_ms = window_start_ms + row["offset_ms"]
            if CPU_METRIC_ID in metrics and row["cpu_pct"] is not None:
                self.emit_sample(_sample_record(CPU_METRIC_ID, row["cpu_pct"], ts_ms))
            if MEM_METRIC_ID in metrics and row["footprint_mb"] is not None:
                self.emit_sample(
                    _sample_record(MEM_METRIC_ID, row["footprint_mb"], ts_ms)
                )
        return True

    # -- 进程名解析（xctrace 禁 PID attach：仅进程名，§26.2）--

    def _resolve_attach_name(self) -> str | None:
        """会话目标 → xctrace --attach 进程名。

        session.process_name 显式提供时直用（R20 子进程目标同语义）；否则经
        devicectl apps+processes **strict** 探测把 packageName 归一到主进程名。
        探测命令失败（重试耗尽）→ raise ApiError（调用方按「无数据」诊断处置，
        该窗不出点不造数）；返回 None 仅表示探测成功后的权威负读数（未安装/
        未运行，调用方走 app.process.not_found + null 点，§26.5）。
        """
        explicit = getattr(self.session, "process_name", None)
        if explicit:
            return explicit
        provider = self._provider
        if provider is None:
            provider = RealIOSProvider(devicectl_runner=self._devicectl_runner)
        resolved = provider.resolve_process_strict(
            self.session.device_id, self.session.package_name
        )
        return resolved["name"] if resolved else None

    def _notify_process_missing(self) -> None:
        """§26.5/§6.1：目标进程不存在 → 诊断（锁存）+ 双指标 null 点。"""
        if not self._process_missing_notified:
            self._process_missing_notified = True
            self.emit_diagnostic(
                "app.process.not_found",
                "warn",
                f"目标进程 {self.session.package_name} 不存在，进程级指标出 null",
            )
        metrics = [m for m in list(self.session.metrics) if m in IOS_PROCESS_METRICS]
        for metric_id in metrics:
            self.emit_null_sample(metric_id, tags={"basis": BASIS})

    # -- 隧道在线检查（§26.5 会话中途失联）--

    def _device_online(self) -> bool:
        """tunnelState 是唯一在线判据（§26.1）：离线 → device.tunnel.offline 诊断。

        在线检查走 strict 探测（find_device_strict）：devicectl 命令失败重试耗尽
        → ApiError（xctrace_unavailable，reason=devicectl_probe_failed）——按
        「无数据」诊断处置（§26.4 锁屏「重试耗尽 → §26.5 诊断」同源），不把
        探测失败误报成 tunnel 离线（2026-11-16 审读修订）；device.tunnel.offline
        仅在命令成功且设备确不在表/非 online 时发出（权威负读数）。
        """
        provider = self._provider
        if provider is None:
            provider = RealIOSProvider(devicectl_runner=self._devicectl_runner)
        try:
            device = provider.find_device_strict(self.session.device_id)
        except ApiError as exc:
            self.emit_diagnostic(exc.code, "error", exc.message, dict(exc.details))
            return False
        if device is not None and device["state"] == "online":
            return True
        self.emit_diagnostic(
            "device.tunnel.offline",
            "error",
            f"设备 {self.session.device_id} 隧道离线（tunnelState≠connected），该窗不出点",
            {"state": device["state"] if device else None},
        )
        return False
