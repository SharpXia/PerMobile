"""装配流 S4 合流单测（契约 §26.1 装配语义 / §23.1 聚合 sim-boot / §20.3 判定 mode 改造）。

与 test_simulator_flow / test_real_ios_provider 同哲学：fixture provider 注入驱动
**生产装配链**（CompositeProvider 三源聚合、按源分发、判定通道、落盘平台、PATCH
平台门），无真机/模拟器依赖，如实声明：

- devicectl 语料复用 S1 执行面 golden（tests/fixtures/realios/，2026-11-16 真机
  iPhone 15 Pro Max iOS 27.2.1 xcrun 651.32 脱敏实测）；真机 iOS 源直接换装 S1
  ``RealIOSProvider``（S4 合流点不重复实现 devicectl 命令面）；
- 采集执行面为 S1 ``realios_sampler.RealIOSCollector``（§26.4 窗口批采样）——
  分流断言走真实类面（loader 懒加载已激活）；S1 未落地路径（501）以 monkeypatch
  loader 覆盖；
- Android 采集器（RealCollector）不触 adb：会话经 manager 直建（不构造采集器），
  分流断言调 create_collector 纯构造（不 start）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings, VALID_PROVIDERS  # noqa: E402
from app.errors import ApiError  # noqa: E402
from app.main import create_app  # noqa: E402
from app.providers.base import DeviceProvider  # noqa: E402
from app.providers.composite import (  # noqa: E402
    CompositeProvider,
    channel_platform,
    device_channel,
    provider_device_mode,
)
from app.providers.realios import RealIOSProvider  # noqa: E402
from app.providers.simulator import SimulatorProvider  # noqa: E402
from app.providers.synthetic import SyntheticProvider  # noqa: E402
from app.services.collector_service import (  # noqa: E402
    RealCollector,
    SyntheticCollector,
    _load_real_ios_collector,
    create_collector,
)
from app.services.realios_sampler import RealIOSCollector  # noqa: E402
from app.services.simulator_sampler import SimulatorCollector  # noqa: E402
from perfeval.collectors.android.samples import SampleRecord  # noqa: E402

REALIOS_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "realios"

# S1 golden 设备 id（hardwareProperties.udid——xctrace --device 只认 UDID）与
# 模拟器行（UDID；devicectl 输出中 reality="simulated"/transport sameMachine，
# §26.1 三过滤规则拦下，避免与 simctl 源双源重复行）
IPHONE_ID = "00008130-000955B7C2D1E84A"
SIMULATED_UDID_IN_DEVICECTL = "00000000-0000-4000-8000-00000000009B"
WATCH_ID = "00008310-000D55AA11BB77CD"
UNPAIRED_ECID_ID = "8D3E5A70-C1F4-59B2-A6D8-2E4F6A8C0D1E"
ANDROID_SERIAL = "R3CN80ABCDEF"
ANDROID_WIFI_ID = f"{ANDROID_SERIAL}-wifi"
SIM_UDID = "00000000-0000-4000-8000-00000000009D"
IOS_APP = "com.perfeval.agenttool"
TARGET_APP = "com.example.iosapp"

DEVICE_SAMPLE = json.loads((REALIOS_FIXTURES / "devices-sample.json").read_text())
APPS_RAW = (REALIOS_FIXTURES / "apps-raw.json").read_text()
PROCESSES_RAW = (REALIOS_FIXTURES / "processes-raw.json").read_text()
LAUNCH_RAW = (REALIOS_FIXTURES / "launch-raw.json").read_text()


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> Any:
    result = type("_R", (), {})()
    result.stdout = stdout
    result.returncode = returncode
    result.stderr = stderr
    return result


def _trimmed_devices_json() -> str:
    """S1 golden 设备语料修剪：保留在网 usb 行 + watchOS/模拟器/未配对行
    （聚合过滤断言用）；同 UDID 的 localNetwork 离线行剔除（get_device 取首行）。"""
    body = json.loads(json.dumps(DEVICE_SAMPLE))  # 深拷贝
    rows = body["result"]["devices"]
    keep = [
        r for r in rows
        if ((r.get("properties") or {}).get("connection") or r.get("connectionProperties") or {})
        .get("tunnelState") == "connected"
        or ((r.get("properties") or {}).get("hardware") or r.get("hardwareProperties") or {})
        .get("platform") != "iOS"
    ]
    body["result"]["devices"] = keep
    return json.dumps(body, ensure_ascii=False)


def make_devicectl_runner(
    *, launch_stdout: str | None = None, fail_list: bool = False
) -> Any:
    """devicectl 假 runner：list 回 S1 golden（修剪版）；apps/processes/launch 回
    各自 golden 原文（run_devicectl_json 的 --json-output 落盘文件在假 runner 下
    不存在 → 回退 stdout，生产代码同路径消费）。"""

    def runner(command: list[str], timeout: float) -> Any:
        args = command[2:]  # 剥 ["xcrun", "devicectl"]
        if fail_list:
            raise FileNotFoundError("xcrun: No such file or directory")
        if args[:2] == ["list", "devices"]:
            return _completed(_trimmed_devices_json())
        if args[:3] == ["device", "info", "apps"]:
            return _completed(APPS_RAW)
        if args[:3] == ["device", "info", "processes"]:
            return _completed(PROCESSES_RAW)
        if args[:3] == ["device", "process", "launch"]:
            return _completed(
                launch_stdout if launch_stdout is not None else LAUNCH_RAW
            )
        return _completed("")

    return runner


SIM_DEVICES_JSON = json.dumps({
    "devices": {
        "com.apple.CoreSimulator.SimRuntime.iOS-27-0": [
            {"udid": SIM_UDID, "isAvailable": True, "state": "Booted",
             "name": "iPhone 17 Pro"},
        ]
    }
})


def make_simctl_runner() -> Any:
    """simctl 假 runner（simulator 源）：设备枚举/listapps/launchctl/bootstatus。"""

    def runner(command: list[str], timeout: float) -> Any:
        args = command[2:]  # 剥 ["xcrun", "simctl"]
        if args[:3] == ["list", "devices", "--json"]:
            return _completed(SIM_DEVICES_JSON)
        if args[:1] == ["listapps"]:
            return _completed(
                '{\n    "com.apple.Preferences" =     {\n'
                '        ApplicationType = System;\n'
                '        CFBundleDisplayName = Settings;\n'
                '        CFBundleIdentifier = "com.apple.Preferences";\n    };\n}\n'
            )
        if len(args) >= 3 and args[:3] == ["spawn", SIM_UDID, "launchctl"]:
            return _completed("81296\t0\tUIKitApplication:com.apple.Preferences[70d3]\n")
        if args[:1] == ["bootstatus"]:
            return _completed("Device already booted\n")
        return _completed("")

    return runner


class FakeAndroidProvider(DeviceProvider):
    """adb Android 源替身：USB 行 + WiFi 行固定两台；分发调用全量记录。"""

    mode = "real"

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail  # True → list_devices 抛 adb_not_found（单源缺失降级）

    def list_devices(self) -> list[dict[str, Any]]:
        if self.fail:
            raise ApiError("adb_not_found", "adb 不可用（fixture 注入失败）")
        return [
            {
                "id": ANDROID_SERIAL,
                "platform": "android",
                "model": "Pixel 8",
                "serial": ANDROID_SERIAL,
                "state": "online",
                "connection": "usb",
                "info": {"os": "Android 14"},
            },
            {
                "id": ANDROID_WIFI_ID,
                "platform": "android",
                "model": "Pixel 8",
                "serial": ANDROID_SERIAL,
                "state": "online",
                "connection": "wifi",
                "info": {"os": "Android 14"},
            },
        ]

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        for device in self.list_devices():
            if device["id"] == device_id:
                return device
        return None

    def get_profile(self, device_id: str) -> dict[str, Any] | None:
        from app.services.capability_matrix import device_profile

        if self.get_device(device_id) is None:
            return None
        return device_profile(
            usb=device_id == ANDROID_SERIAL,
            wifi=device_id == ANDROID_WIFI_ID,
            gpu_source="kgsl",
            cpu_cores=8,
            refresh_rate_hz=120,
            meminfo_detail=True,
            power_supply=True,
        )

    def adb_serial(self, device_id: str) -> str:
        self.calls.append(f"adb_serial:{device_id}")
        if device_id.endswith("-wifi"):
            return "192.168.1.23:5555"
        return device_id

    def wifi_enable(self, device_id: str) -> dict[str, Any]:
        self.calls.append(f"wifi_enable:{device_id}")
        return {
            "deviceId": device_id, "action": "enable",
            "state": "tcpip_enabled", "port": 5555,
        }

    def wifi_connect(self, device_id: str, host: str) -> dict[str, Any]:
        self.calls.append(f"wifi_connect:{device_id}:{host}")
        return {"deviceId": device_id, "action": "connect", "host": host, "port": 5555}

    def speedtest(self, device_id: str, *, bytes_count: int, backend_port: int,
                  max_time_s: int = 60) -> dict[str, Any]:
        self.calls.append(f"speedtest:{device_id}")
        return {"deviceId": device_id, "method": "adb.reverse.shell.curl", "mbps": 41.9}

    def launch_app(self, device_id: str, package_name: str) -> dict[str, Any]:
        self.calls.append(f"launch_app:{device_id}:{package_name}")
        return {"component": f"{package_name}/.MainActivity", "error": None}

    def list_apps(self, device_id: str) -> dict[str, Any] | None:
        if self.get_device(device_id) is None:
            return None
        return {"deviceId": device_id, "apps": [
            {"packageName": "com.example.game", "name": "Game", "pid": 1234,
             "running": True, "isForeground": True,
             "processes": [{"pid": 1234, "name": "com.example.game", "isMain": True}]},
        ]}

    def app_version(self, device_id: str, package_name: str) -> dict[str, Any] | None:
        return {"versionName": "1.2.3", "versionCode": 123}

    def list_metric_ids(self) -> list[str]:
        from app.registry import available_metric_ids

        return available_metric_ids("real")


class _BrokenSource(DeviceProvider):
    """list_devices 必抛源（单源缺失降级/id 冲突断言用）。"""

    mode = "real"

    def __init__(self, exc: Exception | None = None, rows: list | None = None) -> None:
        self._exc = exc
        self._rows = rows or []

    def list_devices(self) -> list[dict[str, Any]]:
        if self._exc is not None:
            raise self._exc
        return self._rows

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        return None

    def list_apps(self, device_id: str) -> dict[str, Any] | None:
        return None

    def list_metric_ids(self) -> list[str]:
        return []


def make_composite(*, android: DeviceProvider | None = None, **kwargs: Any) -> CompositeProvider:
    """三源聚合装配：adb 替身 + S1 RealIOSProvider（假 runner）+ simctl 假 runner。"""
    return CompositeProvider(
        android=android if android is not None else FakeAndroidProvider(),
        simulator=SimulatorProvider(host_cores=8, simctl_runner=make_simctl_runner()),
        devicectl_runner=make_devicectl_runner(**kwargs),
    )


@pytest.fixture()
def composite_client(tmp_path: Path) -> TestClient:
    """--provider real 聚合面客户端：三源 fixture 注入（无真机/模拟器依赖）。"""
    settings = Settings(
        provider="real",
        runs_dir=tmp_path / "runs",
        cases_db_path=tmp_path / "data" / "cases.db",
    )
    app = create_app(settings, provider=make_composite())
    with TestClient(app) as c:
        c.runs_dir = settings.runs_dir  # type: ignore[attr-defined]
        yield c


def _error_body(resp: Any) -> dict[str, Any]:
    body = resp.json()
    assert "error" in body, body
    return body["error"]


def _manager(client: TestClient) -> Any:
    return client.app.state.sessions  # type: ignore[attr-defined]


# ---- ① 聚合设备面（三源聚合 / 双源去重 / watchOS 过滤）----


def test_composite_device_list_aggregates_three_sources(composite_client: TestClient) -> None:
    """--provider real 聚合面：Android 真机 + WiFi 行 + devicectl 真机 + simctl
    模拟器同行共存；simulated/watchOS/未配对行不出（§26.1 三过滤 + 无
    devicectl/simctl 双源重复行）。"""
    resp = composite_client.get("/api/devices")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "real"  # 装配模式仍 real（不新增 provider 枚举值）
    by_id = {d["id"]: d for d in body["devices"]}

    # 三源设备行齐备
    assert ANDROID_SERIAL in by_id
    assert ANDROID_WIFI_ID in by_id
    assert IPHONE_ID in by_id
    assert SIM_UDID in by_id
    # devicectl 的 simulated 行与 simctl 同 UDID——聚合面零重复行；watchOS 行与
    # 未配对 ECID 行（pairingState=unsupported）不泄漏（§26.1 三过滤）
    assert len(by_id) == len(body["devices"]) == 4
    assert SIMULATED_UDID_IN_DEVICECTL not in by_id
    assert WATCH_ID not in by_id
    assert UNPAIRED_ECID_ID not in by_id

    iphone = by_id[IPHONE_ID]
    assert iphone["platform"] == "ios"
    assert iphone["connection"] == "usb"  # §26.1：transportType wired→usb
    assert iphone["state"] == "online"  # tunnelState=connected（唯一在线判据）
    assert iphone["serial"] == IPHONE_ID
    sim = by_id[SIM_UDID]
    assert sim["platform"] == "ios" and sim["connection"] == "simulator"

    # 判定通道：ios+simulator → simulator；真机/Android → real
    assert device_channel(iphone) == "real"
    assert device_channel(sim) == "simulator"
    assert channel_platform("real", iphone["platform"]) == "ios"
    assert channel_platform("real", "android") == "android"

    # health 仍如实报告聚合装配模式
    health = composite_client.get("/api/health").json()
    assert health["providers"]["devices"] == "real"


def test_composite_degrades_when_adb_and_simctl_missing(tmp_path: Path) -> None:
    """单源缺失降级：adb 源炸（adb_not_found）+ simctl 源炸（OSError）→ 聚合面
    仍 200，devicectl 真机行照常出行（不 500）。"""
    composite = CompositeProvider(
        android=FakeAndroidProvider(fail=True),
        simulator=SimulatorProvider(
            host_cores=8,
            simctl_runner=lambda command, timeout: (_ for _ in ()).throw(
                OSError("xcrun: No such file or directory")
            ),
        ),
        devicectl_runner=make_devicectl_runner(),
    )
    settings = Settings(
        provider="real",
        runs_dir=tmp_path / "runs",
        cases_db_path=tmp_path / "data" / "cases.db",
    )
    app = create_app(settings, provider=composite)
    with TestClient(app) as client:
        resp = client.get("/api/devices")
        assert resp.status_code == 200
        rows = resp.json()["devices"]
        assert [d["id"] for d in rows] == [IPHONE_ID]  # 仅健康源出行


def test_composite_all_sources_missing_still_empty_200(tmp_path: Path) -> None:
    """三源全缺（adb 不在/xcrun 不在/devicectl 不在）→ 设备面 200 空表，不 500。"""
    composite = CompositeProvider(
        android=FakeAndroidProvider(fail=True),
        simulator=SimulatorProvider(
            host_cores=8,
            simctl_runner=lambda command, timeout: (_ for _ in ()).throw(
                OSError("xcrun: No such file or directory")
            ),
        ),
        devicectl_runner=make_devicectl_runner(fail_list=True),
    )
    settings = Settings(
        provider="real",
        runs_dir=tmp_path / "runs2",
        cases_db_path=tmp_path / "data2" / "cases.db",
    )
    app = create_app(settings, provider=composite)
    with TestClient(app) as client:
        resp = client.get("/api/devices")
        assert resp.status_code == 200
        assert resp.json()["devices"] == []


def test_composite_id_conflict_keeps_first_source(tmp_path: Path) -> None:
    """设备 id 跨源冲突防护：同 id 双源 → 仅先到源（android 优先序最高）出行，
    分发路由到先到源（不炸列表、不双行）。"""
    android = FakeAndroidProvider()
    impostor = _BrokenSource(rows=[{
        "id": ANDROID_SERIAL, "platform": "ios", "model": "impostor",
        "serial": ANDROID_SERIAL, "state": "online", "connection": "wifi",
        "info": {},
    }])
    composite = CompositeProvider(
        android=android,
        ios_real=impostor,
        simulator=SimulatorProvider(host_cores=8, simctl_runner=make_simctl_runner()),
    )
    rows = composite.list_devices()
    assert [d["id"] for d in rows].count(ANDROID_SERIAL) == 1
    row = next(d for d in rows if d["id"] == ANDROID_SERIAL)
    assert row["platform"] == "android"  # 先到源（android）保留
    # 分发跟随先到源：adb_serial 路由 android 源，不落 iOS 短路分支
    assert composite.adb_serial(ANDROID_SERIAL) == ANDROID_SERIAL
    assert f"adb_serial:{ANDROID_SERIAL}" in android.calls


# ---- ③ 判定 mode 按设备源通道（/api/metrics）----


def test_metrics_registry_by_device_channel(composite_client: TestClient) -> None:
    """/api/metrics?deviceId=：mode 按源通道——模拟器设备双指标可勾（simulator
    通道）、真机 iOS 设备 §26.3 双指标可勾（real 通道 + 真机 profile 交集）、
    Android 设备走 real 通道；无 deviceId 时按装配模式 real。"""
    sim = composite_client.get("/api/metrics", params={"deviceId": SIM_UDID}).json()
    assert sim["mode"] == "simulator"
    by_id = {m["metricId"]: m for m in sim["metrics"]}
    assert by_id["cpu.proc.host.pct"]["available"] is True
    assert by_id["memory.rss.mb"]["available"] is True
    assert by_id["cpu.total.pct"]["available"] is False
    assert by_id["cpu.total.pct"]["capabilities"]["reason"] == "platform_unsupported"

    iphone = composite_client.get("/api/metrics", params={"deviceId": IPHONE_ID}).json()
    assert iphone["mode"] == "real"  # 真机 iOS 走 real 通道（§20.3 判定 mode 来源改造）
    i_by_id = {m["metricId"]: m for m in iphone["metrics"]}
    assert i_by_id["cpu.proc.xctrace.pct"]["available"] is True
    assert i_by_id["memory.footprint.mb"]["available"] is True
    assert i_by_id["cpu.proc.host.pct"]["available"] is False
    assert i_by_id["cpu.total.pct"]["capabilities"]["reason"] == "platform_unsupported"

    android = composite_client.get(
        "/api/metrics", params={"deviceId": ANDROID_SERIAL}
    ).json()
    assert android["mode"] == "real"
    a_by_id = {m["metricId"]: m for m in android["metrics"]}
    assert a_by_id["cpu.total.pct"]["available"] is True
    assert a_by_id["cpu.proc.xctrace.pct"]["available"] is False

    plain = composite_client.get("/api/metrics").json()
    assert plain["mode"] == "real"


# ---- ⑤ 会话链路：采集器分流 / 缺省指标集 ----


def test_session_on_ios_real_device_dispatches_real_ios_collector(
    composite_client: TestClient,
) -> None:
    """真机 iOS 会话分流 RealIOSCollector（S1 realios_sampler 类面，loader 懒加载
    已激活；provider_mode 仍 real，分流按设备平台而非装配模式）。"""
    assert _load_real_ios_collector() is RealIOSCollector  # S1 落点已接通
    resp = composite_client.post("/api/sessions", json={
        "deviceId": IPHONE_ID,
        "packageName": TARGET_APP,
        "metrics": [],
    })
    assert resp.status_code == 201, resp.text
    stored = _manager(composite_client).get(resp.json()["id"])
    assert isinstance(stored.collector, RealIOSCollector)
    assert stored.provider_mode == "real"  # 聚合面装配模式不变
    assert stored.device_platform == "ios"
    assert stored.device_channel == "real"
    composite_client.post(f"/api/sessions/{resp.json()['id']}/discard")

    # 单源回归面：android 平台 real 会话仍分流 RealCollector（纯构造，不 start）
    from app.sse import SseBus

    def _stub(platform: str) -> Any:
        return type(
            "S", (),
            {"id": "sess-x", "device_id": "dev-x", "device_platform": platform,
             "metrics": {}},
        )()

    assert isinstance(
        create_collector(_stub("android"), "real", SseBus("sess-x")), RealCollector
    )
    assert isinstance(
        create_collector(_stub("ios"), "real", SseBus("sess-x")), RealIOSCollector
    )
    assert isinstance(
        create_collector(_stub("android"), "synthetic", SseBus("sess-x")),
        SyntheticCollector,
    )


def test_session_on_ios_real_device_unwired_rejected_501(
    composite_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1 执行面未落地（接缝防御面）：真机 iOS 会话创建 501 not_implemented
    契约错误码 + diagnostic（不 AttributeError→500、不静默无数据）。"""
    import app.services.collector_service as collector_service

    monkeypatch.setattr(collector_service, "_load_real_ios_collector", lambda: None)
    resp = composite_client.post("/api/sessions", json={
        "deviceId": IPHONE_ID,
        "packageName": TARGET_APP,
        "metrics": [],
    })
    assert resp.status_code == 501
    error = _error_body(resp)
    assert error["code"] == "not_implemented"
    assert error["details"]["diagnostic"]["code"] == "collector.ios_real.unavailable"


