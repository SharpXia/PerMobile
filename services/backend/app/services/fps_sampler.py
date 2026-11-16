"""frame.fps.mean 的 real 采集通道（矩阵 R01，契约 §5 注 2）。

选型（ARCH §9.3 落地决策，2026-11-02）：**gfxinfo 主通道 + SurfaceFlinger 兜底**——

1. **gfxinfo 周期 reset+轮询**：start 时 `dumpsys gfxinfo <pkg> reset` 清零，每周期
   读累计渲染帧数，fps = Δframes/Δwall-time。多进程包每进程一段各带独立计数
  （各进程 HWUI 同受 vsync 门控）——逐段差分取最活跃段，不跨段求和（复核意见①，
   与 timestats 逐块差分同口径）。开销每 interval 1 次 dumpsys（[P§9]）。
   局限（实测 SM_F9560 + Vulkan 游戏）：HWUI 统计对 Vulkan/原生渲染目标恒为 0。
2. **SurfaceFlinger --latency 兜底**（Display 侧，行业标准的 adb FPS 法，口径对齐
   PerfDog「1 秒内屏幕真实刷新次数」[F§3.1]）：gfxinfo 连续零帧窗口达阈值时，按
   包名发现图层（`--list`），轮询 `--latency <layer>` 的 actual-present 时间戳，
   fps = 新时间戳数/Δwall-time。tags.source=surfaceflinger 标注。
3. **SurfaceFlinger --timestats 升级通道**（新 Android BLAST 合成下 --latency 仅回
   刷新周期行，实测 SM_F9560）：按 uid 精确匹配目标包图层块，逐块差分
   totalFrames 后取最活跃层（与 `_select_active_layer` 同哲学，R01「按窗口取」），
   不跨块求和——多图层各自受 vsync 门控，求和会得到 N×刷新率（2026-11-04 真机
   实测 358~361fps 的根因）。tags.method=timestats 标注。

口径声明：三通道系统层级不同（应用 HWUI 统计 vs Display 合成器侧），tags.source
逐点标注；曲线/报告不做跨源绝对值对齐承诺（矩阵「不可直接对标」节）。

窗口接线（契约 §21.5，S2 落地 2026-11-10，诊断语义 delta 由 S1 落笔契约 §8/§21.5）：
resolve_fps_window 的解析结果经 collector_service（fpsWindow entry → 工厂
fps_window 参数）送达本采样器，三通道按窗口过滤——

- ``surface_view`` → 起步即走 SF 通道（latency → timestats 升级链不变），
  图层发现/选层与 timestats 块过滤均限定 SurfaceView 图层；无可匹配图层时保留
  gfxinfo 通道/未过滤块表 + 一次性诊断（窗口未应用，tags 保留 source/method）；
- ``activity`` → gfxinfo 通道按窗口（包/activity）过滤：`dumpsys gfxinfo <pkg>`
  的段头带进程名 `[pkg(:sub)]`，子进程段（名字含 ":"，如 :push，非 Activity 承载
  进程）整段排除；gfxinfo 升级 SF 后组件图层优先、timestats 排除 SurfaceView 块；
- 每条采样点 tags 附 ``fpsWindow``（请求的窗口类型，与 synthetic 标注同键）——
  实际应用/未应用状态由一次性 diagnostic（``fps.window.*``）报告，不静默忽略。

实现遵循 perfeval Sampler 协议（base.py：probe/start/sample/stop + 内部诊断缓冲），
与 MemSampler 同款失败连击诊断模式。进程不存在由 RealCollector._check_process 的
app.process.not_found 机制统一出 null（frame.fps.mean 已入 PROCESS_METRICS）。
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import datetime, timezone

from perfeval.collectors.android.samplers.base import (
    DeviceContext,
    ProbeResult,
    SampleRecord,
)

METRIC_ID = "frame.fps.mean"

# 默认采样间隔（注册表 §5 defaultIntervalMs 同值）
DEFAULT_INTERVAL_MS = 500

# `dumpsys gfxinfo <pkg>` 累计渲染帧数行（rc=0 时出现；分段 reset 后从 0 计）
_TOTAL_FRAMES_RE = re.compile(r"Total frames rendered:\s*(\d+)")

# 连续读取失败达到该次数记一条通道 diagnostic；恢复即清零（对齐 MemSampler 模式）
_FAILURE_DIAGNOSTIC_THRESHOLD = 2

# gfxinfo 连续零帧窗口达到该数 → 切 SurfaceFlinger 兜底（Vulkan/原生渲染盲区）
_ZERO_WINDOW_FALLBACK_THRESHOLD = 3

# SF latency 连续零帧窗口达到该数 → 升级 timestats（新 Android 的 BLAST 合成下
# --latency 仅回刷新周期行，实测 SM_F9560；timestats per-layer totalFrames 可用）
_LATENCY_ZERO_ESCALATE_THRESHOLD = 3


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FpsSampler:
    """gfxinfo 主 + SurfaceFlinger 兜底的 FPS 采样器（real 通道，矩阵 R01）。"""

    name = "fps"
    metric_ids = (METRIC_ID,)

    def __init__(
        self,
        *,
        interval_ms: int = DEFAULT_INTERVAL_MS,
        utc_now: Callable[[], str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        fps_window: dict[str, str] | None = None,
    ) -> None:
        self.interval_ms = max(100, int(interval_ms))
        self._utc_now = utc_now or _utc_now_iso
        self._monotonic = monotonic
        # 契约 §21.5（S2 接线）：窗口配置 {type: activity|surface_view}——session
        # 侧 resolve_fps_window 已解析（auto 已消解为具体窗口）；None/未知值 =
        # 未送达，维持无窗口过滤的既有行为（不猜）
        window_type = dict(fps_window or {}).get("type")
        self._window_type: str | None = (
            window_type if window_type in ("activity", "surface_view") else None
        )
        self._diagnostics: list[str] = []
        # 窗口诊断一次性旗标（防逐轮刷屏；start 重置）
        self._window_applied_reported = False
        self._window_no_layer_reported = False
        self._fail_streak = 0
        self._failure_reported = False
        # gfxinfo 通道基准：{进程段身份(pid): 累计帧数} + monotonic 时刻；None =
        # 尚未建立。多进程包各进程 HWUI 各自受 vsync 门控，逐段差分取最活跃段
        #（复核意见①：跨进程求和与 timestats 跨块求和同型，会得 N×刷新率）
        self._baseline: dict[str, int] | None = None
        self._baseline_ts: float | None = None
        # 双通道状态
        self._mode = "gfxinfo"  # gfxinfo | surfaceflinger | timestats
        self._zero_streak = 0
        self._sf_layer: str | None = None
        self._sf_last_max_ns = 0
        self._sf_last_t: float | None = None
        self._sf_reported = False
        # timestats 通道：逐块累计 totalFrames 基准（{块身份: 帧数}）+ 轮询窗口
        self._ts_baseline: dict[tuple[str, str, str], int] | None = None
        self._ts_last_t: float | None = None
        self._ts_enabled = False
        self._ts_no_blocks_reported = False
        # 包 uid（timestats 图层名在新 ROM 上可能脱敏为空——实测 SM_F9560，
        # uid 归属是可靠键；pm list packages -U 与 net_sampler 同源路径）
        self._pkg_uid: str | None = None

    @property
    def diagnostics(self) -> list[str]:
        return list(self._diagnostics)

    def drain_diagnostics(self) -> list[str]:
        drained = list(self._diagnostics)
        self._diagnostics.clear()
        return drained

    # ---- gfxinfo 通道 ----

    def _gfxinfo_command(self, ctx: DeviceContext, *, reset: bool) -> list[str]:
        if not ctx.package_name:
            return []
        args = ("dumpsys", "gfxinfo", ctx.package_name) + (("reset",) if reset else ())
        return ctx.adb.command("shell", *args, serial=ctx.serial)

    # `dumpsys gfxinfo <pkg>` 进程段头（多进程包每进程一段，各带独立计数；
    # 段尾 [process_name] 为 AOSP 输出形态——activity 窗口过滤的归属键）
    _GFX_SECTION_RE = re.compile(r"\*\* Graphics info for pid (\d+)(?: \[([^\]]+)\])?")

    def _read_total_frames(self, ctx: DeviceContext, *, reset: bool) -> dict[str, int] | None:
        """`dumpsys gfxinfo <pkg>` → {进程段身份(pid): 累计渲染帧数}。

        复核意见①（2026-11-06）：不再跨段求和——多进程包（主进程 + :push 等）
        每进程各出一个 `Total frames rendered` 行，各进程 HWUI 同样受 vsync 门控，
        求和与 timestats 跨块求和同型（N×刷新率）；对齐 CLI 侧 gfxinfo.py 按段
        圈定 scope 取单值的口径。段头缺失（单行输出）归入身份 ""；同身份多行
        取 max（去重不双计）。无任何计数行 → None（读失败，不出假零）。

        activity 窗口过滤（契约 §21.5，S2 接线）：段头进程名含 ":"（:push/:media
        等子进程，非 Activity 承载进程）的整段排除；段名缺失（旧行形态）不猜、
        段保留——窗口过滤只作用于可归属段。
        """
        command = self._gfxinfo_command(ctx, reset=reset)
        if not command:
            return None
        try:
            completed = ctx.command_runner(command, 10)
        except Exception:  # noqa: BLE001 - 单次读取失败按无数据处理，由调度器继续
            return None
        if completed.returncode != 0:
            return None
        sections: dict[str, int] = {}
        current_pid = ""
        skip_section = False
        for line in completed.stdout.splitlines():
            section = self._GFX_SECTION_RE.search(line)
            if section is not None:
                process_name = (section.group(2) or "").strip()
                if self._window_type == "activity" and ":" in process_name:
                    skip_section = True  # 子进程段：排除到下一段头为止
                    continue
                skip_section = False
                current_pid = section.group(1)
                continue
            if skip_section:
                continue
            counter = _TOTAL_FRAMES_RE.search(line)
            if counter is not None:
                total = int(counter.group(1))
                prev = sections.get(current_pid)
                sections[current_pid] = total if prev is None else max(prev, total)
        return sections or None

    def probe(self, ctx: DeviceContext) -> ProbeResult:
        """探测通道可用性（与 MemSampler 同哲学：探通道不探进程存活——probe 先于
        launch，目标进程此时不存在属预期）。gfxinfo 可用即可 probe 通过；SF 兜底
        在运行期按需发现图层，probe 不预检。"""
        if not ctx.package_name:
            return ProbeResult.unavailable("package_name missing")
        command = self._gfxinfo_command(ctx, reset=False)
        if not command:
            return ProbeResult.unavailable("package_name missing")
        try:
            completed = ctx.command_runner(command, 10)
        except Exception as exc:  # noqa: BLE001 - 设备异常/超时按能力缺失
            return ProbeResult.unavailable(f"dumpsys gfxinfo command failed: {exc}")
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[:200]
            suffix = f": {detail}" if detail else ""
            return ProbeResult.unavailable(
                f"dumpsys gfxinfo failed rc={completed.returncode}{suffix}"
            )
        return ProbeResult.ok()

    def start(self, ctx: DeviceContext) -> None:
        """reset 清零计数器并建立基准；失败不阻塞调度器（sample 轮询会重试）。

        契约 §21.5（S2 接线）：surface_view 窗口起步即切 SF 通道（Display 侧
        图层过滤）；activity 窗口留 gfxinfo 通道（进程段过滤）并报告实际应用。
        """
        self._mode = "gfxinfo"
        self._zero_streak = 0
        self._sf_layer = None
        self._sf_last_max_ns = 0
        self._sf_last_t = None
        self._sf_reported = False
        self._ts_baseline = None
        self._ts_last_t = None
        self._ts_no_blocks_reported = False
        self._window_applied_reported = False
        self._window_no_layer_reported = False
        if self._ts_enabled:
            self._ts_set_enabled(ctx, False)
        if self._window_type == "surface_view" and self._apply_window_surface_view(ctx):
            # SF 通道接管：gfxinfo 基准不在 start 建立，SF 回退路径按需重建
            #（_sample_surfaceflinger 失败路径置 None → 首轮补建，不伪造）
            return
        if self._window_type == "activity" and not self._window_applied_reported:
            self._diagnostics.append("fps.window.activity.applied")
            self._window_applied_reported = True
        sections = self._read_total_frames(ctx, reset=True)
        if sections is None:
            return
        self._baseline = sections
        self._baseline_ts = self._monotonic()

    def _apply_window_surface_view(self, ctx: DeviceContext) -> bool:
        """surface_view 窗口 → SF 图层发现（仅 SurfaceView 图层）+ 活跃度选层。

        应用成功 → 切 surfaceflinger 模式 + 一次性诊断（实际应用报告，契约
        §21.5「由采样层 diagnostic 报告实际应用」）；无可匹配 SurfaceView 图层 →
        保留 gfxinfo 通道 + 一次性诊断（窗口未应用，不伪造）。
        """
        candidates = [
            layer for layer in self._discover_sf_layers(ctx) if "SurfaceView" in layer
        ]
        layer = self._select_active_layer(ctx, candidates) if candidates else None
        if layer is None:
            if not self._window_no_layer_reported:
                self._diagnostics.append("fps.window.surface_view.no_layer")
                self._window_no_layer_reported = True
            return False
        self._mode = "surfaceflinger"
        self._sf_layer = layer
        self._sf_last_max_ns = 0
        self._sf_last_t = None
        self._zero_streak = 0
        if not self._window_applied_reported:
            self._diagnostics.append("fps.window.surface_view.applied")
            self._window_applied_reported = True
        return True

    def sample(self, ctx: DeviceContext) -> list[SampleRecord]:
        if self._mode == "surfaceflinger":
            return self._sample_surfaceflinger(ctx)
        if self._mode == "timestats":
            return self._sample_timestats(ctx)
        return self._sample_gfxinfo(ctx)

    # ---- gfxinfo 采样 ----

    def _sample_gfxinfo(self, ctx: DeviceContext) -> list[SampleRecord]:
        sections = self._read_total_frames(ctx, reset=False)
        now = self._monotonic()
        if sections is None:
            self._fail_streak += 1
            if (
                self._fail_streak >= _FAILURE_DIAGNOSTIC_THRESHOLD
                and not self._failure_reported
            ):
                self._diagnostics.append("fps.gfxinfo.no_counters")
                self._failure_reported = True
            return []
        self._fail_streak = 0
        self._failure_reported = False
        baseline = self._baseline
        baseline_ts = self._baseline_ts
        if baseline is None or baseline_ts is None:
            # start 时 reset 失败：以当前读数补建基准，本轮不出点
            self._baseline = sections
            self._baseline_ts = now
            return []
        if any(sections.get(pid, 0) < frames for pid, frames in baseline.items()):
            # 任一进程段计数回退 = 目标进程重启（计数随进程销毁清零）：静默重置基准
            self._baseline = sections
            self._baseline_ts = now
            return []
        elapsed_s = now - baseline_ts
        if elapsed_s <= 0:
            return []
        # 逐进程段差分取最活跃段（复核意见①，与 timestats 逐块差分同口径）：
        # 新出现进程段（无基线，如刚拉起的 :push）本窗不计入，不猜
        best_delta = 0
        for pid, total in sections.items():
            base_frames = baseline.get(pid)
            if base_frames is None:
                continue
            best_delta = max(best_delta, total - base_frames)
        fps = best_delta / elapsed_s
        self._baseline = sections
        self._baseline_ts = now
        if best_delta == 0:
            self._zero_streak += 1
            if self._zero_streak >= _ZERO_WINDOW_FALLBACK_THRESHOLD:
                self._try_switch_surfaceflinger(ctx)
        else:
            self._zero_streak = 0
        return [
            SampleRecord(
                metric_id=METRIC_ID,
                value=round(fps, 2),
                ts_ms=ctx.elapsed_ms(),
                tags={
                    "unit": "fps",
                    "collector": f"sampler.{self.name}",
                    "source": "gfxinfo",
                    "samplingWindowMs": int(elapsed_s * 1000),
                    **self._window_tags(),
                },
            )
        ]

    def _window_tags(self) -> dict[str, str]:
        """契约 §21.5（S2 接线）：采样点 tags 附请求的窗口类型（与 synthetic
        frame.fps.mean 的 tags.fpsWindow 同键；实际应用状态由 fps.window.* 诊断
        报告——tags 标请求、诊断标结果，不伪造应用成功）。"""
        if self._window_type is None:
            return {}
        return {"fpsWindow": self._window_type}

    # ---- SurfaceFlinger 兜底（Display 侧）----

    def _sf_list_command(self, ctx: DeviceContext) -> list[str]:
        return ctx.adb.command("shell", "dumpsys", "SurfaceFlinger", "--list", serial=ctx.serial)

    def _sf_latency_command(self, ctx: DeviceContext) -> list[str]:
        return ctx.adb.command(
            "shell", "dumpsys", "SurfaceFlinger", "--latency", self._sf_layer or "",
            serial=ctx.serial,
        )

    # `--list` 行内的图层名（component#seq 或 SurfaceView[...]#seq）。字符类排除
    # `{`——新版 RequestedLayerState{<hash> <name> …} 的 hash 前缀不能被吞进匹配；
    # ActivityRecord 镜像行因组件与 # 之间有空格不会命中
    _SF_LAYER_NAME_RE = re.compile(r"[\w.$]+/[\w.$\-]+#\d+|\S*SurfaceView\S*#\d+")

    def _discover_sf_layers(self, ctx: DeviceContext) -> list[str]:
        """按包名发现候选图层（有序：SurfaceView 优先，其余按 --list 顺序）。

        兼容两种 `--list` 输出：旧版纯图层名行；新版 `RequestedLayerState{<hash>
        <name> parentId=…}` 复合行（实测 SM_F9560）——图层名从行内提取，
        ActivityRecordInputSink（窗口遮挡占位）与 Splash 层过滤。
        """
        try:
            completed = ctx.command_runner(self._sf_list_command(ctx), 10)
        except Exception:  # noqa: BLE001 - 发现失败按无图层处理
            return []
        if completed.returncode != 0:
            return []
        pkg = (ctx.package_name or "").split(":")[0]
        candidates: list[str] = []
        for line in completed.stdout.splitlines():
            if not pkg or pkg not in line or "Splash" in line:
                continue
            if "ActivityRecordInputSink" in line:
                continue
            for match in self._SF_LAYER_NAME_RE.finditer(line):
                name = match.group(0)
                if pkg in name and name not in candidates:
                    candidates.append(name)
        surface_views = [c for c in candidates if "SurfaceView" in c]
        others = [c for c in candidates if "SurfaceView" not in c]
        return surface_views + others

    def _select_active_layer(self, ctx: DeviceContext, candidates: list[str]) -> str | None:
        """活跃度选层：对候选各探一次 latency，取最近合成时间戳最新的层。

        包内多图层（可见页/Debug 悬浮窗/已 detach 的残留层）同名共存是常态
        （实测 SM_F9560），按名字序会选到不可见层——「最新 actual-present 时间戳
        最大」的层才是当前在出帧的窗口。全部探测失败返回 None；全部零帧时返回
        首个候选（静止画面零帧属真实状态）。
        """
        best: str | None = None
        best_key: tuple[int, int] = (-1, -1)  # (最新 actual-present ns, 帧数)
        saw_ok = False
        for layer in candidates[:4]:
            self._sf_layer = layer
            try:
                completed = ctx.command_runner(self._sf_latency_command(ctx), 10)
            except Exception:  # noqa: BLE001 - 单层探测失败跳过
                continue
            if completed.returncode != 0:
                continue
            times = self._parse_present_times(completed.stdout)
            saw_ok = True
            key = (max(times) if times else 0, len(times))
            if key > best_key:
                best_key = key
                best = layer
        if not saw_ok:
            return None
        self._sf_layer = best
        return best

    def _try_switch_surfaceflinger(self, ctx: DeviceContext) -> None:
        candidates = self._discover_sf_layers(ctx)
        if self._window_type == "activity":
            # activity 窗口（契约 §21.5，S2 接线）：组件图层（pkg/Activity#seq）
            # 优先——SurfaceView 子窗口不是 Activity 承载面
            surface_views = [c for c in candidates if "SurfaceView" in c]
            others = [c for c in candidates if "SurfaceView" not in c]
            candidates = others + surface_views
        layer = self._select_active_layer(ctx, candidates) if candidates else None
        if layer is None:
            if not self._sf_reported:
                self._diagnostics.append("fps.surfaceflinger.no_layer")
                self._sf_reported = True
            self._zero_streak = 0  # 不逐轮重试发现；下次连续零帧再试
            return
        self._mode = "surfaceflinger"
        self._sf_layer = layer
        self._sf_last_max_ns = 0
        self._sf_last_t = None
        self._zero_streak = 0
        self._diagnostics.append("fps.source.fallback_surfaceflinger")

    def _sample_surfaceflinger(self, ctx: DeviceContext) -> list[SampleRecord]:
        try:
            completed = ctx.command_runner(self._sf_latency_command(ctx), 10)
        except Exception:  # noqa: BLE001 - 单次读取失败按无数据处理
            return []
        now = self._monotonic()
        if completed.returncode != 0:
            # 图层消失/命令失败：回退 gfxinfo 通道重探
            self._diagnostics.append("fps.surfaceflinger.unavailable")
            if self._window_type == "surface_view" and not self._window_no_layer_reported:
                # surface_view 窗口会话回退 gfxinfo：窗口不适用该通道（gfxinfo 统计
                # 包级 HWUI、覆盖不了 SurfaceView 子窗口）——一次性诊断 + tags 保留
                # source/method 如实标注（契约 §21.5「不适用通道……出 diagnostic」）
                self._diagnostics.append("fps.window.surface_view.no_layer")
                self._window_no_layer_reported = True
            self._mode = "gfxinfo"
            self._zero_streak = 0
            self._baseline = None
            self._baseline_ts = None
            return []
        # 新 Android（BLAST 合成）的 --latency 仅回一行刷新周期 ns（实测 SM_F9560
        # 输出 "8333333"）——无时间戳可差分，直接升级 timestats（per-layer
        # totalFrames，本机实测可用）
        if self._parse_present_times(completed.stdout) == [] and len(
            completed.stdout.split()
        ) <= 1:
            self._diagnostics.append("fps.surfaceflinger.latency_unavailable")
            self._escalate_timestats(ctx)
            return []
        present_ns = self._parse_present_times(completed.stdout)
        new_frames = [t for t in present_ns if t > self._sf_last_max_ns]
        if new_frames:
            self._sf_last_max_ns = max(self._sf_last_max_ns, max(new_frames))
        last_t = self._sf_last_t
        self._sf_last_t = now
        if last_t is None:
            return []  # 首轮建时间基准
        elapsed_s = now - last_t
        if elapsed_s <= 0:
            return []
        if not new_frames:
            self._zero_streak += 1
            if self._zero_streak >= _LATENCY_ZERO_ESCALATE_THRESHOLD:
                self._diagnostics.append("fps.surfaceflinger.zero_windows")
                self._escalate_timestats(ctx)
                return []
        else:
            self._zero_streak = 0
        fps = len(new_frames) / elapsed_s
        return [
            SampleRecord(
                metric_id=METRIC_ID,
                value=round(fps, 2),
                ts_ms=ctx.elapsed_ms(),
                tags={
                    "unit": "fps",
                    "collector": f"sampler.{self.name}",
                    "source": "surfaceflinger",
                    "method": "latency",
                    "layer": self._sf_layer or "",
                    "samplingWindowMs": int(elapsed_s * 1000),
                    **self._window_tags(),
                },
            )
        ]

    # ---- SurfaceFlinger timestats（新 Android Display 侧，per-layer totalFrames）----

    def _ts_command(self, ctx: DeviceContext, arg: str) -> list[str]:
        return ctx.adb.command("shell", "dumpsys", "SurfaceFlinger", "--timestats", arg, serial=ctx.serial)

    def _ts_set_enabled(self, ctx: DeviceContext, enable: bool) -> None:
        arg = "-enable" if enable else "-disable"
        try:
            ctx.command_runner(self._ts_command(ctx, arg), 10)
        except Exception:  # noqa: BLE001 - 开关失败由后续 dump 诊断兜底
            pass
        self._ts_enabled = enable

    def _escalate_timestats(self, ctx: DeviceContext) -> None:
        """清零并开启 timestats 统计窗，切 timestats 模式。"""
        try:
            ctx.command_runner(self._ts_command(ctx, "-clear"), 10)
        except Exception:  # noqa: BLE001
            pass
        self._ts_set_enabled(ctx, True)
        self._mode = "timestats"
        self._ts_baseline = None
        self._ts_last_t = None
        self._ts_no_blocks_reported = False
        self._zero_streak = 0
        self._diagnostics.append("fps.source.escalate_timestats")

    _TS_BLOCK_KEYS = ("layerName", "packageName")

    def _resolve_pkg_uid(self, ctx: DeviceContext) -> str | None:
        """`pm list packages -U` → 主包 uid（缓存一次；失败 None，退回名字匹配）。"""
        if self._pkg_uid is not None:
            return self._pkg_uid
        pkg = (ctx.package_name or "").split(":")[0]
        if not pkg:
            return None
        command = ctx.adb.command("shell", "pm", "list", "packages", "-U", serial=ctx.serial)
        try:
            completed = ctx.command_runner(command, 10)
        except Exception:  # noqa: BLE001 - 解析失败退回名字匹配
            return None
        if completed.returncode != 0:
            return None
        uid: str | None = None
        for line in completed.stdout.splitlines():
            # 行形如 "package:com.example.game uid:10780"（子进程包共享主 uid 之外的
            # 独立 uid——只取与主包名精确相等的行）
            if not line.startswith(f"package:{pkg} "):
                continue
            for token in line.split():
                if token.startswith("uid:"):
                    uid = token[4:].strip() or None
                    break
            break
        self._pkg_uid = uid
        return uid

    def _read_ts_blocks(self, ctx: DeviceContext) -> dict[tuple[str, str, str], int] | None:
        """`--timestats -dump` → 目标包图层块的 {块身份: totalFrames}。

        匹配口径（2026-11-06 修复，替代旧「uid OR 包名子串 OR 图层名子串 + 跨块
        求和」——三者叠加曾致真机 358~361fps > 刷新率）：

        - **uid 精确相等**为主键（`pm list packages -U` 主包 uid；图层名在新 ROM
          上会脱敏为空或字面 `none`，实测 SM_F9560，uid 是唯一可靠归属键）；
        - 仅当 uid 解析失败时退回 **packageName 精确相等**——不做任何子串/前缀
          匹配，子包（com.pkg.bilibili 等，uid 不同）天然排除；
        - **同块身份（layerName+packageName+uid）出现多个块取 max 不求和**：
          真机实测同一 (uid, layerName) 会输出重复块（Bouncer/NotificationShade
          各 2 块），求和即同层帧数双计。

        返回 None = 读取失败或无命中块（不出假零，由调用方按无数据处理）。
        """
        try:
            completed = ctx.command_runner(self._ts_command(ctx, "-dump"), 15)
        except Exception:  # noqa: BLE001 - 单次读取失败按无数据处理
            return None
        if completed.returncode != 0:
            return None
        pkg = (ctx.package_name or "").split(":")[0]
        if not pkg:
            return None
        pkg_uid = self._resolve_pkg_uid(ctx)
        blocks: dict[tuple[str, str, str], int] = {}
        layer_name = ""
        package_name = ""
        block_uid = ""
        for line in completed.stdout.splitlines():
            stripped = line.strip()
            key, _, value = stripped.partition("=")
            key = key.strip()
            # 块字段顺序随 ROM/层类型变化（uid 可能先于或后于 layerName）：
            # 在 totalFrames 消费点统一结算并重置，避免清掉已读字段
            if key == "layerName":
                layer_name = value.strip()
            elif key == "uid":
                block_uid = value.strip()
            elif key == "packageName":
                package_name = value.strip()
            elif key == "totalFrames":
                matched = (
                    (pkg_uid is not None and block_uid == pkg_uid)
                    or (pkg_uid is None and package_name == pkg)
                )
                if matched:
                    try:
                        frames = int(value.strip())
                    except ValueError:
                        frames = 0
                    identity = (layer_name, package_name, block_uid)
                    prev = blocks.get(identity)
                    blocks[identity] = frames if prev is None else max(prev, frames)
                layer_name = package_name = block_uid = ""
        return blocks or None

    def _filter_blocks_by_window(
        self, blocks: dict[tuple[str, str, str], int]
    ) -> tuple[dict[tuple[str, str, str], int], bool]:
        """显式窗口/图层过滤（契约 §21.5，S2 接线——R01「按窗口取」的入参化）。

        surface_view → 仅保留图层名含 SurfaceView 的块；activity → 排除
        SurfaceView 块（组件层承载 Activity 窗口）。过滤后为空 → 回退未过滤块表
        并标记窗口未应用（调用方出一次性诊断，tags 保留 source/method 如实标注，
        不伪造窗口已生效）。无窗口配置 → (原块表, True)。
        """
        if self._window_type is None:
            return blocks, True
        if self._window_type == "surface_view":
            filtered = {
                identity: frames
                for identity, frames in blocks.items()
                if "SurfaceView" in identity[0]
            }
        else:  # activity
            filtered = {
                identity: frames
                for identity, frames in blocks.items()
                if "SurfaceView" not in identity[0]
            }
        if not filtered:
            return blocks, False
        return filtered, True

    def _sample_timestats(self, ctx: DeviceContext) -> list[SampleRecord]:
        blocks = self._read_ts_blocks(ctx)
        now = self._monotonic()
        if blocks is None:
            if self._ts_baseline is None and not self._ts_no_blocks_reported:
                self._diagnostics.append("fps.timestats.no_matching_blocks")
                self._ts_no_blocks_reported = True
            return []
        blocks, window_applied = self._filter_blocks_by_window(blocks)
        if not window_applied and not self._window_no_layer_reported:
            # 窗口过滤后无命中块：回退未过滤块表（窗口未应用）——一次性诊断，
            # tags 保留 method=timestats 如实标注（契约 §21.5「不适用通道……出
            # diagnostic」）
            self._diagnostics.append(f"fps.window.{self._window_type}.no_layer")
            self._window_no_layer_reported = True
        last_t = self._ts_last_t
        self._ts_last_t = now
        baseline = self._ts_baseline
        if baseline is None or last_t is None:
            self._ts_baseline = blocks
            return []  # 首轮建基准
        elapsed_s = now - last_t
        if elapsed_s <= 0:
            return []
        # 逐块差分后取最活跃层（与 _select_active_layer 同哲学，R01「按窗口取」）：
        # 同 uid 多图层各自受 vsync 门控（窗口层 + SurfaceView 层 ≈ 各 120fps），
        # 跨块求和会得到 N×刷新率——只取 ΔtotalFrames 最大的层。
        best_delta = 0
        best_layer = ""
        overlap = False
        for identity, cur_frames in blocks.items():
            prev_frames = baseline.get(identity)
            if prev_frames is None:
                continue  # 新出现块：无窗口内基线，本轮不计入（不猜）
            overlap = True
            delta = cur_frames - prev_frames
            if delta < 0:
                continue  # 块级计数器复位（图层销毁重建）：该块本轮跳过
            if delta > best_delta:
                best_delta = delta
                best_layer = identity[0]
        if not overlap:
            # 复核意见②（2026-11-06）：命中块全部换新身份（UI 应用切 Activity 换
            # layerName 且无持久层）——无窗口内差分基线，真实 fps 未知，按读失败
            # 同径不出点（不产单点假 0），仅重建基线供下一窗口差分
            self._ts_baseline = blocks
            return []
        self._ts_baseline = blocks
        fps = best_delta / elapsed_s
        return [
            SampleRecord(
                metric_id=METRIC_ID,
                value=round(fps, 2),
                ts_ms=ctx.elapsed_ms(),
                tags={
                    "unit": "fps",
                    "collector": f"sampler.{self.name}",
                    "source": "surfaceflinger",
                    "method": "timestats",
                    "layer": best_layer,
                    "samplingWindowMs": int(elapsed_s * 1000),
                    **self._window_tags(),
                },
            )
        ]

    @staticmethod
    def _parse_present_times(stdout: str) -> list[int]:
        """`--latency` 输出行 `desired actual ready`（ns）；取 actual 列的有效值。"""
        times: list[int] = []
        for line in stdout.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                actual = int(parts[1])
            except ValueError:
                continue
            if actual > 0:
                times.append(actual)
        return times

    def stop(self) -> list[SampleRecord]:
        return []

    def close(self, ctx: DeviceContext) -> None:
        """timestats 统计窗关闭（调度器生命周期外、会话停止时由 collector 调用）。"""
        if self._ts_enabled:
            self._ts_set_enabled(ctx, False)
