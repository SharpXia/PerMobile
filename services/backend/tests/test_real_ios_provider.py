"""realios/devicectl 单测（R24 契约 §26）：fixture 驱动，不依赖真机在网。

解析层 golden = tests/fixtures/realios/ 下的真机原文（2026-11-16 本机实测，
xcrun 72 / devicectl 651.32 / jsonVersion 5）：

- ``devices-sample.json``：``devicectl list devices --json-output`` 脱敏实测 7 行
  （未配对 ECID 行 / watchOS 行 / 模拟器行 / 真机 localNetwork 行）+ 1 行
  合成 wired+connected 行（§26.1 golden 映射形态，README 标注合成依据）；
- ``apps-raw.json`` / ``processes-raw.json``：``device info apps``/``processes``
  脱敏实测（§26.2 合成输入）；
- ``launch-raw.json`` / ``lockstate.json`` / ``details-raw.json``：launch pid /
  lockState 预热 / details 读数脱敏实测。

provider 行为以假 devicectl runner 驱动**生产** RealIOSProvider（命令执行面
注入，simctl 假 runner 先例同哲学）；xctrace 命令序列断言见
test_real_ios_sampler.py（窗口链路级）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "realios"
# 真机行实测值（本机 PerfEval iPhone，iPhone 15 Pro Max / iOS 27.2.1）
REAL_UDID = "00008130-000955B7C2D1E84A"
REAL_IDENTIFIER = "6F2A9C41-B8D3-5E07-94A6-1C3D5E7F9B20"  # coredevice UUID（≠UDID）
WATCH_IDENTIFIER = "B4E81C92-7A05-5D3F-8E2B-9C41D6F0A735"
UNPAIRED_IDENTIFIER = "8D3E5A70-C1F4-59B2-A6D8-2E4F6A8C0D1E"
SIM_BOOTED_IDENTIFIER = "00000000-0000-4000-8000-00000000009D"


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> Any:
    result = type("_R", (), {})()
    result.stdout = stdout
    result.returncode = returncode
    result.stderr = stderr
    return result


def _fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


# ---- §26.1 设备行过滤（golden 三规则，硬性）----


def test_parse_devices_golden_fixture_filters() -> None:
    """真机原文 7 行 → 仅 1 行真机 iPhone 出行（watch/未配对/模拟器全拦）。"""
    from app.providers.devicectl import parse_devices_json

    devices = parse_devices_json(_fixture_text("devices-sample.json"))
    # 8 行输入（7 真机原文 + 1 合成 wired 行）→ 2 行真机 iOS 出行
    assert [d["id"] for d in devices] == [REAL_UDID, REAL_UDID]
    wifi_row, usb_row = devices
    assert wifi_row["connection"] == "wifi"
    assert usb_row["connection"] == "usb"
    assert all(d["platform"] == "ios" for d in devices)
    # 行序不变：localNetwork 真机行在前（tunnelState=disconnected → offline）
    assert wifi_row["state"] == "offline"
    # 信息映射（§26.1：marketingName → model、osVersionNumber → info.os）
    assert wifi_row["model"] == "iPhone 15 Pro Max"
    assert wifi_row["info"]["os"] == "iOS 27.2.1"
    assert wifi_row["info"]["cpu"] is None  # devicectl 不暴露，不猜
    assert wifi_row["info"]["resolution"] is None
    assert wifi_row["info"]["batteryLevel"] is None
    assert wifi_row["info"]["refreshRateHz"] is None
    # id/serial = hardwareProperties.udid（identifier 是 coredevice UUID 不采用）
    assert wifi_row["serial"] == REAL_UDID


def test_parse_devices_excludes_watch_os_row() -> None:
    """规则 1：platform=="iOS" 限定——watchOS 行不出（本机实测 Watch7,2 行）。"""
    from app.providers.devicectl import parse_devices_json

    devices = parse_devices_json(_fixture_text("devices-sample.json"))
    assert all(WATCH_IDENTIFIER not in str(d) for d in devices)
    body = json.loads(_fixture_text("devices-sample.json"))
    watch = next(
        r for r in body["result"]["devices"] if r["identifier"] == WATCH_IDENTIFIER
    )
    assert watch["hardwareProperties"]["platform"] == "watchOS"  # 拦截依据存档


def test_parse_devices_excludes_unpaired_row() -> None:
    """规则 3：pairingState=="unsupported" 未配对行不出（实测 tunnel 亦 unavailable）。"""
    from app.providers.devicectl import parse_devices_json

    devices = parse_devices_json(_fixture_text("devices-sample.json"))
    assert all(UNPAIRED_IDENTIFIER not in str(d) for d in devices)
    body = json.loads(_fixture_text("devices-sample.json"))
    unpaired = next(
        r for r in body["result"]["devices"] if r["identifier"] == UNPAIRED_IDENTIFIER
    )
    cp = unpaired["connectionProperties"]
    assert cp["pairingState"] == "unsupported"  # 拦截依据存档
    assert cp["tunnelState"] == "unavailable"
    # 未配对行无 udid——若漏拦会回退 identifier 出错行，规则须在前
    assert "udid" not in unpaired["hardwareProperties"]


def test_parse_devices_excludes_simulator_rows_even_connected() -> None:
    """规则 2：reality=="simulated"/transportType=="sameMachine" 模拟器行不出。

    fixture 第 5 行为 booted 且 tunnelState=connected 的模拟器——tunnel 在线
    信号**不得**让模拟器行漏出（防与 simctl 源同 UDID 双行）。
    """
    from app.providers.devicectl import parse_devices_json

    devices = parse_devices_json(_fixture_text("devices-sample.json"))
    assert all(SIM_BOOTED_IDENTIFIER not in str(d) for d in devices)
    body = json.loads(_fixture_text("devices-sample.json"))
    sim = next(
        r for r in body["result"]["devices"] if r["identifier"] == SIM_BOOTED_IDENTIFIER
    )
    assert sim["hardwareProperties"]["reality"] == "simulated"
    assert sim["connectionProperties"]["transportType"] == "sameMachine"
    assert sim["connectionProperties"]["tunnelState"] == "connected"  # 在线仍拦


def test_parse_devices_synthetic_wired_online_row_maps_golden_shape() -> None:
    """§26.1 golden 映射形态：wired+connected → usb+online（合成行，README 存档）。"""
    from app.providers.devicectl import parse_devices_json

    devices = parse_devices_json(_fixture_text("devices-sample.json"))
    usb_row = devices[1]
    assert usb_row == {
        "id": REAL_UDID,
        "platform": "ios",
        "model": "iPhone 15 Pro Max",
        "serial": REAL_UDID,
        "state": "online",
        "connection": "usb",
        "info": {
            "os": "iOS 27.2.1",
            "cpu": None,
            "resolution": None,
            "batteryLevel": None,
            "refreshRateHz": None,
        },
    }


def test_parse_devices_state_mapping_tunnel_only() -> None:
    """映射规则：tunnelState 是唯一在线判据（connected→online，其余→offline）。

    deviceProperties.connectionState 7/7 行实测恒 None——不作信号（不猜）。
    """
    from app.providers.devicectl import parse_devices_json

    body = json.loads(_fixture_text("devices-sample.json"))
    rows = body["result"]["devices"]
    assert all(
        (r.get("deviceProperties") or {}).get("connectionState") is None
        for r in rows
    )  # 恒 None 实测存档（合成行同 None——不作信号不失效）
    devices = parse_devices_json(_fixture_text("devices-sample.json"))
    states = {d["connection"]: d["state"] for d in devices}
    assert states == {"wifi": "offline", "usb": "online"}  # disconnected/unavailable→offline


def test_parse_devices_unknown_transport_row_dropped() -> None:
    """未映射 transportType 的行不出行（不猜——宁可少一行不出错行）。"""
    from app.providers.devicectl import parse_devices_json

    body = json.loads(_fixture_text("devices-sample.json"))
    row = json.loads(json.dumps(body["result"]["devices"][6]))
    row["connectionProperties"]["transportType"] = "mysteryLink"
    body["result"]["devices"] = [row]
    assert parse_devices_json(json.dumps(body)) == []


def test_parse_devices_tolerates_properties_dict_shape() -> None:
    """jsonVersion 5 行内 *Properties 已标 deprecated（properties 字典兜底）。

    未来版本移除 deprecated 字段后解析不炸（_deprecationNotice 实测存档）。
    """
    from app.providers.devicectl import parse_devices_json

    body = json.loads(_fixture_text("devices-sample.json"))
    row = json.loads(json.dumps(body["result"]["devices"][7]))  # 合成 wired 行
    props = row.pop("properties") or {
        "hardware": row["hardwareProperties"],
        "device": row["deviceProperties"],
        "connection": row["connectionProperties"],
    }
    row["hardwareProperties"] = row["deviceProperties"] = (
        row["connectionProperties"]
    ) = None
    row["properties"] = props
    body["result"]["devices"] = [row]
    devices = parse_devices_json(json.dumps(body))
    assert len(devices) == 1
    assert devices[0]["id"] == REAL_UDID
    assert devices[0]["connection"] == "usb"


def test_parse_devices_garbage_returns_empty() -> None:
    from app.providers.devicectl import parse_devices_json

    assert parse_devices_json("not json") == []
    assert parse_devices_json("") == []


# ---- §26.2 应用/进程/launch（golden）----


def test_parse_processes_json_golden_fixture() -> None:
    from app.providers.devicectl import parse_processes_json

    processes = parse_processes_json(_fixture_text("processes-raw.json"))
    assert len(processes) == 322  # 全进程表（真机实测）
    springboard = next(p for p in processes if p["name"] == "SpringBoard")
    assert springboard["pid"] == 13678
    assert springboard["path"] == (
        "/System/Library/CoreServices/SpringBoard.app/SpringBoard"
    )
    # file URL → 路径（attach 目标名 = executable 末段）
    assert all(p["name"] and "/" not in p["name"] for p in processes)


def test_parse_apps_json_golden_fixture() -> None:
    from app.providers.devicectl import parse_apps_json

    apps = parse_apps_json(_fixture_text("apps-raw.json"))
    assert [a["packageName"] for a in apps] == [
        "com.perfeval.agenttool",
        "com.perfeval.demo",
        "com.perfeval.gamebox",
    ]
    runner = next(a for a in apps if a["packageName"] == "com.perfeval.demo")
    assert runner["name"] == "demo"
    assert runner["bundleDir"] == "Runner.app"  # bundle 目录名（匹配用）


def test_match_app_processes_by_bundle_dir() -> None:
    """processes 按 bundle 目录匹配（§26.2「按 bundle 匹配」）：Extension 同归属。"""
    from app.providers.devicectl import match_app_processes, parse_apps_json, parse_processes_json

    apps = parse_apps_json(_fixture_text("apps-raw.json"))
    processes = parse_processes_json(_fixture_text("processes-raw.json"))
    matched = match_app_processes(apps, processes)
    # AgentTool：主进程在场（isMain）
    agent = matched["com.perfeval.agenttool"]
    assert agent == [{"pid": 15334, "name": "AgentTool", "isMain": True}]
    # Runner.app：仅通知 Extension 存活（isMain=False，真实机形态）
    runner = matched["com.perfeval.demo"]
    assert runner == [{
        "pid": 15377,
        "name": "RunnerExt-NotificationService",
        "isMain": False,
    }]


def test_resolve_main_process_rejects_extension_only() -> None:
    """仅 Extension 存活 → None（Extension 不作 xctrace attach 目标）。"""
    from app.providers.devicectl import parse_apps_json, parse_processes_json, resolve_main_process

    apps = parse_apps_json(_fixture_text("apps-raw.json"))
    processes = parse_processes_json(_fixture_text("processes-raw.json"))
    # com.perfeval.demo 仅 RunnerExt-NotificationService 存活 → None
    assert resolve_main_process(apps, processes, "com.perfeval.demo") is None
    # 未安装 bundle → None
    assert resolve_main_process(apps, processes, "com.example.nope") is None
    # 解析失败（None 输入）→ None
    assert resolve_main_process(None, processes, "com.perfeval.agenttool") is None


def test_parse_launch_json_golden_fixture() -> None:
    from app.providers.devicectl import parse_launch_json

    assert parse_launch_json(_fixture_text("launch-raw.json")) == 15436
    assert parse_launch_json("{}") is None
    assert parse_launch_json("garbage") is None


def test_parse_lock_state_golden_fixture() -> None:
    from app.providers.devicectl import parse_lock_state_json

    state = parse_lock_state_json(_fixture_text("lockstate.json"))
    assert state == {
        "deviceIdentifier": REAL_IDENTIFIER,
        "passcodeRequired": True,
        "unlockedSinceBoot": True,
    }


def test_parse_details_json_golden_fixture() -> None:
    from app.providers.devicectl import parse_details_json

    details = parse_details_json(_fixture_text("details-raw.json"))
    assert details == {
        "udid": REAL_UDID,
        "marketingName": "iPhone 15 Pro Max",
        "osVersionNumber": "27.2.1",
        "serialNumber": "TESTIOS001",
        "cpuCores": 6,
        "reality": "physical",
    }


# ---- RealIOSProvider（假 runner 驱动生产 provider）----


def _fake_provider(
    *,
    devices_text: str | None = None,
    apps_text: str | None = None,
    processes_text: str | None = None,
    launch_text: str = "",
    fail_lockstate: bool = False,
    journal: list[list[str]] | None = None,
) -> Any:
    from app.providers.realios import RealIOSProvider

    def runner(command: list[str], timeout: float) -> Any:
        args = command[2:]  # 剥掉 ["xcrun", "devicectl"]
        if journal is not None:
            journal.append(args)
        # run_devicectl_json 把 --json-output <tmp> 追加在尾部——剥掉取纯参数
        if "--json-output" in args:
            args = args[: args.index("--json-output")]
        if args == ["list", "devices"]:
            return _completed(devices_text if devices_text is not None else "")
        if args[:2] == ["device", "info"]:
            if args[2] == "apps":
                return _completed(apps_text or "")
            if args[2] == "processes":
                return _completed(processes_text or "")
            if args[2] == "lockState":
                if fail_lockstate:
                    return _completed("", returncode=1, stderr="device locked")
                return _completed(_fixture_text("lockstate.json"))
            if args[2] == "details":
                return _completed(_fixture_text("details-raw.json"))
        if args[:2] == ["device", "process"] and args[2] == "launch":
            return _completed(launch_text)
        return _completed("")

    return RealIOSProvider(devicectl_runner=runner)


def test_provider_list_devices_profile_explicit_simulator_false() -> None:
    """§26.1/§20.2：真机行 profile.simulator 恒显式 False（漏传会按 ios 推导 True）。"""
    provider = _fake_provider(devices_text=_fixture_text("devices-sample.json"))
    devices = provider.list_devices()
    wifi_row, usb_row = devices
    assert wifi_row["profile"] == {
        "usb": False,
        "wifi": True,
        "simulator": False,
        "gpuSource": None,
        "cpuCores": None,
        "refreshRateHz": None,
    }
    assert usb_row["profile"]["usb"] is True
    assert usb_row["profile"]["wifi"] is False
    assert usb_row["profile"]["simulator"] is False
    # §20.3 判定输入联动：双指标（modes=usb|wifi）在该设备可勾。判定输入用
    # 内部 profile 形状（含 platform 判定字段；wire_profile 剥离内部字段是设备
    # 行序列化口径，不作判定输入——capability_matrix 契约）
    from app.services.capability_matrix import metric_available_on_device

    available, reason = metric_available_on_device(
        "cpu.proc.xctrace.pct", "real", provider.get_profile(REAL_UDID)
    )
    assert available is True and reason is None


def test_provider_get_device_and_profile_fallback() -> None:
    provider = _fake_provider(devices_text=_fixture_text("devices-sample.json"))
    device = provider.get_device(REAL_UDID)
    assert device is not None and device["connection"] == "wifi"
    assert provider.get_device("NOPE") is None
    # 不在网设备（get_profile 无行）按双模式可能给 usb|wifi=True
    profile = provider.get_profile("NOPE")
    assert profile["usb"] is True and profile["wifi"] is True
    assert profile["simulator"] is False and profile["platform"] == "ios"


def test_provider_devices_unavailable_xcrun() -> None:
    """xcrun 本体缺失 → 503 xctrace_unavailable（details.reason=devicectl_missing）。"""
    from app.errors import ApiError
    from app.providers import xctrace

    def failing_runner(command: list[str], timeout: float) -> Any:
        raise FileNotFoundError("xcrun")

    from app.providers.realios import RealIOSProvider

    provider = RealIOSProvider(devicectl_runner=failing_runner)
    with pytest.raises(ApiError) as excinfo:
        provider.list_devices()
    assert excinfo.value.code == xctrace.XCTRACE_UNAVAILABLE
    assert excinfo.value.status == 503
    assert excinfo.value.details["reason"] == "devicectl_missing"


def test_provider_list_apps_composition_golden() -> None:
    """§26.2 合成：装机清单 ∩ 运行进程；isForeground 恒 null。"""
    provider = _fake_provider(
        devices_text=_fixture_text("devices-sample.json"),
        apps_text=_fixture_text("apps-raw.json"),
        processes_text=_fixture_text("processes-raw.json"),
    )
    body = provider.list_apps(REAL_UDID)
    assert body is not None and body["deviceId"] == REAL_UDID
    by_id = {app["packageName"]: app for app in body["apps"]}
    assert len(by_id) == 3
    agent = by_id["com.perfeval.agenttool"]
    assert agent["pid"] == 15334 and agent["running"] is True
    assert agent["processes"] == [{"pid": 15334, "name": "AgentTool", "isMain": True}]
    # 仅 Extension 存活：running=False/pid=None，processes 照实出行（isMain=false）
    runner = by_id["com.perfeval.demo"]
    assert runner["running"] is False and runner["pid"] is None
    assert runner["processes"] == [{
        "pid": 15377, "name": "RunnerExt-NotificationService", "isMain": False,
    }]
    # 未运行：三字段全空（§26.2 golden「未运行 → running=false、pid=null、processes=[]」）
    game = by_id["com.perfeval.gamebox"]
    assert game["running"] is False
    assert game["pid"] is None and game["processes"] == []
    # 真机通道前台恒 null（§26.2 不猜）
    assert all(app["isForeground"] is None for app in body["apps"])


def test_provider_list_apps_fail_is_404_shape() -> None:
    """apps 命令失败 → None（调用方 404 语义，SimulatorProvider 同哲学）。"""
    provider = _fake_provider(devices_text=_fixture_text("devices-sample.json"))
    assert provider.list_apps(REAL_UDID) is None


def test_provider_launch_app_parses_real_pid() -> None:
    provider = _fake_provider(
        devices_text=_fixture_text("devices-sample.json"),
        launch_text=_fixture_text("launch-raw.json"),
    )
    result = provider.launch_app(REAL_UDID, "com.perfeval.demo")
    assert result == {"component": "com.perfeval.demo", "error": None, "pid": 15436}
    failed = _fake_provider(
        devices_text=_fixture_text("devices-sample.json"), launch_text="oops"
    )
    result = failed.launch_app(REAL_UDID, "com.perfeval.demo")
    assert result["component"] is None and result["error"]


def test_provider_boot_device_error_body_matches_contract_golden() -> None:
    """§26.1 sim-boot golden 错误体：validation_error + deviceId/connection。"""
    from app.errors import ApiError

    provider = _fake_provider(devices_text=_fixture_text("devices-sample.json"))
    with pytest.raises(ApiError) as excinfo:
        provider.boot_device(REAL_UDID)
    body = excinfo.value.to_body()["error"]
    assert body["code"] == "validation_error"
    assert body["message"] == (
        "sim-boot 仅对 simulator 连接设备有意义"
        "（usb|wifi 连接的 Android/iOS 真机行无 boot 语义，契约 §23.1/§26.1）"
    )
    assert body["details"] == {"deviceId": REAL_UDID, "connection": "wifi"}


def test_provider_device_details_reads_real_values() -> None:
    provider = _fake_provider(devices_text=_fixture_text("devices-sample.json"))
    details = provider.device_details(REAL_UDID)
    assert details is not None
    assert details["udid"] == REAL_UDID
    assert details["cpuCores"] == 6


def test_provider_metric_ids_include_xctrace_pair() -> None:
    """§26.3：real 聚合面 metricId 全集含双指标（注册表行 S2 已落地）。"""
    from app.registry import is_registered

    provider = _fake_provider()
    ids = provider.list_metric_ids()
    assert "cpu.proc.xctrace.pct" in ids
    assert "memory.footprint.mb" in ids
    assert is_registered("cpu.proc.xctrace.pct")
    assert is_registered("memory.footprint.mb")


# ---- 审读修订回归（2026-11-16）：strict 探测（重试 + 失败语义分离）----


def test_provider_find_device_strict_retries_transient_then_succeeds() -> None:
    """审读#3：list devices 瞬时失败（空输出）→ 短重试；成功后返回权威读数。"""
    from app.providers.realios import RealIOSProvider

    calls = {"n": 0}
    sleeps: list[float] = []

    def runner(command: list[str], timeout: float) -> Any:
        calls["n"] += 1
        if calls["n"] <= 2:
            return _completed("")  # 锁屏瞬时失败形态（空输出=命令失败）
        return _completed(_fixture_text("devices-sample.json"))

    provider = RealIOSProvider(devicectl_runner=runner)
    device = provider.find_device_strict(REAL_UDID, sleep=sleeps.append)
    assert device is not None
    assert device["id"] == REAL_UDID and device["state"] == "offline"
    assert calls["n"] == 3  # 失败 2 次 + 成功 1 次
    assert sleeps == [0.5, 0.5]  # 缺省重试间隔


