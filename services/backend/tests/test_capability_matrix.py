"""R21 能力门禁矩阵（契约 §20）：/api/metrics?deviceId= 交集判定 + 设备 profile。

synthetic 双设备 profile 不同（001 无 kgsl / 002 有 kgsl）演示交集判定两条分支；
available 判定 = capabilities ∩ profile（契约 §20.3），POST /api/sessions 落点
受 metric_unavailable 拦截。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

from conftest import make_session  # noqa: E402


def _metrics_by_id(client: TestClient, device_id: str | None = None) -> dict:
    params = {"deviceId": device_id} if device_id else {}
    resp = client.get("/api/metrics", params=params)
    assert resp.status_code == 200, resp.text
    return {m["metricId"]: m for m in resp.json()["metrics"]}


def test_devices_have_two_synthetic_profiles(client: TestClient) -> None:
    """契约 §20.2 golden：两台合成设备 profile 不同（001 无 kgsl / 002 有 kgsl）。

    M-iOS §23.1（§11.1 兼容扩展）：wire 形状新增可选 ``simulator`` 字段——
    Android 设备恒 False（simulator 连接模式标志）。
    """
    body = client.get("/api/devices").json()
    devices = body["devices"]
    assert [d["id"] for d in devices] == ["synthetic-001", "synthetic-002"]
    profile_001 = devices[0]["profile"]
    profile_002 = devices[1]["profile"]
    assert profile_001 == {
        "usb": True, "wifi": False, "simulator": False, "gpuSource": None,
        "cpuCores": 8, "refreshRateHz": 120,
    }
    assert profile_002 == {
        "usb": True, "wifi": False, "simulator": False, "gpuSource": "kgsl",
        "cpuCores": 8, "refreshRateHz": 90,
    }
    # 单设备详情同形（契约 §20.2「与单设备详情同形」）
    detail = client.get("/api/devices/synthetic-002").json()
    assert detail["profile"] == profile_002
    # info 契约 §20.2 golden（002：电量 64 / 90Hz）
    assert detail["info"]["batteryLevel"] == 64
    assert detail["info"]["refreshRateHz"] == 90


def test_metrics_registry_entries_and_defaults(client: TestClient) -> None:
    """契约 §21.2/§21.3：注册表新增 7 项 + defaultIntervalMs/capabilities 形态。"""
    by_id = _metrics_by_id(client)
    assert by_id["cpu.freq.mhz"]["defaultIntervalMs"] == 2000
    assert by_id["cpu.freq.mhz"]["unit"] == "MHz"
    assert by_id["cpu.freq.mhz"]["direction"] == "none"
    assert by_id["cpu.proc.normalized.pct"]["defaultIntervalMs"] == 1000
    assert by_id["cpu.proc.normalized.pct"]["direction"] == "lower_better"
    # 规范化口径声明进 description（契约 §21.2）
    assert "规范化" in by_id["cpu.proc.normalized.pct"]["description"]
    assert "cpu.proc.pct" in by_id["cpu.proc.normalized.pct"]["description"]
    assert by_id["cpu.proc.thread.pct"]["defaultIntervalMs"] == 2000
    # 内存 Detail 组：四项 description 带官方损耗提示（[F§3.8][I§2.4]）
    for metric_id in (
        "memory.detail.native_mb", "memory.detail.dalvik_mb",
        "memory.detail.gfx_mb", "memory.detail.gl_mb",
    ):
        entry = by_id[metric_id]
        assert entry["unit"] == "MB"
        assert entry["defaultIntervalMs"] == 2000
        assert "Memory Detail" in entry["description"]
        assert entry["available"] is True  # synthetic 合成明细恒可解析
    # 契约 §5 缺省表：memory.*/gpu.busy.pct/thermal.max.c=2000
    assert by_id["memory.pss.peak_mb"]["defaultIntervalMs"] == 2000
    assert by_id["gpu.busy.pct"]["defaultIntervalMs"] == 2000
    assert by_id["thermal.max.c"]["defaultIntervalMs"] == 2000


def test_intersection_judgment_two_branches(client: TestClient) -> None:
    """契约 §20.3 交集判定两条分支（§20.2 golden 双设备演示口径）。

    - gpu.busy.pct 对 001（gpuSource=null）：available=false + kgsl_missing；
    - gpu.busy.pct 对 002（kgsl）：设备资源满足，但 synthetic 模式采集通道未
      建模 → available=false + not_landed（模式未落地，非设备缺失）；
    - 其余指标在两台设备均不带 reason。
    """
    by_id_001 = _metrics_by_id(client, "synthetic-001")
    gpu_001 = by_id_001["gpu.busy.pct"]
    assert gpu_001["available"] is False
    assert gpu_001["capabilities"]["reason"] == "kgsl_missing"
    assert gpu_001["capabilities"]["modes"] == ["usb", "wifi"]
    assert gpu_001["capabilities"]["platform"] == "android"

    by_id_002 = _metrics_by_id(client, "synthetic-002")
    gpu_002 = by_id_002["gpu.busy.pct"]
    assert gpu_002["available"] is False
    assert gpu_002["capabilities"]["reason"] == "not_landed"

    # 非受限指标不带 reason；frame.fps.mean real 通道已落地（gfxinfo 轮询，
    # 2026-11-02），synthetic/real 均可用无 reason
    assert "reason" not in by_id_001["cpu.total.pct"]["capabilities"]
    assert "reason" not in by_id_002["cpu.proc.thread.pct"]["capabilities"]


def test_metrics_unknown_device_404(client: TestClient) -> None:
    resp = client.get("/api/metrics", params={"deviceId": "ghost"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "device_not_found"


def test_session_create_device_gate_metric_unavailable(client: TestClient) -> None:
    """契约 §20.3：落点拦截——kgsl 缺失设备请求 gpu.busy.pct → 409
    metric_unavailable（details.metricId）。"""
    resp = client.post("/api/sessions", json={
        "deviceId": "synthetic-001",
        "packageName": "com.example.synthetic.game",
        "metrics": [{"metricId": "gpu.busy.pct", "intervalMs": 1000}],
        "interactive": False,
        "launchOnCreate": False,
    })
    assert resp.status_code == 409, resp.text
    error = resp.json()["error"]
    assert error["code"] == "metric_unavailable"
    assert error["details"]["metricId"] == "gpu.busy.pct"
    # 002 无 kgsl 门禁（内存明细组同径放行）
    resp2 = client.post("/api/sessions", json={
        "deviceId": "synthetic-002",
        "packageName": "com.example.synthetic.game",
        "metrics": [{"metricId": "memory.detail.native_mb", "intervalMs": 2000}],
        "interactive": False,
        "launchOnCreate": False,
    })
    assert resp2.status_code == 201, resp2.text
    assert resp2.json()["metrics"] == [
        {"metricId": "memory.detail.native_mb", "intervalMs": 2000}
    ]


def test_default_metric_set_excludes_device_gated(client: TestClient) -> None:
    """缺省指标集（metrics 缺省）同样经交集判定过滤（§20.3 判定输入一致性）。"""
    resp = client.post("/api/sessions", json={
        "deviceId": "synthetic-001",
        "packageName": "com.example.synthetic.app",
        "interactive": False,
        "launchOnCreate": False,
    })
    assert resp.status_code == 201
    metric_ids = {m["metricId"] for m in resp.json()["metrics"]}
    assert "gpu.busy.pct" not in metric_ids  # synthetic 不可用
    assert "memory.detail.native_mb" in metric_ids  # synthetic 明细可用
    client.post(f"/api/sessions/{resp.json()['id']}/discard")
