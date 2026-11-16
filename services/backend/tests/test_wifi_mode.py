"""R15 WiFi 模式（契约 §21.1）：real-only adb tcpip/connect + synthetic 501+诊断。

单测用 mock adb（FakeAdb 应答表）断言命令序列（`adb -s <serial> tcpip 5555` /
`adb connect <host>:5555`）；真机 I/O 路径未实测（fixture 驱动生产代码）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from conftest import make_session  # noqa: E402
from m6_fixtures import (  # noqa: E402
    FakeAdb,
    completed,
    default_probe_responses,
    fake_preflight_result,
)


# ---- synthetic 模式（501 + 诊断）----


def test_wifi_mode_synthetic_501_with_diagnostic(client: TestClient) -> None:
    resp = client.post("/api/devices/synthetic-001/wifi-mode", json={"action": "enable"})
    assert resp.status_code == 501
    error = resp.json()["error"]
    assert error["code"] == "not_implemented"
    # 诊断随错误体 details 透出（无活跃会话时不下发丢失）
    diagnostic = error["details"]["diagnostic"]
    assert diagnostic["code"] == "wifi.unavailable"
    assert diagnostic["severity"] == "info"
    assert "synthetic" in diagnostic["message"]


def test_wifi_mode_synthetic_diagnostic_broadcast_to_session(client: TestClient) -> None:
    """该设备上有活跃会话时，诊断经其 SSE bus 广播（与 §16 截图诊断同哲学）。"""
    session = make_session(client)
    try:
        with client.stream(
            "GET", f"/api/sessions/{session['id']}/events",
            headers={"Last-Event-ID": "0"},
        ) as resp:
            client.post("/api/devices/synthetic-001/wifi-mode", json={"action": "enable"})
            events = []
            for line in resp.iter_lines():
                if line is None:
                    continue
                text = line.rstrip("\r")
                if text.startswith("data:"):
                    env = json.loads(text.removeprefix("data:").strip())
                    events.append(env)
                    if len(events) >= 12:
                        break
            diagnostics = [e for e in events if e["type"] == "diagnostic"]
            assert any(
                d["code"] == "wifi.unavailable" and d["severity"] == "info"
                for d in diagnostics
            ), [e["type"] for e in events]
    finally:
        client.post(f"/api/sessions/{session['id']}/discard")


# ---- 校验与 404（双模式同径）----


@pytest.mark.parametrize("payload", [{}, {"action": "toggle"}, {"action": None}])
def test_wifi_mode_invalid_action_400(client: TestClient, payload: dict) -> None:
    resp = client.post("/api/devices/synthetic-001/wifi-mode", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"]["field"] == "action"


def test_wifi_mode_connect_requires_host(client: TestClient) -> None:
    for payload in ({"action": "connect"}, {"action": "connect", "host": ""}, {"action": "connect", "host": 42}):
        resp = client.post("/api/devices/synthetic-001/wifi-mode", json=payload)
        assert resp.status_code == 400, payload
        error = resp.json()["error"]
        assert error["code"] == "validation_error"
        assert error["details"]["field"] == "host"


def test_wifi_mode_unknown_device_404(client: TestClient) -> None:
    resp = client.post("/api/devices/ghost/wifi-mode", json={"action": "enable"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "device_not_found"


# ---- real 模式（mock adb 命令序列断言）----


def _make_real_wifi_provider(responses: list | None = None):
    """real provider + FakeAdb（list_devices 枚举 monkeypatch run_preflight）。"""
    import app.providers.real as real_module

    adb = FakeAdb(responses or default_probe_responses())
    provider = real_module.RealProvider(adb=adb)
    return provider, adb


def test_wifi_enable_command_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    """enable：`adb -s <serial> tcpip 5555`（重启 adbd 于 TCP 模式）+ golden 响应。"""
    import app.providers.real as real_module

    adb = FakeAdb(default_probe_responses())
    provider = real_module.RealProvider(adb=adb)
    monkeypatch.setattr(real_module, "run_preflight", lambda _adb: fake_preflight_result())

    result = provider.wifi_enable("R3CN80ABCDEF")
    assert result == {
        "deviceId": "R3CN80ABCDEF", "action": "enable",
        "state": "tcpip_enabled", "port": 5555,
    }
    tcpip_calls = [c for c in adb.commands if "tcpip" in c]
    assert tcpip_calls == [["tcpip", "5555", "R3CN80ABCDEF"]]


def test_wifi_connect_command_sequence_and_wifi_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect：`adb connect <host>:5555` + golden 响应 + GET /api/devices 的
    WiFi 独立行（同 serial、connection=wifi，契约 §21.1 行并存语义）。"""
    import app.providers.real as real_module

    adb = FakeAdb(default_probe_responses())
    provider = real_module.RealProvider(adb=adb)
    monkeypatch.setattr(real_module, "run_preflight", lambda _adb: fake_preflight_result())

    result = provider.wifi_connect("R3CN80ABCDEF", "192.168.1.23")
    # 契约 §21.1 connect 响应 golden（独立设备 id = serial + "-wifi"）
    assert result == {
        "deviceId": "R3CN80ABCDEF-wifi", "serial": "R3CN80ABCDEF",
        "action": "connect", "host": "192.168.1.23", "port": 5555,
        "connection": "wifi",
    }
    connect_calls = [c for c in adb.commands if c and c[0] == "connect"]
    assert connect_calls == [["connect", "192.168.1.23:5555", None]]

    # 连接成功后 GET /api/devices：USB 行与 WiFi 行并存（同 serial、独立 id）
    devices = provider.list_devices()
    rows = {d["id"]: d for d in devices}
    assert rows["R3CN80ABCDEF"]["connection"] == "usb"
    wifi_row = rows["R3CN80ABCDEF-wifi"]
    assert wifi_row["connection"] == "wifi"
    assert wifi_row["serial"] == "R3CN80ABCDEF"
    assert wifi_row["profile"]["wifi"] is True
    assert wifi_row["profile"]["usb"] is False


