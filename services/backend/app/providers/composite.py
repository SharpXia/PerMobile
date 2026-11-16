"""CompositeProvider（装配流 S4 合流点，契约 §26.1 装配语义 / §23.1 聚合 sim-boot）。

``--provider real`` 的聚合设备面：一个 provider 同时聚合三设备源——

- adb Android 真机（``providers/real.py`` RealProvider，perfeval adb，含 WiFi 行）；
- devicectl iOS 真机（``providers/realios.py`` RealIOSProvider——S1 执行面：
  设备枚举/应用面/launch/profile 全部经 ``providers/devicectl.py``，采集执行在
  ``services/realios_sampler.py`` RealIOSCollector，经 collector_service 的
  real-iOS 分支分流）；
- simctl iOS 模拟器（``providers/simulator.py`` SimulatorProvider）。

装配规则（全局决策，不新增 provider 枚举值——config.VALID_PROVIDERS/run.py choices 不动）：

- ``--provider real`` → CompositeProvider（打包态 services/desktop/main.js 恒
  ``--provider real``，零改动即见 iOS 设备）；
- ``--provider simulator/synthetic`` 保持单源逐字段不变（main.py 装配不动）；
- ``--provider auto`` 维持探测择一不动（规避 ADR-0002 记录的 GUI 精简 PATH 下
  auto 误判锁死回归）。

关键语义：

- **单源缺失优雅降级**：adb 不在（adb_not_found/adb_devices_failed）、xcrun 不在
  （simctl_unavailable/xctrace_unavailable/FileNotFoundError）、devicectl 命令失败
  → 该源静默空，不炸设备面（GET /api/devices 仍 200，只出其余源设备行）；
- **设备 id 跨源冲突防护**：同 id 行只保留先到源（源优先序 adb Android →
  devicectl 真机 → simctl 模拟器），后到源丢弃，不出现双行；devicectl 源侧的
  simulated/watchOS/未配对行在 ``devicectl.parse_devices_json``（§26.1 三过滤
  规则）即拦下——模拟器归 simctl 源，聚合面无 devicectl/simctl 双源重复行与
  watchOS 行泄漏；
- **设备源通道（判定 mode 推导，§20.3/§23.1）**：provider.mode 只能表达装配模式
  （聚合面恒 "real"），判定与落盘的 mode/平台按**设备源通道**推导：
  ios+simulator 连接 → "simulator" 通道；其余（Android 行、真机 iOS usb|wifi 行）
  → "real" 通道。派生点：``/api/metrics``（main.py）、会话 metric_unavailable
  （session_manager）、PATCH /metrics 平台门（sessions.py）、落盘 platform
  （perfeval.summary.v1/timeline）。单源 provider 无 ``device_mode`` 方法，
  duck-type 回退 ``provider.mode``（simulator/synthetic 逐字段不变）；
- **设备级 duck-typed 方法按设备源分发**：wifi_enable/wifi_connect/speedtest/
  adb_serial/app_version 仅 adb 源实现（iOS 设备返回契约错误码/None 短路，
  聚合面不产生 AttributeError→500）；boot_device/launch_app/list_apps/
  get_profile 按拥有源分发（RealIOSProvider/SimulatorProvider 各自实现）。

依赖注记（合流期）：RealIOSProvider 的 xcrun 缺失路径引用错误码
``xctrace_unavailable``（S2 §3 错误表行；constants 在 providers/xctrace.py），
须与 errors.ERROR_STATUS 同步注册——未注册期间该源异常经本类 list_devices 的
单源降级兜住（不炸设备面），直接分发调用以 AttributeError→500 防护门兜底。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from ..errors import ApiError
from ..registry import available_metric_ids
from .base import DeviceProvider
from .realios import RealIOSProvider
from .simulator import SimulatorProvider

logger = logging.getLogger("pd-backend.provider.composite")

# §23.1/§26.1 golden 错误体（真机行 sim-boot 400 validation_error）
SIM_BOOT_UNSUPPORTED_MESSAGE = (
    "sim-boot 仅对 simulator 连接设备有意义"
    "（usb|wifi 连接的 Android/iOS 真机行无 boot 语义，契约 §23.1/§26.1）"
)

# devicectl JSON runner 注入面（(command, timeout) → CompletedProcess | None；
# RealIOSProvider 同面透传，测试以假 runner 驱动，无需真机在网）
DevicectlRunner = Any


# ---- 设备源通道推导（判定 mode / 落盘 platform 的单一事实源）----


def device_channel(device: dict[str, Any]) -> str:
    """设备行 → 判定通道（registry mode_available / capability_matrix 的 mode 入参）。

    - platform="ios" 且 connection="simulator" → "simulator"（§23.2 宿主口径双指标
      modes=("simulator",) 走模拟器通道可勾）；
    - 其余（Android usb|wifi 行、真机 iOS usb|wifi 行——§26.1 connection 按链路
      形态映射）→ "real"（§26.3 真机 xctrace 双指标 modes=("usb","wifi") 在
      real 通道 + 真机 profile 下交集判定通过）。
    """
    if device.get("platform") == "ios" and device.get("connection") == "simulator":
        return "simulator"
    return "real"


def channel_platform(channel: str, row_platform: str | None) -> str:
    """落盘平台（perfeval.summary.v1/timeline 的 platform 字段）推导。

    simulator 通道恒 "ios"；real 通道按设备行 platform（真机 iOS → "ios"、
    Android → "android"、行缺 platform → "android"）；synthetic（单源回退通道）
    行 platform="android" → "android"。聚合面 provider_mode="real" 不再把真机
    iOS 会话错标 android（此前 ``platform="ios" if provider_mode=="simulator"
    else "android"`` 的模式推导在聚合面失效——本函数为修正落点）。
    """
    if channel == "simulator":
        return "ios"
    return "ios" if row_platform == "ios" else "android"


def provider_device_mode(provider: Any, device_id: str) -> str:
    """provider 的判定 mode：duck-type ``device_mode(device_id)``（CompositeProvider
    实现按源通道），单源 provider 回退 ``provider.mode``（simulator/synthetic/
    real 单源判定与改造前逐字段一致；base.DeviceProvider 不扩抽象——与
    launch_app/app_version 的 duck-typing 注入哲学同）。"""
    getter = getattr(provider, "device_mode", None)
    if callable(getter):
        return getter(device_id)
    return provider.mode


class CompositeProvider(DeviceProvider):
    """三设备源聚合（--provider real 装配面）：单源降级 + id 冲突防护 + 按源分发。

    源优先序（id 冲突时先到源保留）：adb Android → devicectl iOS 真机 →
    simctl iOS 模拟器。三个源均可注入替换（测试 fixture 面）；缺省构造不触
    任何子进程（枚举在 list_devices 时进行）。
    """

    mode = "real"

    def __init__(
        self,
        *,
        android: DeviceProvider | None = None,
        ios_real: DeviceProvider | None = None,
        simulator: DeviceProvider | None = None,
        devicectl_runner: DevicectlRunner | None = None,
    ) -> None:
        if android is None:
            from .real import RealProvider  # 局部导入：装配失败面集中在本模块

            android = RealProvider()
        self._android = android
        # S1 执行面（§26.1/§26.2）：枚举/应用/launch/profile 全在 RealIOSProvider；
        # 本类只做聚合/降级/分发/通道推导，不重复实现 devicectl 命令面
        self._ios_real = ios_real if ios_real is not None else RealIOSProvider(
            devicectl_runner=devicectl_runner
        )
        self._simulator = simulator if simulator is not None else SimulatorProvider()
        self._sources: tuple[DeviceProvider, ...] = (
            self._android, self._ios_real, self._simulator,
        )
        # 设备 id → 拥有源（list_devices 时刷新；分发方法据此路由）
        self._owners: dict[str, DeviceProvider] = {}
        self._owner_lock = threading.Lock()

    @property
    def ios_real_source(self) -> DeviceProvider:
        """真机 iOS 源（RealIOSProvider；S1 采集链 resolve_process 复用入口）。"""
        return self._ios_real

    # ---- 设备枚举（聚合 + 降级 + 冲突防护）----

    def list_devices(self) -> list[dict[str, Any]]:
        devices: list[dict[str, Any]] = []
        owners: dict[str, DeviceProvider] = {}
        for source in self._sources:
            try:
                rows = source.list_devices()
            except Exception as exc:  # noqa: BLE001 —— 单源缺失静默降级（不炸设备面）
                logger.warning(
                    "聚合面设备源 %s 枚举失败（该源置空）：%s", type(source).__name__, exc
                )
                continue
            for row in rows or []:
                device_id = row.get("id")
                if not device_id:
                    continue
                if device_id in owners:
                    # 跨源 id 冲突防护：先到源保留（源优先序见类 docstring），
                    # 后到源丢弃——不出现双行，也不因冲突炸列表
                    logger.warning("聚合面设备 id 跨源冲突（保留先到源）：%s", device_id)
                    continue
                owners[device_id] = source
                devices.append(row)
        with self._owner_lock:
            self._owners = owners
        return devices

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        for device in self.list_devices():
            if device["id"] == device_id:
                return device
        return None

    def device_mode(self, device_id: str) -> str:
        """设备源通道（判定 mode；provider_device_mode duck-type 入口）。"""
        device = self.get_device(device_id)
        if device is None:
            return self.mode
        return device_channel(device)

    # ---- 按源分发（duck-typed 设备级方法；iOS 设备契约错误码，不 AttributeError→500）----

    def _owner_for(self, device_id: str) -> DeviceProvider | None:
        with self._owner_lock:
            known = device_id in self._owners
        if not known:
            self.list_devices()  # 刷新 ownership 表（幂等）
        with self._owner_lock:
            return self._owners.get(device_id)

    def _android_only(
        self, device_id: str, capability: str, diagnostic_code: str
    ) -> DeviceProvider:
        """非 adb 源设备调用 Android-only 能力的统一 500 防护：契约错误码。"""
        owner = self._owner_for(device_id)
        if owner is self._android and owner is not None:
            return owner
        device = self.get_device(device_id)
        if device is None:
            raise ApiError("device_not_found", f"device {device_id} 不存在")
        raise ApiError(
            "not_implemented",
            f"{capability} 为 Android 真机能力（adb），iOS 设备（{device_channel(device)}"
            f" 通道）未实现",
            details={
                "diagnostic": {
                    "code": diagnostic_code,
                    "severity": "info",
                    "message": f"{capability} 在 iOS 设备上不可用（本期未接线，契约 §26）",
                },
            },
        )

    def adb_serial(self, device_id: str) -> str | None:
        """设备 id → adb serial；iOS 设备短路返回 None（不解析、不分发到 adb 源，
        udid 不冒充 serial；WiFi 行会话仍经 adb 源映射回 host:port）。"""
        owner = self._owner_for(device_id)
        if owner is self._android and owner is not None:
            return owner.adb_serial(device_id)
        return None

    def wifi_enable(self, device_id: str) -> dict[str, Any]:
        return self._android_only(device_id, "WiFi 模式", "wifi.unavailable").wifi_enable(
            device_id
        )

    def wifi_connect(self, device_id: str, host: str) -> dict[str, Any]:
        return self._android_only(device_id, "WiFi 模式", "wifi.unavailable").wifi_connect(
            device_id, host
        )

    def speedtest(
        self,
        device_id: str,
        *,
        bytes_count: int,
        backend_port: int,
        max_time_s: int = 60,
    ) -> dict[str, Any]:
        return self._android_only(
            device_id, "网络测速", "speedtest.unavailable"
        ).speedtest(
            device_id, bytes_count=bytes_count, backend_port=backend_port,
            max_time_s=max_time_s,
        )

    def boot_device(self, device_id: str) -> dict[str, Any]:
        """sim-boot（§23.1 聚合/§26.1 golden）：simulator 源设备分发 boot；
        真机行（RealIOSProvider 自带 §26.1 golden 错误体）/无 boot 语义源 →
        400 validation_error（目标设备形态不符，非模式缺失）。"""
        owner = self._owner_for(device_id)
        if owner is not None and callable(getattr(owner, "boot_device", None)):
            return owner.boot_device(device_id)
        device = self.get_device(device_id)
        if device is None:
            raise ApiError("device_not_found", f"device {device_id} 不存在")
        raise ApiError(
            "validation_error",
            SIM_BOOT_UNSUPPORTED_MESSAGE,
            details={"deviceId": device_id, "connection": device.get("connection")},
        )

    def launch_app(self, device_id: str, package_name: str) -> dict[str, Any]:
        """launchOnCreate 分流（§6.1/§26.2）：adb 源 am start / simctl launch /
        devicectl process launch（S1 RealIOSProvider 承载真机形态；
        返回 {"component","error"} 契约形状，失败不抛异常）。"""
        owner = self._owner_for(device_id)
        if owner is not None and callable(getattr(owner, "launch_app", None)):
            return owner.launch_app(device_id, package_name)
        device = self.get_device(device_id)
        if device is None:
            return {"component": None, "error": f"device {device_id} 不存在"}
        return {"component": None, "error": "provider 不支持应用拉起"}

    def get_profile(self, device_id: str) -> dict[str, Any] | None:
        owner = self._owner_for(device_id)
        if owner is not None and callable(getattr(owner, "get_profile", None)):
            return owner.get_profile(device_id)
        return None

    def app_version(self, device_id: str, package_name: str) -> dict[str, Any] | None:
        """save 时应用版本探测：仅 adb 源实现（dumpsys package）；iOS 设备 None
        （不伪造——manifest.appVersion=null，session_manager duck-type 消费）。"""
        owner = self._owner_for(device_id)
        if owner is self._android and owner is not None:
            probe = getattr(owner, "app_version", None)
            if callable(probe):
                return probe(device_id, package_name)
        return None

    def list_apps(self, device_id: str) -> dict[str, Any] | None:
        """应用列表按源分发（§26.2：真机 iOS 走 RealIOSProvider 的 devicectl
        apps+processes 合成面）。设备不在任何源 → None（404 语义）。"""
        owner = self._owner_for(device_id)
        if owner is None:
            return None
        return owner.list_apps(device_id)

    def list_metric_ids(self) -> list[str]:
        """模式级可用指标全集（无设备上下文口径，聚合面按 real 通道模式）。"""
        return available_metric_ids(self.mode)
