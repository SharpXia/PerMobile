"""统一错误体与错误码（契约 §3）。

所有非 2xx 响应统一为：
    {"error": {"code": ..., "message": ..., "details": {...}}}
FastAPI 默认 422 必须被 exception handler 覆写为本格式（main.py）。
"""

from __future__ import annotations

from typing import Any

# code -> HTTP 状态码（契约 §3 表）
ERROR_STATUS: dict[str, int] = {
    "validation_error": 400,
    "device_not_found": 404,
    "device_not_online": 503,
    "device_ambiguous": 409,
    "device_none_online": 503,
    "adb_devices_failed": 503,
    "adb_not_found": 503,
    "session_not_found": 404,
    "session_state_conflict": 409,
    "metric_unavailable": 409,
    "device_busy": 409,
    "report_not_found": 404,
    # M4（契约 §3/§16.2）：real 模式从设备取工件数据失败（截图 exec-out screencap
    # 失败等，details.phase 标明阶段）；§3 既有行「save 时拉取工件失败」含义不变
    "artifact_pull_failed": 502,
    # M6（契约 §3 M6 新增行/§21.1）：real 模式 wifi-mode 的 adb tcpip/connect
    # 执行失败（仅该端点发出；既有行含义不变）
    "wifi_connect_failed": 503,
    # M-iOS（契约 §3 M-iOS 新增行/§23.1）：simulator 模式 xcrun simctl 不可用
    #（命令行工具缺失/执行失败/boot 超时；与 adb_not_found 同语义分离先例）
    "simctl_unavailable": 503,
    # M8+（契约 §3 新增行/§24）：网络测速通道不可用（设备侧 curl 缺失/adb reverse
    # 失败/curl 执行失败/输出不可解析，details.reason 区分阶段；仅 speedtest 端点发出）
    "speedtest_unavailable": 503,
    "not_implemented": 501,
    "internal_error": 500,
}


class ApiError(Exception):
    """业务错误；router/exception handler 转换为契约错误体。"""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        status: int | None = None,
    ) -> None:
        if code not in ERROR_STATUS:
            raise ValueError(f"未知错误码 {code!r}，契约 §3 未定义")
        self.code = code
        self.message = message
        self.details = details if details is not None else {}
        self.status = status or ERROR_STATUS[code]
        super().__init__(message)

    def to_body(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


def validation_error(message: str, field: str | None = None, **details: Any) -> ApiError:
    payload: dict[str, Any] = dict(details)
    if field is not None:
        payload["field"] = field
    return ApiError("validation_error", message, details=payload)
