"""深度分析定时截图（M7 R38，契约 §22.3，[F§7] 超低消耗定时截图 2s 一次）。

real-only 循环：``exec-out screencap`` 周期截图落 run 目录 ``artifacts/``（命名
``screenshot-<tsMs>.png`` 同 §16.2，§17 索引按 ``kind:"screenshot"`` 归类）。

- 命令构造复用 ``screen_capture.capture_screenshot_bytes``（§16.2 单帧截图同一
  事实源，不另写 adb 交互）；
- 首帧由端点同步截取（设备故障当场 502 artifact_pull_failed），后续帧由本循环
  线程异步产出；单帧失败 → 整循环终止并广播 ``diagnostic``（severity=error，
  code 沿契约 §3 ``artifact_pull_failed``）；
- **数量上限防滥用**（M7 实现期约定，如实声明）：单次循环 ``MAX_CAPTURES`` 帧
  封顶（默认间隔 2s → 最长约 10 分钟，与 R50 深度分析「≤10min」窗口同量纲），
  达上限循环自然结束并广播 info 诊断；重复 POST 以新 intervalMs 幂等重启
  （先停旧循环，不叠加多循环）；
- 生命周期：仅 ``recording`` 可启动；随 ``record/stop``/收口（save/discard/
  delete）自动终止（SessionManager ``_stop_timed_screenshot`` 接线）；
- synthetic 模式 501 not_implemented + SSE ``timed-screenshot.unavailable``
  （severity=info）——§16 截图/录屏 synthetic 诊断同哲学。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable

from ..errors import ApiError
from ..sse import now_ms
from .screen_capture import capture_screenshot_bytes

logger = logging.getLogger("pd-backend.timed-screenshot")

DEFAULT_INTERVAL_MS = 2000  # [F§7]：超低消耗定时截图，2s 一次
MIN_INTERVAL_MS = 500  # 契约 §22.3 允许区间下限
MAX_INTERVAL_MS = 60000  # 契约 §22.3 允许区间上限
# 数量上限防滥用（契约 §22.3 未固化上限，M7 实现期约定并如实声明）：
# 默认 2s × 300 帧 ≈ 10 分钟，与 R50 深度分析「≤10min」窗口同量纲。
MAX_CAPTURES = 300

UNAVAILABLE_CODE = "timed-screenshot.unavailable"


def parse_interval_ms(raw: Any) -> int:
    """``intervalMs`` 校验（契约 §22.3）：缺省 2000，允许 500–60000 正整数。

    越界/非正整数 → 400 validation_error（details.field="intervalMs"）。
    """
    if raw is None:
        return DEFAULT_INTERVAL_MS
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ApiError(
            "validation_error",
            f"intervalMs 必须是正整数（{MIN_INTERVAL_MS}–{MAX_INTERVAL_MS}）",
            details={"field": "intervalMs"},
        )
    if not MIN_INTERVAL_MS <= raw <= MAX_INTERVAL_MS:
        raise ApiError(
            "validation_error",
            f"intervalMs 超出允许区间 {MIN_INTERVAL_MS}–{MAX_INTERVAL_MS}，收到 {raw}",
            details={"field": "intervalMs"},
        )
    return raw


class TimedScreenshotLoop:
    """周期 screencap → artifacts 落盘线程（LogStreamChannel 同款生命周期）。

    ``capture`` 注入面：``() -> bytes``（缺省 ``capture_screenshot_bytes(adb,
    serial)`` 闭包）；测试注入假 capture 驱动真实循环/落盘/终止逻辑，无需真机。
    ``sleep`` 注入面：``(seconds) -> bool``（缺省 ``stop_event.wait``——返回 True
    即 stop 已触发，提前退出等待）。
    """

    def __init__(
        self,
        session: Any,
        *,
        interval_ms: int,
        artifact_dir: Path,
        capture: Callable[[], bytes] | None = None,
        sleep: Callable[[float], bool] | None = None,
        max_captures: int = MAX_CAPTURES,
    ) -> None:
        self.session = session
        self.interval_ms = int(interval_ms)
        self._artifact_dir = artifact_dir
        self._max_captures = max(1, int(max_captures))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._sleep = sleep if sleep is not None else self._stop_event.wait
        if capture is not None:
            self._capture = capture
        else:
            from perfeval.collectors.android.adb import AdbClient

            adb = AdbClient()
            serial = str(session.device_id)
            self._capture = lambda: capture_screenshot_bytes(adb, serial)
        # 已产出文件名（测试与后续状态展示用；顺序即产出序）
        self.captures: list[str] = []
        self.stop_reason: str | None = None

    # ---- 生命周期 ----

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"timed-screenshot-{self.session.id}", daemon=True
        )
        self._thread.start()

    def capture_first(self) -> None:
        """首帧同步截取（POST 请求内执行）：失败按原码冒泡（端点 502/§3 原码）。

        诊断广播与异步路径同一实现（``_capture_once``）；冒泡的 ``ApiError`` 由
        router/统一错误体转换为契约错误响应。
        """
        self._capture_once(reraise=True)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---- 循环体 ----

    def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                if len(self.captures) >= self._max_captures:
                    self.stop_reason = "max_captures"
                    self.session.bus.publish(
                        "diagnostic",
                        code=UNAVAILABLE_CODE,
                        severity="info",
                        message=(
                            f"定时截图达到数量上限（{self._max_captures} 帧），循环自动结束"
                            "（防滥用上限，可重新 POST 重启）"
                        ),
                        detail={"captures": len(self.captures)},
                    )
                    break
                # 先等间隔再截：首帧已由 capture_first 同步产出，循环帧严格按
                # interval 节拍产出——若先截后等，首帧与同步帧会同 ms 互覆
                # （产物名 screenshot-<tsMs>.png 以毫秒为唯一性，§17 索引正则口径）。
                # wait 返回 True = stop 已触发（提前退出，不再截下一帧）。
                if self._sleep(self.interval_ms / 1000.0):
                    break
                if self._stop_event.is_set():
                    break
                self._capture_once()
        finally:
            logger.info(
                "会话 %s 定时截图循环结束（%d 帧，原因=%s）",
                self.session.id,
                len(self.captures),
                self.stop_reason or ("stopped" if self._stop_event.is_set() else "done"),
            )

    def _capture_once(self, *, reraise: bool = False) -> None:
        try:
            png = self._capture()
        except ApiError as exc:
            # 单帧截取失败 → 整循环终止 + error 诊断（契约 §22.3）；首帧同步路径
            # （reraise=True）按原码冒泡（artifact_pull_failed → 502）
            self.stop_reason = f"capture_failed:{exc.code}"
            self.session.bus.publish(
                "diagnostic",
                code=exc.code,
                severity="error",
                message=f"定时截图单帧截取失败，循环终止：{exc.message}",
                detail={"phase": "screenshot"},
            )
            self._stop_event.set()
            if reraise:
                raise
            return
        except Exception as exc:  # noqa: BLE001 —— 任意异常同样终止循环并如实广播
            self.stop_reason = "capture_failed:internal"
            self.session.bus.publish(
                "diagnostic",
                code="internal_error",
                severity="error",
                message=f"定时截图单帧截取异常，循环终止：{exc}",
                detail={"phase": "screenshot"},
            )
            self._stop_event.set()
            if reraise:
                raise
            return
        name = f"screenshot-{now_ms()}.png"
        try:
            self._artifact_dir.mkdir(parents=True, exist_ok=True)
            (self._artifact_dir / name).write_bytes(png)
        except OSError as exc:
            self.stop_reason = "write_failed"
            self.session.bus.publish(
                "diagnostic",
                code="internal_error",
                severity="error",
                message=f"定时截图落盘失败，循环终止：{exc}",
                detail={"phase": "screenshot"},
            )
            self._stop_event.set()
            if reraise:
                raise ApiError(
                    "internal_error", f"定时截图落盘失败：{exc}"
                ) from exc
            return
        self.captures.append(name)
