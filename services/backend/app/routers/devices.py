"""设备与应用路由（契约 §4 + §20.2 profile + §21.1 wifi-mode + §23.1 sim-boot）。

装配流 S4（契约 §26/§23.1 聚合语义）：设备级能力门从「provider.mode 一刀切」
改为「设备源通道」判定（provider_device_mode：ios+simulator 设备 → simulator
通道、其余 → real；单源 provider 回退 provider.mode 逐字段不变）——

- wifi-mode：real 通道设备分发 provider（聚合面路由 adb 源）；simulator 通道
  设备与 synthetic 单源保持 501 not_implemented + 诊断（iOS 设备无 adb 通道）；
- sim-boot：simulator 通道设备放行 boot（聚合面内 simctl 源设备，原
  provider.mode 一刀切 501 修订）；synthetic 单源保持 501；real 通道设备
  （Android 真机/真机 iOS）→ 400 validation_error（目标设备形态不符，
  非模式缺失）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Request

from ..errors import ApiError, validation_error
from ..providers.composite import provider_device_mode

router = APIRouter(prefix="/api", tags=["devices"])

WIFI_ACTIONS = ("enable", "connect")


def _provider(request: Request) -> Any:
    return request.app.state.provider


@router.get("/devices")
def list_devices(request: Request) -> dict[str, Any]:
    return {"mode": _provider(request).mode, "devices": _provider(request).list_devices()}


@router.get("/devices/{device_id}")
def get_device(device_id: str, request: Request) -> dict[str, Any]:
    device = _provider(request).get_device(device_id)
    if device is None:
        raise ApiError("device_not_found", f"device {device_id} 不存在")
    return device


@router.get("/devices/{device_id}/apps")
def list_apps(device_id: str, request: Request) -> dict[str, Any]:
    body = _provider(request).list_apps(device_id)
    if body is None:
        raise ApiError("device_not_found", f"device {device_id} 不存在")
    return body


# ---- M6 R15：WiFi 模式（契约 §21.1，real-only）----


@router.post("/devices/{device_id}/wifi-mode")
def wifi_mode(
    device_id: str, request: Request, body: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """adb over WiFi（real-only）：enable = `adb tcpip 5555`，connect = `adb connect
    <host>:5555`（host 必填）。

    - synthetic 模式：501 not_implemented + SSE diagnostic（code wifi.unavailable，
      severity info，向该设备上活跃会话的 bus 广播——与 §16 截图/录屏的 synthetic
      诊断同哲学）；无活跃会话时诊断随错误体 details 透出（不下发丢失）；
    - 校验：action 非法 / connect 缺 host → 400 validation_error；
      404 device_not_found；adb tcpip/connect 执行失败 → 503 wifi_connect_failed；
    - 设备级命令经 DeviceCommandQueue 串行（R22 按设备 actor 隔离）。
    """
    provider = _provider(request)
    action = body.get("action")
    if action not in WIFI_ACTIONS:
        raise validation_error(
            f"action 必须是 {'|'.join(WIFI_ACTIONS)} 之一", field="action"
        )
    host = body.get("host")
    if action == "connect" and (
        not isinstance(host, str) or not host.strip()
    ):
        raise validation_error("connect 动作必须提供 host", field="host")

    device = provider.get_device(device_id)
    if device is None:
        raise ApiError("device_not_found", f"device {device_id} 不存在")
    # S4：门按设备源通道判定（聚合面下 simulator 通道 iOS 设备同拒；real 通道
    # Android 设备（含 WiFi 行）分发 provider → adb 源，行为不变）
    channel = provider_device_mode(provider, device_id)
    if channel != "real":
        if channel == "simulator":
            unavailable_message = (
                "simulator 通道 iOS 设备无 WiFi 通道（wifi-mode 为 real-only adb 能力）"
            )
        else:
            unavailable_message = "synthetic 模式无 WiFi 通道（wifi-mode 为 real-only adb 能力）"
        diagnostic = {
            "code": "wifi.unavailable",
            "severity": "info",
            "message": unavailable_message,
        }
        manager = request.app.state.sessions
        for session in manager.list():
            if session.device_id == device_id and session.state in ("monitoring", "recording"):
                session.bus.publish("diagnostic", **diagnostic, detail=None)
        raise ApiError(
            "not_implemented",
            f"WiFi 模式为 real-only 能力（adb tcpip/connect），{channel} 通道未实现",
            details={"diagnostic": diagnostic},
        )

    # 设备级串行窗口（R22：同设备命令互斥，跨设备并行）
    queue = request.app.state.device_queue

    def _run() -> dict[str, Any]:
        if action == "enable":
            return provider.wifi_enable(device_id)
        return provider.wifi_connect(device_id, host.strip())

    return queue.run_serially(device_id, _run)


# ---- M-iOS §23.1：iOS Simulator 启动（simulator-only）----


@router.post("/devices/{device_id}/sim-boot")
def sim_boot(device_id: str, request: Request) -> dict[str, Any]:
    """启动 iOS Simulator（M-iOS，契约 §23.1；S4 §23.1 聚合 sim-boot 语义）。

    - ``simctl boot <udid>`` + ``bootstatus -b``（阻塞至启动完成，上限 ~180s）；
      已 Booted 设备幂等返回当前态；
    - 装配流 S4：门按设备源通道判定——simulator 通道设备放行（聚合面内 simctl
      源设备经 provider.boot_device 分发；原 provider.mode 一刀切 501 修订）；
    - synthetic 单源（模式无模拟器设备面）：501 not_implemented + SSE diagnostic
      （sim-boot.unavailable，info）——与 wifi-mode 的 synthetic 分支同哲学；
    - real 通道设备（Android 真机/真机 iOS/聚合面下其余形态）：400
      validation_error（目标设备非 simulator 连接，属请求目标错误而非模式缺失）；
    - 错误：404 device_not_found；503 simctl_unavailable（xcrun 缺失/启动超时，
      §23 新增错误码，§3 错误表 M-iOS 行）；
    - 设备级命令经 DeviceCommandQueue 串行（R22 按设备 actor 隔离）。
    """
    provider = _provider(request)
    device = provider.get_device(device_id)
    if device is None:
        raise ApiError("device_not_found", f"device {device_id} 不存在")
    channel = provider_device_mode(provider, device_id)
    if channel != "simulator":
        if channel == "synthetic" or provider.mode == "synthetic":
            diagnostic = {
                "code": "sim-boot.unavailable",
                "severity": "info",
                "message": "sim-boot 为 simulator-only 能力（xcrun simctl），当前模式未实现",
            }
            manager = request.app.state.sessions
            for session in manager.list():
                if session.device_id == device_id and session.state in (
                    "monitoring", "recording",
                ):
                    session.bus.publish("diagnostic", **diagnostic, detail=None)
            raise ApiError(
                "not_implemented",
                "sim-boot 为 simulator-only 能力（xcrun simctl boot/bootstatus）",
                details={"diagnostic": diagnostic},
            )
        # §26.1 golden 错误体（真机行 400 validation_error，与 S1
        # RealIOSProvider.boot_device 同体——聚合面下 Android 真机行同语义）
        from ..providers.composite import SIM_BOOT_UNSUPPORTED_MESSAGE

        raise ApiError(
            "validation_error",
            SIM_BOOT_UNSUPPORTED_MESSAGE,
            details={"deviceId": device_id, "connection": device.get("connection")},
        )
    queue = request.app.state.device_queue
    return queue.run_serially(device_id, lambda: provider.boot_device(device_id))