def test_wifi_connect_failure_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """adb connect 执行失败 → 503 wifi_connect_failed（§3 M6 新增行）。"""
    import app.providers.real as real_module

    adb = FakeAdb([
        FakeAdb.when("connect", "failed to connect to 192.168.1.23:5555\n"),
    ])
    provider = real_module.RealProvider(adb=adb)
    monkeypatch.setattr(real_module, "run_preflight", lambda _adb: fake_preflight_result())

    with pytest.raises(real_module.ApiError) as exc_info:
        provider.wifi_connect("R3CN80ABCDEF", "192.168.1.23")
    assert exc_info.value.code == "wifi_connect_failed"
    assert exc_info.value.status == 503


def test_wifi_endpoint_real_mode_via_app(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """端点级 real 形态：FakeRealProvider + 注入 wifi host 行为（enable→connect）。"""
    from app.config import Settings
    from app.main import create_app
    from app.providers.real import RealProvider
    from fastapi.testclient import TestClient as TC

    import app.providers.real as real_module

    adb = FakeAdb(default_probe_responses())
    provider = RealProvider(adb=adb)
    monkeypatch.setattr(real_module, "run_preflight", lambda _adb: fake_preflight_result())

    settings = Settings(
        provider="real", runs_dir=tmp_path / "runs",
        cases_db_path=tmp_path / "data" / "cases.db",
    )
    app = create_app(settings, provider=provider)
    with TC(app) as client:
        resp = client.post(
            "/api/devices/R3CN80ABCDEF/wifi-mode", json={"action": "enable"}
        )
        assert resp.status_code == 200, resp.text
        # golden：enable 响应
        assert resp.json() == {
            "deviceId": "R3CN80ABCDEF", "action": "enable",
            "state": "tcpip_enabled", "port": 5555,
        }
        resp = client.post(
            "/api/devices/R3CN80ABCDEF/wifi-mode",
            json={"action": "connect", "host": "192.168.1.23"},
        )
        assert resp.status_code == 200, resp.text
        # golden：connect 响应
        assert resp.json() == {
            "deviceId": "R3CN80ABCDEF-wifi", "serial": "R3CN80ABCDEF",
            "action": "connect", "host": "192.168.1.23", "port": 5555,
            "connection": "wifi",
        }
        # 设备列表两行并存
        listing = client.get("/api/devices").json()
        ids = [d["id"] for d in listing["devices"]]
        assert "R3CN80ABCDEF-wifi" in ids
        # 连接失败 → 503 wifi_connect_failed（改应答表为失败输出）
        adb.responses = [
            FakeAdb.when("connect", "cannot connect to 192.168.1.99:5555\n"),
        ]
        bad = client.post(
            "/api/devices/R3CN80ABCDEF/wifi-mode",
            json={"action": "connect", "host": "192.168.1.99"},
        )
        assert bad.status_code == 503
        assert bad.json()["error"]["code"] == "wifi_connect_failed"