def test_provider_find_device_strict_exhausted_raises_probe_failed() -> None:
    """审读#3：重试耗尽 → ApiError(xctrace_unavailable, devicectl_probe_failed)——
    调用方按无数据语义处置，不得消费成权威离线。"""
    from app.errors import ApiError
    from app.providers import xctrace
    from app.providers.realios import RealIOSProvider

    calls = {"n": 0}

    def runner(command: list[str], timeout: float) -> Any:
        calls["n"] += 1
        return _completed("")

    provider = RealIOSProvider(devicectl_runner=runner)
    with pytest.raises(ApiError) as excinfo:
        provider.find_device_strict(REAL_UDID, sleep=lambda _s: None)
    body = excinfo.value.to_body()["error"]
    assert body["code"] == xctrace.XCTRACE_UNAVAILABLE
    assert excinfo.value.status == 503
    assert body["details"]["reason"] == "devicectl_probe_failed"
    assert body["details"]["probe"] == "list-devices"
    assert body["details"]["attempts"] == 3
    assert calls["n"] == 3


def test_provider_find_device_strict_absent_is_authoritative_offline() -> None:
    """审读#3：命令成功但设备不在表 → None（权威负读数，非探测失败）。"""
    from app.providers.realios import RealIOSProvider

    provider = _fake_provider(devices_text=_fixture_text("devices-sample.json"))
    assert provider.find_device_strict("NOPE-UDID", sleep=lambda _s: None) is None


