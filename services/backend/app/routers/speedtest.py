"""网络测速路由（R53 最小切片，契约 §24）。

- `GET /api/speedtest/payload?bytes=N`：测速负载源（全模式可用，纯字节流）；
- `POST /api/devices/{deviceId}/speedtest`：设备侧实测下行吞吐（real-only，
  adb reverse + 设备 shell curl；synthetic/simulator 501 + 诊断，与 wifi-mode/
  sim-boot 同哲学）。

装配流 S4（契约 §26）：能力门从 provider.mode 改为设备源通道判定
（provider_device_mode）——聚合面下 iOS 设备（simulator 通道/真机）返回 501
契约错误码 + 诊断（不 AttributeError→500），real 通道 Android 设备分发
provider（聚合面路由 adb 源）行为不变。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Request
from fastapi.responses import StreamingResponse

from ..errors import ApiError, validation_error
from ..providers.composite import provider_device_mode
from ..services import speedtest as st

router = APIRouter(prefix="/api", tags=["speedtest"])


@router.get("/speedtest/payload")
def speedtest_payload(bytes: int = st.SPEEDTEST_DEFAULT_BYTES) -> StreamingResponse:
    """测速负载源（契约 §24）：确定性字节流，Content-Length 精确等于请求字节数。

    设备侧 curl 依赖 Content-Length 判定传输完成，因此必须显式携带该头。
    """
    if bytes < st.SPEEDTEST_MIN_BYTES or bytes > st.SPEEDTEST_MAX_BYTES:
        raise validation_error(
            f"bytes 必须在 [{st.SPEEDTEST_MIN_BYTES}, {st.SPEEDTEST_MAX_BYTES}] 内",
            field="bytes",
        )
    return StreamingResponse(
        st.payload_chunks(bytes),
        media_type="application/octet-stream",
        headers={"Content-Length": str(bytes)},
    )


@router.post("/devices/{device_id}/speedtest")
def device_speedtest(
    device_id: str, request: Request, body: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    """设备侧下行测速（契约 §24，real-only）。

    - 请求体可选字段：`bytes`（缺省 4 MiB）、`maxSeconds`（缺省 60）；
    - synthetic/simulator：501 not_implemented + SSE diagnostic
      （code `speedtest.unavailable`，severity info，wifi-mode 同哲学）；
    - 404 device_not_found；设备侧 curl 缺失 / adb reverse 失败 / curl 执行失败
      → 503 speedtest_unavailable（details.reason 区分阶段）；
    - 设备级命令经 DeviceCommandQueue 串行（R22 按设备 actor 隔离）。
    """
    provider = _provider(request)
    bytes_count = body.get("bytes", st.SPEEDTEST_DEFAULT_BYTES)
    if not isinstance(bytes_count, int) or isinstance(bytes_count, bool) or not (
        st.SPEEDTEST_MIN_BYTES <= bytes_count <= st.SPEEDTEST_MAX_BYTES
    ):
        raise validation_error(
            f"bytes 必须是 [{st.SPEEDTEST_MIN_BYTES}, {st.SPEEDTEST_MAX_BYTES}] 内的整数",
            field="bytes",
        )
    max_time_s = body.get("maxSeconds", st.SPEEDTEST_DEFAULT_TIME_S)
    if not isinstance(max_time_s, int) or isinstance(max_time_s, bool) or not (
        st.SPEEDTEST_MIN_TIME_S <= max_time_s <= st.SPEEDTEST_MAX_TIME_S
    ):
        raise validation_error(
            f"maxSeconds 必须是 [{st.SPEEDTEST_MIN_TIME_S}, {st.SPEEDTEST_MAX_TIME_S}] 内的整数",
            field="maxSeconds",
        )

    device = provider.get_device(device_id)
    if device is None:
        raise ApiError("device_not_found", f"device {device_id} 不存在")
    # S4：门按设备源通道判定（聚合面下 simulator 通道 iOS 设备/真机 iOS 设备同拒）
    channel = provider_device_mode(provider, device_id)
    if channel != "real":
        if channel == "simulator":
            unavailable_message = (
                "simulator 通道 iOS 设备无下行测速通道（adb reverse + 设备侧 curl 为 "
                "real-only 能力；simulator 宿主口径链路 ≠ 模拟器网络栈，R24 真机切片再评估）"
            )
        else:
            unavailable_message = (
                "真机 iOS 设备无下行测速通道（adb reverse + 设备侧 curl 为 Android "
                "链路能力；真机 iOS 语义 R24 真机切片再评估）"
            ) if device.get("platform") == "ios" else (
                f"{channel} 通道无下行测速通道（adb reverse + 设备侧 curl 为 real-only 能力）"
            )
        diagnostic = {
            "code": "speedtest.unavailable",
            "severity": "info",
            "message": unavailable_message,
        }
        _broadcast_diagnostic(request, device_id, diagnostic)
        raise ApiError(
            "not_implemented",
            "网络测速为 real-only 能力（adb reverse + 设备侧 curl）",
            details={"diagnostic": diagnostic},
        )

    backend_port = request.app.state.settings.port
    queue = request.app.state.device_queue

    def _run() -> dict[str, Any]:
        return provider.speedtest(
            device_id, bytes_count=bytes_count, backend_port=backend_port,
            max_time_s=max_time_s,
        )

    return queue.run_serially(device_id, _run)


def _provider(request: Request) -> Any:
    return request.app.state.provider


def _broadcast_diagnostic(request: Request, device_id: str, diagnostic: dict) -> None:
    """向该设备上活跃会话的 SSE bus 广播诊断（无活跃会话时随错误体透出）。"""
    manager = request.app.state.sessions
    for session in manager.list():
        if session.device_id == device_id and session.state in ("monitoring", "recording"):
            session.bus.publish("diagnostic", **diagnostic, detail=None)
