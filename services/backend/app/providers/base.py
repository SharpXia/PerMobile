"""providers 抽象（ARCH §4.2）：real（perfeval adb）/ synthetic（合成流）。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class DeviceProvider(ABC):
    """设备/应用枚举与模式元信息；采集执行在 app.services.collector_service。"""

    mode: str = "synthetic"

    @abstractmethod
    def list_devices(self) -> list[dict[str, Any]]:
        """契约 §4 设备列表（不含 mode 外壳）。"""

    @abstractmethod
    def get_device(self, device_id: str) -> dict[str, Any] | None:
        """单设备详情；不存在返回 None。"""

    @abstractmethod
    def list_apps(self, device_id: str) -> dict[str, Any] | None:
        """契约 §4 应用列表外壳（{"deviceId", "apps"}）；设备不存在返回 None。"""

    @abstractmethod
    def list_metric_ids(self) -> list[str]:
        """该模式下 available=true 的 metricId 全集（registry.available_metric_ids）。"""

    def get_profile(self, device_id: str) -> dict[str, Any] | None:
        """M6 R21 设备 profile（capability_matrix 判定输入）；缺省无 profile。

        返回形状见 capability_matrix.device_profile（含内部探测字段，不直接序列化；
        wire 形状经 capability_matrix.wire_profile 剥离）。设备不存在返回 None。
        """
        return None

    def device_exists(self, device_id: str) -> bool:
        return self.get_device(device_id) is not None

    def device_state(self, device_id: str) -> str | None:
        device = self.get_device(device_id)
        return device["state"] if device else None