def test_provider_resolve_process_strict_absent_is_authoritative_none() -> None:
    """审读#3：apps/processes 探测成功但目标未安装/未运行 → None（权威负读数，
    调用方走 app.process.not_found + null 点）。"""
    from app.providers.realios import RealIOSProvider

    provider = _fake_provider(
        devices_text=_fixture_text("devices-sample.json"),
        apps_text=_fixture_text("apps-raw.json"),
        processes_text=_fixture_text("processes-raw.json"),
    )
    assert (
        provider.resolve_process_strict(REAL_UDID, "com.example.nope") is None
    )


def test_provider_resolve_process_strict_transient_failure_raises() -> None:
    """审读#3：processes 命令瞬时失败重试耗尽 → raise（probe=info-processes）——
    不得返回 None 被消费成 not_found（旧缺陷：探测失败伪造 not_found + null 点）。"""
    from app.errors import ApiError
    from app.providers.realios import RealIOSProvider

    processes_calls = {"n": 0}

    def runner(command: list[str], timeout: float) -> Any:
        args = command[2:]
        if "--json-output" in args:
            args = args[: args.index("--json-output")]
        if args == ["list", "devices"]:
            return _completed(_fixture_text("devices-sample.json"))
        if args[:3] == ["device", "info", "apps"]:
            return _completed(_fixture_text("apps-raw.json"))
        if args[:3] == ["device", "info", "processes"]:
            processes_calls["n"] += 1
            return _completed("")  # 恒瞬时失败
        return _completed("")

    provider = RealIOSProvider(devicectl_runner=runner)
    with pytest.raises(ApiError) as excinfo:
        provider.resolve_process_strict(REAL_UDID, "com.perfeval.demo", sleep=lambda _s: None)
    assert excinfo.value.details["reason"] == "devicectl_probe_failed"
    assert excinfo.value.details["probe"] == "info-processes"
    assert processes_calls["n"] == 3


