"""屏幕采集（M4，契约 §16.1 录屏 / §16.2 截图，DEVELOPMENT_PLAN R35/R36）。

录屏 = 会话 record/start~record/stop 生命周期内的 ``SessionScreenRecorder``：

- **复用 perfeval 采集内核，不复制实现**（ARCH §1 路线 A 同仓库 import）：
  三段命令计划来自 ``build_screen_record_plan``（plan.py:278-390），分段/watchdog/
  rollover/判废生命周期来自 ``_ScreenRecordSession``（runner.py:502-758）——
  175s 单段上限（plan.py:20 ``SCREENRECORD_TIME_LIMIT_MAX_S``）、watchdog 中途
  rollover（默认 0.5s 轮询，runner.py:59）、pull 后 <1KB 判废
  （``SCREENRECORD_MIN_VALID_BYTES=1024``，plan.py:29）全部原样生效；
- 会话录屏没有可预估的交互窗口（录到用户按 stop 为止），等价于 perfeval
  「含 manual 步骤且未设 maxDurationMs」分支：计划经一个 manual 占位步骤驱动
  ``build_screen_record_plan`` 走 extendable 路径——time-limit=175s、首段 1 段、
  超长窗口由 watchdog 动态续段（screen-segNNN.mp4，plan.py:83-95）。170s 预分段
  逻辑保留在 perfeval 计划构造内部，会话模式无估算窗口故不触发（如实声明）；
- 本地产物沿用 perfeval 采集命名 ``screen.mp4``/``screen-segNNN.mp4``，save 时由
  session_manager 统一重命名为契约命名 ``video.mp4``/``video-segNNN.mp4``
  （契约 §16.1 显式映射）；
- video/clock 锚点形状由 ``_ScreenRecordSession`` + ``derive_clock_anchors``
  （timebase.py:79-128）产出，save 时原样落盘 manifest ``context.clockAnchors``。

截图 = real-only ``exec-out screencap``：perfeval ``AdbClient`` 只有文本模式
``run()``（adb.py:62-77，binary 会损坏 PNG），本模块新增命令构造 + 二进制子进程
调用（契约 §16.2「实现期新增命令构造」）。
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from perfeval.collectors.android.adb import ADB_CREATION_FLAGS, AdbClient
from perfeval.collectors.android.flow import InteractionCommand
from perfeval.collectors.android.plan import (
    SCREENRECORD_DEFAULT_BITRATE_MBPS,
    build_screen_record_plan,
)
from perfeval.collectors.android.runner import (
    SCREENRECORD_FINALIZE_WAIT_S,
    SCREENRECORD_WATCHDOG_INTERVAL_S,
    AndroidCommandResult,
    AndroidRunExecutor,
    _ScreenRecordSession,
)

from ..errors import ApiError

# 截图 PNG 魔数：exec-out 返回非 PNG（ROM 弹窗/权限拦截等）按拉流失败处理
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_SCREENCAP_TIMEOUT_S = 30.0


class SessionScreenRecorder:
    """单次会话录屏生命周期封装（probe → start → watchdog rollover → stop → pull）。

    perfeval ``_ScreenRecordSession`` 只依赖 executor 的 ``_run`` 命令执行面
    （runner.py:550-553），因此直接持有 ``AndroidRunExecutor``（命令执行、超时
    归一、TimeoutExpired→rc=124 全部复用），不重写任何 adb 交互。
    """

    def __init__(
        self,
        *,
        session: Any,
        adb: AdbClient,
        artifact_dir: Path,
        case_name: str,
        bitrate_mbps: float = SCREENRECORD_DEFAULT_BITRATE_MBPS,
        finalize_wait_s: float = SCREENRECORD_FINALIZE_WAIT_S,
        watchdog_interval_s: float = SCREENRECORD_WATCHDOG_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
        command_runner: Callable[[list[str], float | None], Any] | None = None,
    ) -> None:
        self._session = session
        self._adb = adb
        self._artifact_dir = artifact_dir
        self._case_name = case_name
        self._bitrate_mbps = bitrate_mbps
        self._finalize_wait_s = max(0.0, float(finalize_wait_s))
        self._watchdog_interval_s = max(0.0, float(watchdog_interval_s))
        self._clock = clock
        # 诊断落点与会话共享同一列表（对齐 perfeval collect 内部做法，runner.py:536-537）
        self.diagnostics: list[str] = []
        # command_runner 注入面：与 perfeval AndroidRunExecutor 同款接缝（测试用
        # 假 adb runner 驱动真实 _ScreenRecordSession 生命周期，无需真机）
        executor_kwargs: dict[str, Any] = {
            "adb": adb,
            "screenrecord_finalize_wait_s": self._finalize_wait_s,
            "clock": clock,
            "screenrecord_watchdog_interval_s": self._watchdog_interval_s,
        }
        if command_runner is not None:
            executor_kwargs["command_runner"] = command_runner
        self._executor = AndroidRunExecutor(**executor_kwargs)
        self._inner: _ScreenRecordSession | None = None
        self._commands: list[AndroidCommandResult] = []

    # ---- 锚点（save 时经 derive_clock_anchors 原样落盘）----

    @property
    def video_anchors(self) -> dict | None:
        return self._inner.video_anchors if self._inner is not None else None

    # ---- 生命周期 ----

    def start(self) -> None:
        """probe + 读锚点 + 启动首段 + 起 watchdog（对齐 runner start_first）。"""
        # perfeval collect 同款前置：本地产物目录先行创建（runner.py:135-137），
        # 否则段到时 rollover / pull 的本地写盘目标不存在
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        plan = build_screen_record_plan(
            adb=self._adb,
            run_id=self._case_name,
            # 会话录屏：开关即契约 §16.1 recordScreen，无 maxDurationMs（录到 stop）
            config={"enabled": True, "bitrateMbps": self._bitrate_mbps},
            # manual 占位步骤（timeout_s=None 不计入估算窗口）→ 计划走 extendable
            # 分支：time-limit=175s、watchdog 动态续段（plan.py:323-337）
            interaction_commands=[
                InteractionCommand(name="session-record-window", is_manual=True)
            ],
            post_launch_wait_s=0.0,
            artifact_dir=self._artifact_dir,
            serial=self._session.device_id,
        )
        if plan is None:  # 防御：enabled=True 恒有计划；变不回归 409 而是诊断降级
            self.diagnostics.append("screen.record.unavailable:plan_build_failed")
            return
        self._inner = _ScreenRecordSession(
            self._executor,
            plan,
            finalize_wait_s=self._finalize_wait_s,
            clock=self._clock,
            watchdog_interval_s=self._watchdog_interval_s,
            commands=self._commands,
            diagnostics=self.diagnostics,
        )
        self._inner.start_first(self.diagnostics)

    def stop(self) -> None:
        """停 watchdog + pkill -INT 收尾封装 MP4 + pull 各段并 <1KB 判废。"""
        if self._inner is None:
            return
        self._inner.stop(self.diagnostics)
        self._inner.pull(self.diagnostics)

    def drain_diagnostics(self) -> list[str]:
        """取走新增诊断（广播后清空，避免 start/stop 重复广播同一批）。"""
        drained = list(self.diagnostics)
        self.diagnostics.clear()
        return drained


def create_session_recorder(
    session: Any, run_path: Path, *, adb: AdbClient | None = None
) -> SessionScreenRecorder:
    """record/start 时构造会话录屏器（SessionManager 经工厂注入，测试可替换）。"""
    return SessionScreenRecorder(
        session=session,
        adb=adb or AdbClient(),
        artifact_dir=run_path / "artifacts",
        case_name=run_path.name,
    )


def create_session_recorder_for_test(
    session: Any,
    run_path: Path,
    *,
    command_runner: Callable[[list[str], float | None], Any],
    clock: Callable[[], float] = time.monotonic,
    watchdog_interval_s: float = 0.0,
) -> SessionScreenRecorder:
    """测试工厂：注入假 adb 命令 runner + 注入时钟，驱动真实录屏内核。"""
    return SessionScreenRecorder(
        session=session,
        adb=AdbClient("/fake-adb"),
        artifact_dir=run_path / "artifacts",
        case_name=run_path.name,
        finalize_wait_s=0.0,
        watchdog_interval_s=watchdog_interval_s,
        clock=clock,
        command_runner=command_runner,
    )


def capture_screenshot_bytes(
    adb: AdbClient, serial: str, *, timeout: float = _SCREENCAP_TIMEOUT_S
) -> bytes:
    """``adb -s <serial> exec-out screencap -p`` → PNG 字节（契约 §16.2，real-only）。

    exec-out 直出二进制：不能用 ``AdbClient.run``（text/errors=replace 会损坏
    PNG），命令构造复用 ``AdbClient.command`` 后以二进制子进程执行。
    执行失败（非 0 返回码）或产物非 PNG（魔数不符）→ ``artifact_pull_failed``，
    ``details.phase="screenshot"``（契约 §16.2/§3）。

    实机验证修复（2026-11-03）：多屏设备（SM_F9560 折叠屏，实测）`screencap -p`
    会在 stdout 前置 `[Warning] Multiple displays…`（347 字节）再输出 PNG——
    按流内 PNG 签名定位剥离前缀（`-d <id>` 指定显示在该设备报 Failed，不可用）；
    找不到签名仍按拉流失败处理（真实非 PNG 场景不受影响）。
    """
    command = adb.command("exec-out", "screencap", "-p", serial=serial)
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            creationflags=ADB_CREATION_FLAGS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _pull_failed(f"screencap 执行失败: {exc}") from exc
    if completed.returncode != 0:
        stderr = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        raise _pull_failed(
            f"screencap 返回 {completed.returncode}（{''.join(command[:3])}…）: {stderr}"
        )
    data = completed.stdout or b""
    png_at = data.find(_PNG_MAGIC)
    if png_at < 0:
        raise _pull_failed(
            f"screencap 产物非 PNG（{len(data)} 字节，魔数不符）——设备侧输出异常"
        )
    return data[png_at:]


def _pull_failed(message: str) -> ApiError:
    return ApiError(
        "artifact_pull_failed",
        message,
        details={"phase": "screenshot"},
    )


def broadcast_recorder_diagnostics(session: Any, diagnostics: list[str]) -> None:
    """perfeval 采集诊断串 → SSE ``diagnostic`` 事件（契约 §10 原样透传）。

    perfeval 诊断串形如 ``screen.record.unavailable:pull_failed:1:...``——头部为
    机器可读 code，其余原文进 message（§10「diagnostic 消息 code 原样透传」）。
    """
    for raw in diagnostics:
        if not isinstance(raw, str) or not raw:
            continue
        code, _, rest = raw.partition(":")
        session.bus.publish(
            "diagnostic",
            code=code,
            severity="warn",
            message=raw if not rest else rest,
            detail={"raw": raw},
        )


def broadcast_new_recorder_diagnostics(session: Any, recorder: Any) -> None:
    """广播录制器自上次广播以来的新增诊断（幂等，见 drain_diagnostics）。"""
    drain = getattr(recorder, "drain_diagnostics", None)
    broadcast_recorder_diagnostics(session, drain() if callable(drain) else [])
