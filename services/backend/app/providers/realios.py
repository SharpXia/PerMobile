"""realios provider（R24 契约 §26）：真机 iOS 设备面（xcrun devicectl）。

与 ``providers/simulator.py``（simctl 源）并列的真机面：real 模式聚合面（§26.1）
下 Android usb|wifi 行（perfeval adb）+ 本 provider 的真机 iOS usb|wifi 行 +
simulator 行同列表出行。设备行 platform="ios"、connection 按 devicectl
transportType 映射（localNetwork→wifi、wired→usb）、**profile.simulator 恒显式
False**（capability_matrix.device_profile 的 ``simulator=None`` 推导是
ios→True——真机行漏传会把 §20.3 判定输入写错，§26.1 修订注）。

- 设备枚举/过滤/映射：``devicectl.parse_devices_json``（§26.1 三过滤规则 +
  tunnel 唯一在线判据）；
- 应用列表（§26.2）：``device info apps`` + ``device info processes`` 两命令
  按 bundle 目录合成；``isForeground`` 真机通道恒 null（不猜，§26.2）；
- launch（§26.2 launchOnCreate 真机形态）：``device process launch`` → pid；
- sim-boot：真机行无 boot 语义 → §26.1 golden 错误体（validation_error）；
- profile：usb/wifi 按连接、simulator=False、gpuSource/cpuCores/refreshRateHz
  devicectl 不暴露 → null（不猜，§26.1 映射规则）。

命令执行面经 ``devicectl.run_devicectl*``（runner 注入面测试可替换）；xcrun
本体缺失 → 503 ``xctrace_unavailable``（details.reason="devicectl_missing"，
§3 §26 新增行，命名与 S2 §3 契约同步冻结）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, TypeVar

from . import devicectl, xctrace
from .base import DeviceProvider
from ..errors import ApiError
from ..registry import available_metric_ids
from ..services.capability_matrix import device_profile, wire_profile

_T = TypeVar("_T")


def _probe_with_retry(
    attempt: Callable[[], _T | None],
    *,
    what: str,
    attempts: int,
    retry_delay_s: float,
    sleep: Callable[[float], None],
) -> _T:
    """采集窗内探测短重试（§26.4 锁屏瞬时失败同源语义，prewarm 同范式）。

    ``attempt`` 约定：命令/解析失败 → 返回 None（瞬时态，重试）；xcrun 本体
    缺失等**非瞬时态**直接 raise ApiError（透传，不重试——§3 两态区分）。
    重试耗尽 → ``ApiError(XCTRACE_UNAVAILABLE,
    details.reason="devicectl_probe_failed", details.probe=what)``——调用方
    按「无数据」诊断处置（§26.5），不得消费为权威负读数（离线/进程不存在）。
    """
    total = max(1, attempts)
    for index in range(total):
        result = attempt()
        if result is not None:
            return result
        if index < total - 1:
            sleep(retry_delay_s)
    raise ApiError(
        xctrace.XCTRACE_UNAVAILABLE,
        f"devicectl {what} 探测重试耗尽（{total} 次）",
        details={"reason": "devicectl_probe_failed", "probe": what, "attempts": total},
    )


def _run_or_raise_json(
    *args: str,
    timeout: float = 30.0,
    runner: Callable[[list[str], float], Any] | None = None,
) -> str:
    """devicectl JSON 命令 → JSON 文本；xcrun 缺失 → 503 xctrace_unavailable。"""
    try:
        return devicectl.run_devicectl_json(*args, timeout=timeout, runner=runner)
    except FileNotFoundError as exc:
        raise ApiError(
            xctrace.XCTRACE_UNAVAILABLE,
            "xcrun devicectl 不可用（命令行工具缺失或非 macOS 环境）",
            details={"reason": "devicectl_missing"},
        ) from exc


class RealIOSProvider(DeviceProvider):
    """真机 iOS 设备/应用枚举（R24 契约 §26.1/§26.2）。"""

    mode = "real"  # §26.1：real 模式聚合面（Android/iOS 真机/simulator 同列表）

    def __init__(
        self,
        *,
        devicectl_runner: Callable[[list[str], float], Any] | None = None,
    ) -> None:
        # devicectl 命令执行注入面（测试假 runner 驱动，无需真机在网）
        self._runner = devicectl_runner
        # 装机/进程表短缓存（一次窗口/一次列表内合成；真机面变更低频）
        self._apps_cache: dict[str, list[dict[str, Any]]] = {}

    # ---- 设备（§26.1）----

    def list_devices(self) -> list[dict[str, Any]]:
        stdout = _run_or_raise_json(
            "list", "devices", timeout=30.0, runner=self._runner
        )
        devices = devicectl.parse_devices_json(stdout)
        for device in devices:
            # profile 由行内 connection 直接构造（不得经 get_profile 回查设备，
            # 否则 list_devices↔get_profile 互调递归）
            device["profile"] = wire_profile(
                self._profile_for_connection(device["connection"])
            )
        return devices

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        for device in self.list_devices():
            if device["id"] == device_id:
                return device
        return None

    @staticmethod
    def _profile_for_connection(connection: str) -> dict[str, Any]:
        """§26.1 profile：usb/wifi 按连接、simulator=False 恒显式、读数 null 不猜。"""
        return device_profile(
            usb=connection == "usb",
            wifi=connection == "wifi",
            simulator=False,  # 真机行恒 False——None 推导会按 ios 误判 True（§26.1）
            platform="ios",
            gpu_source=None,  # devicectl 不暴露（§26.1 不猜）
            cpu_cores=None,
            refresh_rate_hz=None,
        )

    def get_profile(self, device_id: str) -> dict[str, Any]:
        """§26.1 判定输入：设备在网按其连接，不在网按双模式可能（usb|wifi）。"""
        device = self.get_device(device_id)
        if device is not None:
            return self._profile_for_connection(device["connection"])
        return device_profile(
            usb=True,
            wifi=True,
            simulator=False,
            platform="ios",
            gpu_source=None,
            cpu_cores=None,
            refresh_rate_hz=None,
        )

    # ---- 应用（§26.2）----

    def list_apps(self, device_id: str) -> dict[str, Any] | None:
        """装机清单 ∩ 运行中进程（bundle 目录匹配）；apps **或 processes** 命令
        失败 → None（404 语义）——processes 失败不得按空表消费（那会把全部 app
        伪造成 running=false 负读数，2026-11-16 审读修订）。"""
        apps = self._installed_apps(device_id)
        if apps is None:
            return None
        processes = self._device_processes(device_id)
        if processes is None:
            return None
        matched = devicectl.match_app_processes(apps, processes)
        rows: list[dict[str, Any]] = []
        for app in apps:
            entries = matched.get(app["packageName"], [])
            main = next((e for e in entries if e["isMain"]), None)
            pid = main["pid"] if main else None
            rows.append(
                {
                    "packageName": app["packageName"],
                    "name": app["name"],
                    "pid": pid,
                    # running↔pid 一致（§26.2 golden 注）：主进程在场才算 running；
                    # 仅 Extension 存活时 processes 照实出行（isMain=false）但
                    # running=false——Extension 不作 attach/采样目标
                    "running": pid is not None,
                    # 真机通道恒 null（§26.2：前台信号源未勘察，不猜）
                    "isForeground": None,
                    "processes": [
                        {"pid": e["pid"], "name": e["name"], "isMain": e["isMain"]}
                        for e in entries
                    ],
                }
            )
        return {"deviceId": device_id, "apps": rows}

    def _installed_apps_once(self, device_id: str) -> list[dict[str, Any]] | None:
        """单次 apps 命令严格探测：命令失败（空输出/结构不识别）→ None（瞬时态，
        采集窗重试语义）；xcrun 本体缺失 → raise ApiError（非瞬时态透传）；
        成功 → 应用行列表（可空——空表是权威读数，不是失败）。"""
        stdout = _run_or_raise_json(
            "device", "info", "apps", "--device", device_id,
            timeout=60.0, runner=self._runner,
        )
        if not stdout:
            return None
        apps = devicectl.parse_apps_json(stdout)
        return None if apps is None else apps

    def _installed_apps(self, device_id: str) -> list[dict[str, Any]] | None:
        """装机应用（apps 命令；进程表每次现探）。失败 → None（调用方 404 语义）。"""
        try:
            apps = self._installed_apps_once(device_id)
        except ApiError:
            return None
        if apps is None:
            return None
        self._apps_cache[device_id] = apps
        return apps

    def _device_processes_once(self, device_id: str) -> list[dict[str, Any]] | None:
        """单次 processes 命令严格探测：失败 → None；xcrun 缺失 → raise；成功 →
        进程行列表（可空）。"""
        stdout = _run_or_raise_json(
            "device", "info", "processes", "--device", device_id,
            timeout=60.0, runner=self._runner,
        )
        if not stdout:
            return None
        return devicectl.parse_processes_json(stdout)

    def _device_processes(self, device_id: str) -> list[dict[str, Any]] | None:
        try:
            return self._device_processes_once(device_id)
        except ApiError:
            return None

    # ---- 采集窗内探测（strict：失败≠权威负读数；§26.4 锁屏瞬时失败 → 短重试）----
    # 采集链（realios_sampler）专用：命令失败重试耗尽 → ApiError(
    # xctrace_unavailable, reason="devicectl_probe_failed")，调用方按「无数据」
    # 诊断处置（§26.5），不把探测失败消费成 tunnel 离线/进程不存在/null 点。

    def find_device_strict(
        self,
        device_id: str,
        *,
        attempts: int = 3,
        retry_delay_s: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> dict[str, Any] | None:
        """设备枚举严格探测：返回设备行 / None（命令成功且确不在表内=权威离线）。

        与 ``get_device`` 的差别：后者把「命令失败」与「无此设备」同归 None
        （聚合面降级语义，保持不变）；本方法供采集窗在线检查使用——探测失败
        重试耗尽 → raise（§26.4 锁屏「重试耗尽 → §26.5 诊断」同源）。
        """
        rows = _probe_with_retry(
            lambda: self._probe_devices_once(),
            what="list-devices",
            attempts=attempts,
            retry_delay_s=retry_delay_s,
            sleep=sleep,
        )
        return next((row for row in rows if row.get("id") == device_id), None)

    def _probe_devices_once(self) -> list[dict[str, Any]] | None:
        """单次 list devices 严格探测：命令失败（空输出）→ None；成功 → 行列表
        （可空，未经 profile 装饰——采集窗在线检查只消费 state）。"""
        stdout = _run_or_raise_json(
            "list", "devices", timeout=30.0, runner=self._runner
        )
        if not stdout:
            return None
        return devicectl.parse_devices_json(stdout)

    def resolve_process_strict(
        self,
        device_id: str,
        package_name: str,
        *,
        attempts: int = 3,
        retry_delay_s: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> dict[str, Any] | None:
        """bundle → 主进程 {pid, name}（strict 版）：探测失败重试耗尽 → raise。

        返回 None 仅表示**探测成功后的权威负读数**（未安装/未运行）——调用方
        据此走 app.process.not_found + null 点（§26.5）；apps/processes 命令
        失败按重试耗尽 raise（不伪造 not_found）。
        """
        apps = _probe_with_retry(
            lambda: self._installed_apps_once(device_id),
            what="info-apps",
            attempts=attempts,
            retry_delay_s=retry_delay_s,
            sleep=sleep,
        )
        processes = _probe_with_retry(
            lambda: self._device_processes_once(device_id),
            what="info-processes",
            attempts=attempts,
            retry_delay_s=retry_delay_s,
            sleep=sleep,
        )
        return devicectl.resolve_main_process(apps, processes, package_name)

    def resolve_process(
        self, device_id: str, package_name: str
    ) -> dict[str, Any] | None:
        """bundle → 主进程 {pid, name}（xctrace --attach 目标名，§26.2）。

        非采集窗调用面（容错版：apps/processes 命令失败 → None，与未安装/
        未运行同归 None）。采集窗内一律走 ``resolve_process_strict``（失败
        语义分离，探测失败不伪造 not_found）。
        """
        apps = self._installed_apps(device_id)
        if apps is None:
            return None
        processes = self._device_processes(device_id)
        if processes is None:
            return None
        return devicectl.resolve_main_process(apps, processes, package_name)

    def launch_app(self, device_id: str, bundle_id: str) -> dict[str, Any]:
        """launchOnCreate 真机形态（§26.2）：``device process launch`` → pid。

        返回 ``{"component", "error", "pid"}``（不抛异常，失败由 SessionManager
        广播 phase failed + diagnostic——SimulatorProvider.launch_app 同形状）。
        """
        try:
            stdout = _run_or_raise_json(
                "device", "process", "launch", "--device", device_id, bundle_id,
                timeout=60.0, runner=self._runner,
            )
        except ApiError as exc:
            return {"component": None, "error": f"devicectl launch failed: {exc.message}"}
        pid = devicectl.parse_launch_json(stdout)
        if pid is None:
            return {
                "component": None,
                "error": f"devicectl launch failed: {stdout.strip()[:200] or '无 pid 输出'}",
            }
        return {"component": bundle_id, "error": None, "pid": pid}

    # ---- sim-boot（§26.1 golden 错误体：真机行无 boot 语义）----

    def boot_device(self, device_id: str) -> dict[str, Any]:
        device = self.get_device(device_id)
        connection = device["connection"] if device else None
        raise ApiError(
            "validation_error",
            "sim-boot 仅对 simulator 连接设备有意义"
            "（usb|wifi 连接的 Android/iOS 真机行无 boot 语义，契约 §23.1/§26.1）",
            details={"deviceId": device_id, "connection": connection},
        )

    def device_details(self, device_id: str) -> dict[str, Any] | None:
        """``device info details`` 读数（非 §26.1 契约字段源；live smoke/诊断用）。"""
        try:
            stdout = _run_or_raise_json(
                "device", "info", "details", "--device", device_id,
                timeout=60.0, runner=self._runner,
            )
        except ApiError:
            return None
        return devicectl.parse_details_json(stdout)

    def list_metric_ids(self) -> list[str]:
        # §26.1 聚合面：real 模式全集（含 §26.3 双指标；Android 行由 §20.3
        # 第 1 条对 ios 设备判 platform_unsupported，见 registry_body+profile）
        return available_metric_ids(self.mode)
