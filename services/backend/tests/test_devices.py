"""设备与应用端点（契约 §4）。"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_devices_list_matches_contract(client: TestClient) -> None:
    resp = client.get("/api/devices")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "synthetic"
    devices = body["devices"]
    assert isinstance(devices, list) and devices
    device = devices[0]
    # 契约 §4 示例结构（值按 golden，synthetic 模式逐字一致）
    assert device["id"] == "synthetic-001"
    assert device["platform"] == "android"
    assert device["model"] == "Synthetic Device 001"
    assert device["serial"] == "synthetic-001"
    assert device["state"] == "online"
    assert device["connection"] == "usb"
    assert device["info"] == {
        "os": "Android 14 (synthetic)",
        "cpu": "8 cores (synthetic)",
        "resolution": "1080x2400",
        "batteryLevel": 87,
        "refreshRateHz": 120,
    }


def test_device_detail_and_404(client: TestClient) -> None:
    ok = client.get("/api/devices/synthetic-001")
    assert ok.status_code == 200
    assert ok.json()["id"] == "synthetic-001"

    missing = client.get("/api/devices/nope")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "device_not_found"


def test_device_apps_matches_contract(client: TestClient) -> None:
    resp = client.get("/api/devices/synthetic-001/apps")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deviceId"] == "synthetic-001"
    apps = body["apps"]
    assert apps and isinstance(apps, list)
    app = apps[0]
    assert app["packageName"] == "com.example.synthetic.game"
    assert app["running"] is True
    assert app["isForeground"] is True
    assert isinstance(app["processes"], list)
    assert app["processes"][0]["isMain"] is True


def test_device_apps_404(client: TestClient) -> None:
    resp = client.get("/api/devices/ghost/apps")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "device_not_found"


def test_metric_registry_matches_contract(client: TestClient) -> None:
    """契约 §5：10 项采样器指标 + frame.fps.mean；synthetic 下 gpu.busy.pct 不可用。"""
    resp = client.get("/api/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "synthetic"
    by_id = {m["metricId"]: m for m in body["metrics"]}
    sampler_metrics = {
        "cpu.total.pct", "cpu.proc.pct", "memory.pss.peak_mb", "net.rx.kbps",
        "net.tx.kbps", "power.current.ma", "power.level.pct", "power.energy.mah",
        "gpu.busy.pct", "thermal.max.c",
    }
    assert sampler_metrics <= set(by_id)
    assert "frame.fps.mean" in by_id
    for metric in by_id.values():
        assert set(metric) >= {"metricId", "unit", "direction", "evidenceLevel", "available"}
    # 契约 §5 示例 golden 逐字段（§11.1 兼容：M6 起每指标可附加 defaultIntervalMs/
    # capabilities/description 可选字段——无 deviceId 时 available 仅按模式判定）
    entry = by_id["cpu.total.pct"]
    assert {
        key: entry[key]
        for key in ("metricId", "unit", "direction", "evidenceLevel", "available")
    } == {
        "metricId": "cpu.total.pct", "unit": "%", "direction": "lower_better",
        "evidenceLevel": "L1", "available": True,
    }
    assert set(entry["capabilities"]) == {"modes", "platform"}  # 无设备上下文不出 reason
    assert by_id["frame.fps.mean"]["direction"] == "higher_better"
    assert by_id["frame.fps.mean"]["unit"] == "fps"
    assert by_id["gpu.busy.pct"]["available"] is False  # kgsl OEM 口径，synthetic 不可用


def test_detect_mode_with_fake_adb() -> None:
    """auto 探测：有在线设备 → real；无设备/adb 不可用 → synthetic（H5）。"""
    from dataclasses import dataclass

    from app.providers.real import detect_mode

    @dataclass(frozen=True)
    class _Device:
        serial: str
        state: str

    class _FakeAdb:
        def __init__(self, devices: list, available: bool = True) -> None:
            self._devices = devices
            self._available = available

        def available(self) -> bool:
            return self._available

        def list_devices(self):  # noqa: ANN201
            return self._devices

    assert detect_mode(_FakeAdb([_Device("s1", "device")])) == "real"
    assert detect_mode(_FakeAdb([_Device("s1", "offline")])) == "synthetic"
    assert detect_mode(_FakeAdb([])) == "synthetic"
    assert detect_mode(_FakeAdb([], available=False)) == "synthetic"
