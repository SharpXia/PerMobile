"""simulator provider（M-iOS，契约 §23）：iOS Simulator 设备面（xcrun simctl）。

与 ``providers/real.py``（perfeval adb）并列的第三模式：

- 设备枚举：``simctl list devices --json``（仅 iOS 运行时；watchOS/tvOS 行过滤），
  Booted→online、其余 offline——**Shutdown 设备照常出行**（真机发现语义是
  「接上的就能看到」，模拟器同语义；offline 设备建会话走既有 device_not_online）；
- 应用列表：``simctl listapps``（装机应用）+ ``launchctl list``（运行中 pid）+
  SpringBoard visibility 日志（前台 best-effort，无信号 null 不猜）；
- launch_app：``simctl launch <udid> <bundle>``（launchOnCreate 接线，契约 §6.1）；
- profile：simulator=True、usb/wifi=False、gpuSource=None（模拟器无 kgsl）、
  cpuCores=宿主核数（宿主口径声明）、refreshRateHz=None（模拟器无公开读数）。

命令执行面统一经 ``simctl.run_simctl``（``runner`` 注入面测试可替换，fixture
驱动单测不依赖模拟器常驻）。xcrun 本体不可用（FileNotFoundError）→ 503
``simctl_unavailable``（§3 §23 新增行，与 adb_not_found 同语义分离先例）。
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Callable

from . import simctl
from .base import DeviceProvider
from ..errors import ApiError
from ..registry import available_metric_ids
from ..services.capability_matrix import device_profile, wire_profile

# visibility 前台探测窗口（秒）：覆盖「当前前台应用」的最近一次切换信号
_VISIBILITY_WINDOW_S = 300.0
_VISIBILITY_TIMEOUT_S = 20.0


def _run_or_raise(
    *args: str,
    timeout: float = 15.0,
    runner: Callable[[list[str], float], Any] | None = None,
) -> str:
    """simctl 执行 → stdout；xcrun 缺失 → 503 simctl_unavailable，其余失败 → None 语义。"""
    try:
        completed = simctl.run_simctl(*args, timeout=timeout, runner=runner)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ApiError(
            simctl.SIMCTL_UNAVAILABLE, f"xcrun simctl 执行失败：{exc}"
        ) from exc
    if completed is None:
        raise ApiError(
            simctl.SIMCTL_UNAVAILABLE,
            "xcrun simctl 不可用（命令行工具缺失或非 macOS 环境）",
        )
    if completed.returncode != 0:
        return ""
    return completed.stdout or ""


class SimulatorProvider(DeviceProvider):
    """iOS Simulator 设备/应用枚举（M-iOS，契约 §23.1）。"""

    mode = "simulator"

    def __init__(
        self,
        *,
        host_cores: int | None = None,
        simctl_runner: Callable[[list[str], float], Any] | None = None,
    ) -> None:
        # 宿主核数（宿主口径声明的一部分；None=探测失败不出 cpu 字段值）
        self._host_cores = host_cores if host_cores is not None else (os.cpu_count() or None)
        # simctl 命令执行注入面（测试假 runner 驱动解析逻辑，无需模拟器常驻）
        self._runner = simctl_runner
        # listapps 结果按 udid 缓存（装机清单静态；进程/前台每次现探）
        self._apps_cache: dict[str, list[dict[str, Any]]] = {}

    # ---- 设备 ----

    def list_devices(self) -> list[dict[str, Any]]:
        stdout = _run_or_raise(
            "list", "devices", "--json", timeout=20.0, runner=self._runner
        )
        devices = simctl.parse_devices_json(stdout, host_cores=self._host_cores)
        for device in devices:
            device["profile"] = wire_profile(self.get_profile(device["id"]))
        return devices

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        for device in self.list_devices():
            if device["id"] == device_id:
                return device
        return None

    def get_profile(self, device_id: str) -> dict[str, Any] | None:
        """M-iOS profile：simulator 模式标志 + 宿主核数（§23.1）。"""
        return device_profile(
            usb=False,
            wifi=False,
            simulator=True,
            platform="ios",
            gpu_source=None,  # 模拟器无 kgsl GPU 利用率源（§23.2 如实声明）
            cpu_cores=self._host_cores,
            refresh_rate_hz=None,  # 模拟器无公开刷新率读数，不猜
        )

    # ---- 启动（§23.1 POST /api/devices/{deviceId}/sim-boot）----

    def boot_device(self, device_id: str) -> dict[str, Any]:
        """``simctl boot`` + ``bootstatus -b``（阻塞至启动完成或超时）。

        已 Booted 设备幂等（boot 对已启动设备报错属正常，直接返回当前态）。
        bootstatus 超时（未在时限内 Booted）→ 503 simctl_unavailable（如实报
        未就绪，不伪装在线）。
        """
        device = self.get_device(device_id)
        if device is None:
            raise ApiError("device_not_found", f"device {device_id} 不存在")
        already = device["state"] == "online"
        if already:
            # ``simctl boot``/``bootstatus`` are unnecessary for an already
            # booted device.  Returning the enumerated state preserves the
            # endpoint's idempotent contract even if a transient bootstatus
            # probe fails while the simulator remains usable.
            return {
                "deviceId": device_id,
                "action": "boot",
                "state": device["state"],
            }
        try:
            simctl.run_simctl("boot", device_id, timeout=30.0, runner=self._runner)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ApiError(
                simctl.SIMCTL_UNAVAILABLE, f"xcrun simctl boot 执行失败：{exc}"
            ) from exc
        # Keep the bootstatus return code here.  ``_run_or_raise`` maps every
        # non-zero simctl exit to an empty string for callers that intentionally
        # treat command failures as "no data"; for bootstatus, however, an empty
        # string is also a valid successful output, so using that helper would
        # turn a failed/timeout boot into a false success.
        try:
            completed = simctl.run_simctl(
                "bootstatus", device_id, "-b", timeout=180.0, runner=self._runner
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ApiError(
                simctl.SIMCTL_UNAVAILABLE, f"xcrun simctl bootstatus 执行失败：{exc}"
            ) from exc
        if completed is None or completed.returncode != 0:
            raise ApiError(
                simctl.SIMCTL_UNAVAILABLE,
                f"simulator {device_id} 启动未完成（bootstatus 失败）",
            )
        stdout = completed.stdout or ""
        booted = _bootstatus_ok(stdout)
        state = self.get_device(device_id)
        final_state = state["state"] if state else "offline"
        if not booted and final_state != "online":
            raise ApiError(
                simctl.SIMCTL_UNAVAILABLE,
                f"simulator {device_id} 启动未完成（bootstatus 超时）",
            )
        return {
            "deviceId": device_id,
            "action": "boot",
            "state": final_state,
        }

    # ---- 应用 ----

    def list_apps(self, device_id: str) -> dict[str, Any] | None:
        """契约 §4 应用列表：装机清单 ∩ 运行中 pid ∪ 前台 best-effort。"""
        apps = self._installed_apps(device_id)
        if apps is None:
            return None
        running = self._running_bundles(device_id)
        foreground = self._foreground_bundle(device_id)
        rows: list[dict[str, Any]] = []
        for app in apps:
            package = app["packageName"]
            pid = running.get(package)
            rows.append(
                {
                    "packageName": package,
                    "name": app["name"],
                    "pid": pid,
                    "running": pid is not None,
                    "isForeground": (package == foreground) if foreground else None,
                    # 模拟器单进程模型（Extension 进程枚举 M-iOS 未落地，如实缺省）
                    "processes": (
                        [{"pid": pid, "name": package, "isMain": True}] if pid is not None else []
                    ),
                }
            )
        return {"deviceId": device_id, "apps": rows}

    def _installed_apps(self, device_id: str) -> list[dict[str, Any]] | None:
        """装机应用（listapps 静态清单缓存）；命令失败 → None（调用方 404 语义）。"""
        cached = self._apps_cache.get(device_id)
        if cached is not None:
            return cached
        try:
            stdout = _run_or_raise("listapps", device_id, timeout=30.0, runner=self._runner)
        except ApiError:
            return None
        if not stdout:
            return None
        apps = simctl.parse_listapps(stdout)
        self._apps_cache[device_id] = apps
        return apps

    def _running_bundles(self, device_id: str) -> dict[str, int]:
        """launchctl list UIKitApplication 行 → {bundle: pid}；失败空表（不猜）。"""
        try:
            stdout = _run_or_raise(
                "spawn", device_id, "launchctl", "list", timeout=20.0,
                runner=self._runner,
            )
        except ApiError:
            return {}
        return simctl.parse_launchctl_uikit(stdout)

    def _foreground_bundle(self, device_id: str) -> str | None:
        """统一日志 FBSystemService activation 行 → 当前前台 bundle（best-effort）。

        实测选型（2026-11-04，iOS 27.0 模拟器截图比对）：
        - `Activation request for <bundle>`（FBSystemService）——每次 UI 应用
          激活/复激活触发、widget renderer 不触发，与视觉前台一致 → **主判据**；
        - `Setting process visibility` 行——视觉前台但事件 Background（权限弹窗
          即触发），已否决（simctl.parse_activation_bundles 注保留否决依据）。

        局限（如实声明）：App 经 Home 退后台且无新应用激活时信号滞留——
        activation 只承载「激活」不承载「退前台」；窗口内无信号 → None（不猜）。
        """
        try:
            stdout = _run_or_raise(
                "spawn", device_id, "log", "show",
                "--last", f"{int(_VISIBILITY_WINDOW_S)}s",
                "--style", "ndjson",
                "--predicate",
                'eventMessage CONTAINS "Activation request for"',
                timeout=_VISIBILITY_TIMEOUT_S,
                runner=self._runner,
            )
        except ApiError:
            return None
        if not stdout:
            return None
        return simctl.foreground_from_activation(
            simctl.parse_activation_bundles(stdout)
        )

    def launch_app(self, device_id: str, bundle_id: str) -> dict[str, Any]:
        """launchOnCreate（契约 §6.1）：``simctl launch <udid> <bundle>``。

        返回 ``{"component": str | None, "error": str | None}``（不抛异常，失败由
        SessionManager 广播 phase failed + diagnostic，会话保持 monitoring）。
        """
        try:
            stdout = _run_or_raise(
                "launch", device_id, bundle_id, timeout=30.0, runner=self._runner
            )
        except ApiError as exc:
            return {"component": None, "error": f"simctl launch failed: {exc.message}"}
        pid = _parse_launch_pid(stdout)
        if pid is None:
            return {"component": None, "error": f"simctl launch failed: {stdout.strip()[:200]}"}
        return {"component": bundle_id, "error": None, "pid": pid}

    def list_metric_ids(self) -> list[str]:
        return available_metric_ids(self.mode)


def _parse_launch_pid(stdout: str) -> int | None:
    """``simctl launch`` 输出 ``com.apple.Preferences: 81296`` → 81296。"""
    for token in reversed(stdout.split()):
        if token.isdigit():
            return int(token)
    return None


def _bootstatus_ok(stdout: str) -> bool:
    """bootstatus 成功时输出含 ``Device already booted`` 或为空。"""
    return "already booted" in stdout.lower() or stdout.strip() == ""
