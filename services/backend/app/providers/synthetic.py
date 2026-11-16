"""synthetic provider（H5）：无真机时的设备/应用/指标合成形态，保证切片可演示。

设备与应用结构对齐契约 §4 示例（golden）；包名限两枚举值（契约 §6）。

M6（契约 §20.2/§20.4/§21.6）：
- 合成设备扩为 ≥2 台（synthetic-001/-002，R22 多设备矩阵演示口径；首位设备保持
  既有 golden 不变），两台 profile 不同——001 无 kgsl GPU 源（演示 gpu.busy.pct
  kgsl_missing 禁勾分支）、002 有 kgsl（演示可用分支），刷新率/电量亦按 golden；
- 每设备 profile 由 device_profile() 构造（capability_matrix 判定输入）；
- game 包 processes[] 增加子进程演示元素（R20；既有首元素不变）。
"""

from __future__ import annotations

from typing import Any

from ..registry import available_metric_ids
from ..services.capability_matrix import device_profile, wire_profile
from .base import DeviceProvider

SYNTHETIC_PACKAGES = (
    "com.example.synthetic.game",
    "com.example.synthetic.app",
)

_DEVICES: tuple[dict[str, Any], ...] = (
    {
        "id": "synthetic-001",
        "platform": "android",
        "model": "Synthetic Device 001",
        "serial": "synthetic-001",
        "state": "online",
        "connection": "usb",
        "info": {
            "os": "Android 14 (synthetic)",
            "cpu": "8 cores (synthetic)",
            "resolution": "1080x2400",
            "batteryLevel": 87,
            "refreshRateHz": 120,
        },
    },
    # R22 第二台合成设备（契约 §20.2 golden：gpuSource="kgsl"、90Hz、电量 64）
    {
        "id": "synthetic-002",
        "platform": "android",
        "model": "Synthetic Device 002",
        "serial": "synthetic-002",
        "state": "online",
        "connection": "usb",
        "info": {
            "os": "Android 14 (synthetic)",
            "cpu": "8 cores (synthetic)",
            "resolution": "1080x2400",
            "batteryLevel": 64,
            "refreshRateHz": 90,
        },
    },
)

# profile（契约 §20.2）：两台合成设备特性不同——001 无 kgsl、002 有 kgsl；
# synthetic 模式无 WiFi 通道（wifi=False），meminfo 明细段合成源恒可解析；
# power.* 合成序列有数据（合成流直出），powerSupply 恒 True。
_PROFILES: dict[str, dict[str, Any]] = {
    "synthetic-001": device_profile(
        usb=True, wifi=False, gpu_source=None,
        cpu_cores=8, refresh_rate_hz=120, meminfo_detail=True,
        power_supply=True,
    ),
    "synthetic-002": device_profile(
        usb=True, wifi=False, gpu_source="kgsl",
        cpu_cores=8, refresh_rate_hz=90, meminfo_detail=True,
        power_supply=True,
    ),
}

_APPS: dict[str, list[dict[str, Any]]] = {
    "synthetic-001": [
        {
            "packageName": "com.example.synthetic.game",
            "name": "Synthetic Game",
            "pid": 12345,
            "running": True,
            "isForeground": True,
            # 契约 §21.6：子进程演示（既有首元素不变；pid 12347 为 :sub 子进程）
            "processes": [
                {"pid": 12345, "name": "com.example.synthetic.game", "isMain": True},
                {"pid": 12347, "name": "com.example.synthetic.game:sub", "isMain": False},
            ],
        },
        {
            "packageName": "com.example.synthetic.app",
            "name": "Synthetic App",
            "pid": 12346,
            "running": True,
            "isForeground": False,
            "processes": [
                {"pid": 12346, "name": "com.example.synthetic.app", "isMain": True}
            ],
        },
    ],
    # R22：第二台设备的应用列表（同两枚举包，pid 段错开避免跨设备混淆）
    "synthetic-002": [
        {
            "packageName": "com.example.synthetic.game",
            "name": "Synthetic Game",
            "pid": 22345,
            "running": True,
            "isForeground": True,
            "processes": [
                {"pid": 22345, "name": "com.example.synthetic.game", "isMain": True},
                {"pid": 22347, "name": "com.example.synthetic.game:sub", "isMain": False},
            ],
        },
        {
            "packageName": "com.example.synthetic.app",
            "name": "Synthetic App",
            "pid": 22346,
            "running": True,
            "isForeground": False,
            "processes": [
                {"pid": 22346, "name": "com.example.synthetic.app", "isMain": True}
            ],
        },
    ],
}


class SyntheticProvider(DeviceProvider):
    mode = "synthetic"

    def list_devices(self) -> list[dict[str, Any]]:
        return [self._with_profile(dict(device)) for device in _DEVICES]

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        for device in _DEVICES:
            if device["id"] == device_id:
                return self._with_profile(dict(device))
        return None

    @staticmethod
    def _with_profile(device: dict[str, Any]) -> dict[str, Any]:
        """附加契约 §20.2 profile（wire 形状；设备 id 未知时缺省无 kgsl profile）。"""
        profile = _PROFILES.get(device["id"])
        if profile is not None:
            device["profile"] = wire_profile(profile)
        return device

    def get_profile(self, device_id: str) -> dict[str, Any] | None:
        """capability_matrix 判定输入（含内部探测字段）；设备不存在返回 None。"""
        if self.get_device(device_id) is None:
            return None
        return dict(_PROFILES.get(device_id) or device_profile(
            usb=True, wifi=False, gpu_source=None, cpu_cores=8,
            refresh_rate_hz=120, meminfo_detail=True, power_supply=True,
        ))

    def list_apps(self, device_id: str) -> dict[str, Any] | None:
        if self.get_device(device_id) is None:
            return None
        apps = [dict(app) for app in _APPS.get(device_id, [])]
        return {"deviceId": device_id, "apps": apps}

    def list_metric_ids(self) -> list[str]:
        return available_metric_ids(self.mode)