def test_session_on_simulator_device_dispatches_simulator_collector(
    composite_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """聚合面内模拟器设备开会话 → SimulatorCollector（宿主口径链路零新增；
    provider_mode="real" 不误入 adb/xctrace 采样链）。"""
    monkeypatch.setattr(SimulatorCollector, "_target_pid_via_simctl", lambda self: 81296)
    monkeypatch.setattr(SimulatorCollector, "_sample_ps", lambda self, pid: (12.5, 453.2))
    resp = composite_client.post("/api/sessions", json={
        "deviceId": SIM_UDID,
        "packageName": TARGET_APP,
        "metrics": [],
    })
    assert resp.status_code == 201, resp.text
    stored = _manager(composite_client).get(resp.json()["id"])
    assert isinstance(stored.collector, SimulatorCollector)
    assert stored.device_channel == "simulator"
    assert stored.device_platform == "ios"
    composite_client.post(f"/api/sessions/{resp.json()['id']}/discard")


def test_session_default_metrics_follow_channel(composite_client: TestClient) -> None:
    """缺省指标集按通道取全集再过设备交集：真机 iOS 会话缺省只挂 §26.3 双指标
    （聚合面 provider_mode=real 不再把 Android 指标挂进 iOS 会话）、模拟器设备
    缺省只挂宿主口径双指标。"""
    session = _manager(composite_client).create(
        device_id=IPHONE_ID, package_name=TARGET_APP, metric_entries=None
    )
    assert set(session.metrics) == {"cpu.proc.xctrace.pct", "memory.footprint.mb"}

    sim = _manager(composite_client).create(
        device_id=SIM_UDID, package_name=TARGET_APP, metric_entries=None
    )
    assert set(sim.metrics) == {"cpu.proc.host.pct", "memory.rss.mb"}
    _manager(composite_client).discard(session)
    _manager(composite_client).discard(sim)


# ---- ⑤ PATCH /metrics 平台门（堵跨平台静默无数据）----


def _make_patch_session(client: TestClient, device_id: str) -> Any:
    """manager 直建会话（不构造采集器，不触 adb/xctrace）+ stub set_enabled。"""
    session = _manager(client).create(
        device_id=device_id, package_name="com.example.any", metric_entries=[]
    )
    session.collector = type(
        "C", (), {"set_enabled": staticmethod(lambda ids: session.set_metrics(ids))}
    )()
    return session


def test_patch_metrics_android_session_rejects_ios_metric(
    composite_client: TestClient,
) -> None:
    """Android 会话 PATCH 启用真机 iOS 指标（cpu.proc.xctrace.pct，§26.3 注册行）
    → 409 metric_unavailable（real 通道模式级通过 → 设备 profile 交集门拒，
    reason=platform_unsupported）——「Android 采集器无映射 → 静默无数据」路径封死。"""
    session = _make_patch_session(composite_client, ANDROID_SERIAL)
    resp = composite_client.patch(
        f"/api/sessions/{session.id}/metrics",
        json={"enable": [{"metricId": "cpu.proc.xctrace.pct"}]},
    )
    assert resp.status_code == 409
    error = _error_body(resp)
    assert error["code"] == "metric_unavailable"
    assert error["details"]["metricId"] == "cpu.proc.xctrace.pct"
    assert error["details"]["reason"] == "platform_unsupported"

    # 模拟器口径指标（modes=("simulator",)）在 real 通道模式级即不可用
    resp2 = composite_client.patch(
        f"/api/sessions/{session.id}/metrics",
        json={"enable": [{"metricId": "cpu.proc.host.pct"}]},
    )
    assert resp2.status_code == 409
    assert _error_body(resp2)["code"] == "metric_unavailable"


def test_patch_metrics_ios_real_session_gates(
    composite_client: TestClient,
) -> None:
    """真机 iOS 会话：启用 §26.3 双指标正常（本平台）；启用 Android 指标/
    模拟器指标同拒（platform/modes 交集判定）。"""
    session = _make_patch_session(composite_client, IPHONE_ID)
    ok = composite_client.patch(
        f"/api/sessions/{session.id}/metrics",
        json={"enable": [{"metricId": "cpu.proc.xctrace.pct", "intervalMs": 1000}]},
    )
    assert ok.status_code == 200, ok.text
    ids = {m["metricId"]: m["intervalMs"] for m in ok.json()["metrics"]}
    assert ids.get("cpu.proc.xctrace.pct") == 1000

    for foreign in ("cpu.total.pct", "cpu.proc.host.pct"):
        resp = composite_client.patch(
            f"/api/sessions/{session.id}/metrics",
            json={"enable": [{"metricId": foreign}]},
        )
        assert resp.status_code == 409, (foreign, resp.text)
        assert _error_body(resp)["code"] == "metric_unavailable"
        assert _error_body(resp)["details"]["metricId"] == foreign


def test_patch_metrics_ios_simulator_session_rejects_android_metric(
    composite_client: TestClient,
) -> None:
    """模拟器通道会话 PATCH：Android 指标拒（模式级）、宿主口径双指标正常。"""
    session = _make_patch_session(composite_client, SIM_UDID)
    resp = composite_client.patch(
        f"/api/sessions/{session.id}/metrics",
        json={"enable": [{"metricId": "cpu.total.pct"}]},
    )
    assert resp.status_code == 409
    error = _error_body(resp)
    assert error["code"] == "metric_unavailable"
    assert error["details"]["metricId"] == "cpu.total.pct"

    ok = composite_client.patch(
        f"/api/sessions/{session.id}/metrics",
        json={"enable": [{"metricId": "cpu.proc.host.pct", "intervalMs": 800}]},
    )
    assert ok.status_code == 200, ok.text
    ids = {m["metricId"]: m["intervalMs"] for m in ok.json()["metrics"]}
    assert ids.get("cpu.proc.host.pct") == 800


def test_patch_metrics_android_session_allows_native_metric(
    composite_client: TestClient,
) -> None:
    """本平台指标 PATCH 正常（Android 会话启 cpu.proc.pct → 200）。"""
    session = _make_patch_session(composite_client, ANDROID_SERIAL)
    ok = composite_client.patch(
        f"/api/sessions/{session.id}/metrics",
        json={"enable": [{"metricId": "cpu.proc.pct", "intervalMs": 500}]},
    )
    assert ok.status_code == 200, ok.text
    ids = {m["metricId"] for m in ok.json()["metrics"]}
    assert "cpu.proc.pct" in ids


# ---- ④ 设备级 duck-typed 方法按源分发（聚合面 500 防护）----


def test_ios_device_wifi_mode_and_speedtest_contract_errors(
    composite_client: TestClient,
) -> None:
    """iOS 设备（真机/simulator 通道）调 wifi-mode / speedtest → 501 契约错误码
    + diagnostic（非 500 internal_error；Android 设备分发 adb 源行为不变）。"""
    for device_id in (IPHONE_ID, SIM_UDID):
        resp = composite_client.post(
            f"/api/devices/{device_id}/wifi-mode", json={"action": "enable"}
        )
        assert resp.status_code == 501, (device_id, resp.text)
        error = _error_body(resp)
        assert error["code"] == "not_implemented"
        assert error["details"]["diagnostic"]["code"] == "wifi.unavailable"

        resp = composite_client.post(f"/api/devices/{device_id}/speedtest", json={})
        assert resp.status_code == 501, (device_id, resp.text)
        error = _error_body(resp)
        assert error["code"] == "not_implemented"
        assert error["details"]["diagnostic"]["code"] == "speedtest.unavailable"

    # real 通道 Android 设备：门放行 → 分发 adb 源（命令面行为与改造前一致）
    ok = composite_client.post(
        f"/api/devices/{ANDROID_SERIAL}/wifi-mode", json={"action": "enable"}
    )
    assert ok.status_code == 200
    assert ok.json()["state"] == "tcpip_enabled"


def test_wifi_row_serial_dispatch_and_ios_short_circuit(
    composite_client: TestClient,
) -> None:
    """adb_serial 按源分发：WiFi 行映射回 host:port（经 adb 源）；iOS 设备短路
    返回 None，不分发到 adb 源（udid 不冒充 serial）。"""
    android = composite_client.app.state.provider._android  # type: ignore[attr-defined]
    provider = composite_client.app.state.provider  # type: ignore[attr-defined]
    assert provider.adb_serial(ANDROID_WIFI_ID) == "192.168.1.23:5555"
    assert f"adb_serial:{ANDROID_WIFI_ID}" in android.calls
    calls_before = len(android.calls)
    assert provider.adb_serial(IPHONE_ID) is None
    assert provider.adb_serial(SIM_UDID) is None
    assert len(android.calls) == calls_before  # iOS id 未触 adb 源


def test_device_apps_dispatch(composite_client: TestClient) -> None:
    """应用面按源分发：真机 iOS 走 S1 devicectl apps+processes 合成面（§26.2，
    200 出行）；simulator 行走 simctl 源；Android 走 adb 源替身。"""
    ios = composite_client.get(f"/api/devices/{IPHONE_ID}/apps")
    assert ios.status_code == 200, ios.text
    apps = {a["packageName"]: a for a in ios.json()["apps"]}
    assert IOS_APP in apps  # S1 golden 装机清单出行
    assert set(apps[IOS_APP]) >= {"packageName", "name", "pid", "running", "processes"}

    sim = composite_client.get(f"/api/devices/{SIM_UDID}/apps")
    assert sim.status_code == 200
    assert {a["packageName"] for a in sim.json()["apps"]} == {"com.apple.Preferences"}

    android = composite_client.get(f"/api/devices/{ANDROID_SERIAL}/apps")
    assert android.status_code == 200
    assert android.json()["apps"][0]["packageName"] == "com.example.game"


def test_composite_launch_dispatch(composite_client: TestClient) -> None:
    """launchOnCreate 分流（§26.2）：真机 iOS → devicectl process launch
    （S1 JSON 形态 → component+pid；无 pid 输出 → error 文本透传，SessionManager
    以 phase failed + 诊断兜底）。"""
    provider = composite_client.app.state.provider  # type: ignore[attr-defined]
    ok = provider.launch_app(IPHONE_ID, IOS_APP)
    assert ok["component"] == IOS_APP
    assert ok["error"] is None
    assert ok["pid"] == 15436  # S1 golden：result.process.processIdentifier

    provider._ios_real._runner = make_devicectl_runner(launch_stdout="not-json")
    failed = provider.launch_app(IPHONE_ID, IOS_APP)
    assert failed["component"] is None
    assert failed["error"].startswith("devicectl launch failed")


# ---- ⑤ sim-boot 聚合语义（§23.1/§26.1 golden）----


def test_sim_boot_aggregate_semantics(composite_client: TestClient, tmp_path: Path) -> None:
    """sim-boot：聚合面内 simulator 设备 200；real 通道设备（Android/真机 iOS）
    400 validation_error（§26.1 golden 错误体 deviceId/connection）；synthetic
    单源仍 501 + 诊断（行为保持）。"""
    resp = composite_client.post(f"/api/devices/{SIM_UDID}/sim-boot")
    assert resp.status_code == 200
    assert resp.json() == {"deviceId": SIM_UDID, "action": "boot", "state": "online"}

    resp = composite_client.post(f"/api/devices/{IPHONE_ID}/sim-boot")
    assert resp.status_code == 400
    error = _error_body(resp)
    assert error["code"] == "validation_error"
    assert error["message"] == (
        "sim-boot 仅对 simulator 连接设备有意义"
        "（usb|wifi 连接的 Android/iOS 真机行无 boot 语义，契约 §23.1/§26.1）"
    )
    assert error["details"]["deviceId"] == IPHONE_ID
    assert error["details"]["connection"] == "usb"

    resp = composite_client.post(f"/api/devices/{ANDROID_SERIAL}/sim-boot")
    assert resp.status_code == 400
    error = _error_body(resp)
    assert error["code"] == "validation_error"
    assert error["details"]["connection"] == "usb"

    settings = Settings(
        provider="synthetic",
        runs_dir=tmp_path / "runs3",
        cases_db_path=tmp_path / "data3" / "cases.db",
    )
    app = create_app(settings, provider=SyntheticProvider())
    with TestClient(app) as client:
        resp = client.post("/api/devices/synthetic-001/sim-boot")
        assert resp.status_code == 501
        error = _error_body(resp)
        assert error["code"] == "not_implemented"
        assert error["details"]["diagnostic"]["code"] == "sim-boot.unavailable"


# ---- ⑤ 落盘平台按设备源通道推导 ----


def _emit(session: Any, value: float) -> None:
    session.append_record(
        SampleRecord(
            metric_id="cpu.proc.xctrace.pct", value=value,
            ts_ms=session.created_at + 10, tags={},
        )
    )


def test_saved_platform_real_ios_is_ios(composite_client: TestClient) -> None:
    """真机 iOS 会话 save：timeline/summary 平台=ios（聚合面 provider_mode=real
    不错标 android）。"""
    session = _manager(composite_client).create(
        device_id=IPHONE_ID, package_name=TARGET_APP, metric_entries=[]
    )
    assert session.provider_mode == "real"
    assert session.device_platform == "ios"
    _emit(session, 12.5)
    _emit(session, 13.5)
    saved = _manager(composite_client).save(session, "s4_ios_real")
    run_dir = Path(saved["runPath"])
    timeline = [
        json.loads(line)
        for line in (run_dir / "timeline.ndjson").read_text().splitlines() if line
    ]
    assert timeline
    assert {e["source"]["platform"] for e in timeline} == {"ios"}
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["schemaVersion"].startswith("perfeval.summary.v1")


def test_saved_platform_simulator_and_android_unchanged(
    composite_client: TestClient,
) -> None:
    """模拟器会话仍 ios、Android 会话仍 android（单源语义逐字段不变）。"""
    sim = _manager(composite_client).create(
        device_id=SIM_UDID, package_name=TARGET_APP, metric_entries=[]
    )
    assert sim.device_platform == "ios"
    sim.append_record(SampleRecord(
        metric_id="cpu.proc.host.pct", value=1.0, ts_ms=sim.created_at + 10, tags={},
    ))
    saved = _manager(composite_client).save(sim, "s4_ios_sim")
    timeline = [
        json.loads(line)
        for line in (Path(saved["runPath"]) / "timeline.ndjson").read_text().splitlines()
        if line
    ]
    assert {e["source"]["platform"] for e in timeline} == {"ios"}

    android = _manager(composite_client).create(
        device_id=ANDROID_SERIAL, package_name="com.example.game", metric_entries=[]
    )
    assert android.device_platform == "android"
    android.append_record(SampleRecord(
        metric_id="cpu.total.pct", value=3.0, ts_ms=android.created_at + 10, tags={},
    ))
    saved = _manager(composite_client).save(android, "s4_android")
    timeline = [
        json.loads(line)
        for line in (Path(saved["runPath"]) / "timeline.ndjson").read_text().splitlines()
        if line
    ]
    assert {e["source"]["platform"] for e in timeline} == {"android"}


# ---- 真机 iOS 未接线面（录屏/日志流/截图，契约 §26）----


def test_real_ios_unwired_faces_honest_rejections(
    composite_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真机 iOS：recordScreen 创建诊断 + record/start 不构造 Android 录屏器；
    logLevel 日志流广播 logstream.unavailable；截图 501 not_implemented。"""
    class Quiet:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    import app.services.collector_service as collector_service

    monkeypatch.setattr(collector_service, "_load_real_ios_collector", lambda: Quiet)

    # recordScreen=true：会话照常创建 + screenrecord.unavailable 诊断（不阻断）
    resp = composite_client.post("/api/sessions", json={
        "deviceId": IPHONE_ID, "packageName": TARGET_APP,
        "metrics": [], "recordScreen": True,
    })
    assert resp.status_code == 201
    sid = resp.json()["id"]
    stored = _manager(composite_client).get(sid)
    events, _ = stored.bus.replay(0)
    assert any(
        e["type"] == "diagnostic" and e["code"] == "screenrecord.unavailable"
        for e in events
    )
    composite_client.post(f"/api/sessions/{sid}/record/start")
    assert stored.recorder is None  # 未构造 Android 录屏器（factory 拒绝降级为诊断）
    composite_client.post(f"/api/sessions/{sid}/record/stop")
    composite_client.post(f"/api/sessions/{sid}/discard")

    # logLevel：logstream.unavailable 诊断（logcat 对 iOS 设备无意义，不静默）
    resp = composite_client.post("/api/sessions", json={
        "deviceId": IPHONE_ID, "packageName": TARGET_APP,
        "metrics": [], "logLevel": "debug",
    })
    assert resp.status_code == 201
    stored = _manager(composite_client).get(resp.json()["id"])
    events, _ = stored.bus.replay(0)
    assert any(
        e["type"] == "diagnostic" and e["code"] == "logstream.unavailable"
        for e in events
    )
    assert stored.log_channel is None
    composite_client.post(f"/api/sessions/{resp.json()['id']}/discard")

    # 截图：501 not_implemented + screenshot.unavailable 诊断（不走 screencap）
    session = _manager(composite_client).create(
        device_id=IPHONE_ID, package_name=TARGET_APP, metric_entries=[]
    )
    session.collector = Quiet()
    _manager(composite_client).start_record(session)
    resp = composite_client.post(f"/api/sessions/{session.id}/screenshot")
    assert resp.status_code == 501
    assert _error_body(resp)["code"] == "not_implemented"
    events, _ = session.bus.replay(0)
    assert any(
        e["type"] == "diagnostic" and e["code"] == "screenshot.unavailable"
        for e in events
    )
    _manager(composite_client).stop_record(session)
    _manager(composite_client).discard(session)


# ---- ② 装配规则（枚举值/白名单不动）+ 通道推导单测 ----


def test_provider_enum_and_whitelist_unchanged() -> None:
    """--provider choices 与 config 白名单不新增枚举值（simulator/synthetic 单源
    行为与改造前逐字段一致的装配前提）。"""
    import run as run_module

    parser = run_module.build_parser()
    action = next(a for a in parser._actions if a.dest == "provider")
    assert tuple(action.choices) == ("auto", "real", "synthetic", "simulator")
    assert VALID_PROVIDERS == ("auto", "real", "synthetic", "simulator")


def test_device_channel_and_platform_derivation() -> None:
    """通道/落盘平台推导纯函数：simctl 模拟器行 → simulator/ios；真机 iOS usb|wifi
    行（§26.1 映射）与 Android 行 → real；落盘平台 ios/android 各归其位。"""
    sim_row = {"platform": "ios", "connection": "simulator"}
    assert device_channel(sim_row) == "simulator"
    assert channel_platform("simulator", "ios") == "ios"

    iphone_rows = (
        {"platform": "ios", "connection": "usb"},   # §26.1 wired→usb
        {"platform": "ios", "connection": "wifi"},  # §26.1 localNetwork→wifi
    )
    for row in iphone_rows:
        assert device_channel(row) == "real"
        assert channel_platform("real", row["platform"]) == "ios"

    android_row = {"platform": "android", "connection": "usb"}
    assert device_channel(android_row) == "real"
    assert channel_platform("real", "android") == "android"
    # 行缺 platform（防御）→ android（不猜 ios）
    assert channel_platform("real", None) == "android"


def test_provider_device_mode_fallback_single_source() -> None:
    """单源 provider 无 device_mode → 回退 provider.mode（simulator/synthetic/
    real 单源判定与改造前逐字段一致；RealIOSProvider 单挂亦 real）；CompositeProvider
    按设备行给通道。"""
    assert provider_device_mode(SyntheticProvider(), "synthetic-001") == "synthetic"
    assert provider_device_mode(RealIOSProvider(devicectl_runner=make_devicectl_runner()), "x") == "real"
    composite = make_composite()
    assert provider_device_mode(composite, SIM_UDID) == "simulator"
    assert provider_device_mode(composite, ANDROID_SERIAL) == "real"
    assert provider_device_mode(composite, IPHONE_ID) == "real"
    assert provider_device_mode(composite, "unknown-device") == "real"  # 回退装配模式


# ---- ⑥ simulator_sampler pid 探测实例复用 ----


def test_simulator_collector_reuses_provider_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pid 探测复用同一 SimulatorProvider 实例（不再每秒新建——_apps_cache 不失效）。"""
    created: list[Any] = []

    class CountingSimProvider:
        def __init__(self, *a: Any, **k: Any) -> None:
            created.append(self)

        def _running_bundles(self, device_id: str) -> dict[str, int]:
            return {TARGET_APP: 81296}

    import app.providers.simulator as simulator_module

    monkeypatch.setattr(simulator_module, "SimulatorProvider", CountingSimProvider)
    collector = SimulatorCollector(
        type("S", (), {"id": "s", "device_id": SIM_UDID, "package_name": TARGET_APP})(),
        None,
    )
    assert collector._target_pid_via_simctl() == 81296
    assert collector._target_pid_via_simctl() == 81296
    assert len(created) == 1  # 两次探测同一实例
    assert collector._sim_provider is created[0]