def test_provider_list_apps_processes_failure_is_404_semantics() -> None:
    """审读#3：processes 命令失败 → list_apps 返回 None（404 语义）——不得按
    空进程表消费（旧缺陷：全部 app 伪造成 running=false 负读数）。"""
    from app.providers.realios import RealIOSProvider

    provider = _fake_provider(
        devices_text=_fixture_text("devices-sample.json"),
        apps_text=_fixture_text("apps-raw.json"),
        # processes_text 缺省 "" → processes 命令失败
    )
    assert provider.list_apps(REAL_UDID) is None


# ---- 审读#2 回归：xcrun 家族共锁（devicectl 执行面与窗口锁同一把 RLock）----


def test_xcrun_family_lock_shared_between_devicectl_and_window() -> None:
    """devicectl 执行面在 XCRUN_FAMILY_LOCK 内：采集窗持锁期间 devicectl 探测
    排队（并发 xcrun 命令会失败，勘察实测——串行化覆盖窗内/窗外全部 xcrun 面）。"""
    import threading

    from app.providers import devicectl, xctrace

    assert xctrace._XCRUN_DEVICE_LOCK is devicectl.XCRUN_FAMILY_LOCK
    assert isinstance(devicectl.XCRUN_FAMILY_LOCK, type(threading.RLock()))

    invocations: list[list[str]] = []

    def fake_runner(command: list[str], timeout: float) -> Any:
        invocations.append(command)
        return _completed("")

    with devicectl.XCRUN_FAMILY_LOCK:  # 模拟采集窗全程持锁
        worker = threading.Thread(
            target=lambda: devicectl.run_devicectl(
                "list", "devices", timeout=5.0, runner=fake_runner
            )
        )
        worker.start()
        worker.join(0.2)
        assert invocations == []  # 持锁期间探测命令被挡在锁外（未执行）
    worker.join(2.0)
    assert not worker.is_alive()
    assert len(invocations) == 1  # 锁释放后探测照常执行


def test_xcrun_family_lock_reentrant_within_window_thread() -> None:
    """RLock 重入：窗口链路持锁内嵌套 devicectl 调用（prewarm→lockState）同线程
    不死锁——run_sample_window 现有窗口序列测试即走此路径，此处钉锁语义本体。"""
    from app.providers import devicectl

    invocations: list[list[str]] = []
    with devicectl.XCRUN_FAMILY_LOCK:  # 模拟窗口外层持锁
        devicectl.run_devicectl(
            "device", "info", "lockState", "--device", "D",
            timeout=5.0, runner=lambda c, t: (invocations.append(c), _completed(""))[1],
        )
    assert len(invocations) == 1  # 同线程重入成功（Lock 会死锁）
