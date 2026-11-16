"""simulator provider 单测（M-iOS 契约 §23）：fixture 驱动，不依赖模拟器常驻。

解析层 golden = tests/fixtures/simulator/ 下的真机（iOS 27.0 模拟器）原文：
- ``listapps-ios27-raw.txt``：``simctl listapps`` 全量输出（NeXT 风格 plist）；
- ``launchctl-uikit.txt``：``launchctl list`` 的 UIKitApplication 行；
- ``springboard-visibility-ndjson.txt``：SpringBoard visibility ndjson 行。

设备枚举/应用列表/前台探测以假 simctl runner 驱动**生产** SimulatorProvider
（命令执行面注入，与 m4_fixtures 的 command_runner 接缝同哲学）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "simulator"
SIMULATOR_UDID = "88B9E268-8B16-46BA-9E47-E32495BCE547"

DEVICES_JSON = f"""{{
  "devices": {{
    "com.apple.CoreSimulator.SimRuntime.iOS-27-0": [
      {{"dataPath": "/d/{SIMULATOR_UDID}", "udid": "{SIMULATOR_UDID}",
        "isAvailable": true, "state": "Booted", "name": "iPhone 18 Pro",
        "deviceTypeIdentifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-18-Pro"}},
      {{"udid": "C014107D-FE53-486A-B79E-3398074649B5", "isAvailable": true,
        "state": "Shutdown", "name": "iPhone 18 Pro Max",
        "deviceTypeIdentifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-18-Pro-Max"}}
    ],
    "com.apple.CoreSimulator.SimRuntime.watchOS-27-0": [
      {{"udid": "2F443EBB-227F-46D2-AD75-45CFF7F61CB7", "isAvailable": true,
        "state": "Shutdown", "name": "Apple Watch Series 12 (46mm)"}}
    ]
  }}
}}"""


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> Any:
    result = type("_R", (), {})()
    result.stdout = stdout
    result.returncode = returncode
    result.stderr = stderr
    return result


# ---- 解析层 golden（真机原文 fixture）----


def test_parse_listapps_golden_fixture() -> None:
    from app.providers.simctl import parse_listapps

    apps = parse_listapps((FIXTURES / "listapps-ios27-raw.txt").read_text())
    assert len(apps) == 39  # 全量应用解析（iOS 27.0 模拟器出厂 39 应用）
    prefs = next(a for a in apps if a["packageName"] == "com.apple.Preferences")
    assert prefs == {"packageName": "com.apple.Preferences", "name": "Settings",
                     "appType": "System"}
    # 全部行可解析（bundle 名不含空值/文件 URL 残留）
    assert all(a["packageName"] and "file://" not in a["packageName"] for a in apps)


def test_parse_launchctl_uikit_golden_fixture() -> None:
    from app.providers.simctl import parse_launchctl_uikit

    table = parse_launchctl_uikit((FIXTURES / "launchctl-uikit.txt").read_text())
    assert table["com.apple.Preferences"] == 81296
    assert table["com.apple.mobilecal"] == 81014
    assert len(table) == 5  # 5 条 UIKitApplication 行


def test_parse_activation_and_foreground() -> None:
    from app.providers.simctl import (
        foreground_from_activation,
        parse_activation_bundles,
    )

    bundles = parse_activation_bundles(_ACTIVATION_SAMPLE)
    assert bundles == ["com.apple.Preferences", "com.apple.mobilecal"]
    # 最后一次激活 = 当前前台（iOS 单前台，实测截图比对一致）
    assert foreground_from_activation(bundles) == "com.apple.mobilecal"
    # 空窗口/count 行 → None（不猜）
    assert parse_activation_bundles('{"count":0,"finished":1}\n') == []
    assert foreground_from_activation([]) is None
    # 非 activation 行跳过（log 自身回显行等）
    assert parse_activation_bundles(
        '{"eventMessage": "log run noninteractively, parent: 80456"}\n'
    ) == []


def test_parse_activation_golden_fixture() -> None:
    """真机原文 fixture（iOS 27.0 模拟器 activation ndjson）：Activation request
    行解析 + log 自身回显行跳过。"""
    from app.providers.simctl import (
        foreground_from_activation,
        parse_activation_bundles,
    )

    bundles = parse_activation_bundles(
        (FIXTURES / "activation-ndjson.txt").read_text()
    )
    assert bundles, "activation fixture 必须解析出激活序列"
    assert all(bundle and " " not in bundle for bundle in bundles)
    # 终态 = 序列末位（fixture 末次激活为 Preferences，实测截图比对）
    assert foreground_from_activation(bundles) == bundles[-1]


def test_parse_visibility_and_foreground_golden_fixture() -> None:
    """visibility 解析（已否决为前台判据，保留诊断用途）以真机原文 fixture 对拍。"""
    from app.providers.simctl import (
        foreground_from_visibility,
        parse_visibility_events,
    )

    events = parse_visibility_events(
        (FIXTURES / "springboard-visibility-ndjson.txt").read_text()
    )
    # 真机原文：Preferences Foreground（后）→ mobilecal Background（更后）
    assert events == [
        {"bundle": "com.apple.Preferences", "pid": 81296, "state": "Foreground"},
        {"bundle": "com.apple.mobilecal", "pid": 81014, "state": "Background"},
    ]
    # 栈式判定：mobilecal Background ≠ 栈顶 → 不弹栈 → Preferences
    assert foreground_from_visibility(events) == "com.apple.Preferences"


def test_visibility_rules_rejected_but_parser_kept() -> None:
    """visibility 栈式判定的已实测否决依据存档：iOS 27.0 视觉前台但事件
    Background（权限弹窗触发）——生产前台判定走 activation（§23.1）。"""
    from app.providers.simctl import foreground_from_visibility

    # 只有 Background 信号 → None
    assert foreground_from_visibility(
        [{"bundle": "a", "pid": 1, "state": "Background"}]
    ) is None
    # App 自身 Fore→Back → None
    assert foreground_from_visibility([
        {"bundle": "a", "pid": 1, "state": "Foreground"},
        {"bundle": "a", "pid": 1, "state": "Background"},
    ]) is None


def test_parse_visibility_pid_negative_intent() -> None:
    """launch 意图事件（pid=-1，iOS 27.0 实测）可解析：pid 落 None、bundle 保留。"""
    from app.providers.simctl import parse_visibility_events

    events = parse_visibility_events(
        '{"eventMessage": "[app<com.apple.Preferences>:-1] Setting process visibility'
        ' to: Foreground"}\n'
        '{"eventMessage": "[app<com.apple.Preferences>:91518] Setting process visibility'
        ' to: Background"}\n'
    )
    assert events == [
        {"bundle": "com.apple.Preferences", "pid": None, "state": "Foreground"},
        {"bundle": "com.apple.Preferences", "pid": 91518, "state": "Background"},
    ]


def test_parse_ps_stat() -> None:
    from app.providers.simctl import parse_ps_stat

    assert parse_ps_stat(
        "  PID  %CPU    RSS COMM\n81296   0.0 464112 /path/Preferences"
    ) == (0.0, 453.2)  # KB → MB
    assert parse_ps_stat("  PID  %CPU    RSS COMM") is None  # 无数据行
    assert parse_ps_stat("") is None


# ---- 设备枚举（假 runner 驱动生产 provider）----


def test_provider_list_devices_filters_ios_runtime() -> None:
    from app.providers.simulator import SimulatorProvider

    provider = SimulatorProvider(host_cores=14, simctl_runner=lambda cmd, t: _completed(DEVICES_JSON))
    devices = provider.list_devices()
    # watchOS 行过滤；Bouted→online、Shutdown→offline（Shutdown 照常出行）
    assert [d["state"] for d in devices] == ["online", "offline"]
    assert [d["platform"] for d in devices] == ["ios", "ios"]
    assert [d["connection"] for d in devices] == ["simulator", "simulator"]
    booted = devices[0]
    assert booted["id"] == booted["serial"] == SIMULATOR_UDID
    assert booted["model"] == "iPhone 18 Pro"
    # info/profile 宿主口径声明（契约 §23.1）
    assert booted["info"]["os"] == "iOS 27.0 (Simulator)"
    assert booted["info"]["cpu"] == "14 cores (host)"
    assert booted["info"]["resolution"] is None
    assert booted["info"]["batteryLevel"] is None
    assert booted["profile"] == {
        "usb": False, "wifi": False, "simulator": True, "gpuSource": None,
        "cpuCores": 14, "refreshRateHz": None,
    }


def test_provider_devices_unavailable_xcrun() -> None:
    from app.errors import ApiError
    from app.providers import simctl
    from app.providers.simulator import SimulatorProvider

    def failing_runner(cmd: list[str], t: Any) -> Any:
        raise FileNotFoundError("xcrun")

    provider = SimulatorProvider(simctl_runner=failing_runner)
    try:
        provider.list_devices()
    except ApiError as exc:
        assert exc.code == simctl.SIMCTL_UNAVAILABLE
        assert exc.status == 503
    else:  # pragma: no cover
        raise AssertionError("必须抛 simctl_unavailable")


# ---- 应用列表/前台/launch（假 runner 组合驱动）----


def _fake_provider(
    *,
    devices_json: str = DEVICES_JSON,
    listapps_text: str | None = None,
    launchctl_text: str = "",
    activation_text: str = "",
    launch_stdout: str = "",
) -> Any:
    from app.providers.simulator import SimulatorProvider

    def runner(command: list[str], timeout: float) -> Any:
        args = command[2:]  # 剥掉 ["xcrun", "simctl"]
        if args[:3] == ["list", "devices", "--json"]:
            return _completed(devices_json)
        if args[:1] == ["listapps"]:
            return _completed(listapps_text or "")
        if args[:3] == ["spawn", SIMULATOR_UDID, "launchctl"]:
            return _completed(launchctl_text)
        if args[:3] == ["spawn", SIMULATOR_UDID, "log"]:
            return _completed(activation_text)
        if args[:1] == ["launch"]:
            return _completed(launch_stdout)
        if args[:1] == ["bootstatus"]:
            return _completed("Device already booted\n")
        return _completed("")

    return SimulatorProvider(host_cores=8, simctl_runner=runner)


_LISTAPPS_SAMPLE = """{
    "com.apple.Preferences" =     {
        ApplicationType = System;
        CFBundleDisplayName = Settings;
        CFBundleIdentifier = "com.apple.Preferences";
    };
    "com.apple.mobilecal" =     {
        ApplicationType = System;
        CFBundleDisplayName = Calendar;
        CFBundleIdentifier = "com.apple.mobilecal";
    };
}
"""

_LAUNCHCTL_SAMPLE = (
    "80736\t0\tUIKitApplication:com.apple.Spotlight[6645][rb-legacy]\n"
    "81296\t0\tUIKitApplication:com.apple.Preferences[70d3][rb-legacy]\n"
    "81014\t0\tUIKitApplication:com.apple.mobilecal[55d0][rb-legacy]\n"
    "-\t0\tcom.apple.progressd\n"
)

_ACTIVATION_SAMPLE = (
    '{"eventMessage": "[FBSystemService][0x67da] Activation request for'
    ' com.apple.Preferences contained LSCacheGUID 06A51634", "timestamp":'
    ' "2026-11-03 23:48:19.667378+0800"}\n'
    '{"eventMessage": "[FBSystemService][0x67da] Activation request for'
    ' com.apple.mobilecal will wait 10.00s", "timestamp":'
    ' "2026-11-03 23:49:00.000000+0800"}\n'
)


def test_provider_list_apps_running_and_foreground() -> None:
    provider = _fake_provider(
        listapps_text=_LISTAPPS_SAMPLE,
        launchctl_text=_LAUNCHCTL_SAMPLE,
        activation_text=_ACTIVATION_SAMPLE,
    )
    body = provider.list_apps(SIMULATOR_UDID)
    assert body is not None and body["deviceId"] == SIMULATOR_UDID
    by_id = {app["packageName"]: app for app in body["apps"]}
    assert set(by_id) == {"com.apple.Preferences", "com.apple.mobilecal"}
    prefs = by_id["com.apple.Preferences"]
    assert prefs["pid"] == 81296 and prefs["running"] is True
    # 最后一次激活 = mobilecal（activation 序列末位）→ 单前台
    assert prefs["isForeground"] is False
    assert by_id["com.apple.mobilecal"]["isForeground"] is True
    assert prefs["processes"] == [
        {"pid": 81296, "name": "com.apple.Preferences", "isMain": True}
    ]
    # 未运行应用：pid None / running False / processes 空（不伪造）
    assert by_id["com.apple.mobilecal"]["pid"] == 81014  # 也在跑
    # listapps 失败（空输出）→ None（调用方 404 语义）
    broken = _fake_provider(listapps_text="")
    assert broken.list_apps(SIMULATOR_UDID) is None


def test_provider_list_apps_foreground_unknown_is_null() -> None:
    provider = _fake_provider(
        listapps_text=_LISTAPPS_SAMPLE,
        launchctl_text=_LAUNCHCTL_SAMPLE,
        activation_text="",  # 窗口内无 activation 信号
    )
    body = provider.list_apps(SIMULATOR_UDID)
    assert all(app["isForeground"] is None for app in body["apps"])  # 不猜


def test_provider_launch_app_parses_pid() -> None:
    provider = _fake_provider(launch_stdout="com.apple.Preferences: 81296\n")
    result = provider.launch_app(SIMULATOR_UDID, "com.apple.Preferences")
    assert result == {"component": "com.apple.Preferences", "error": None, "pid": 81296}
    failed = _fake_provider(launch_stdout="An error was encountered\n")
    result = failed.launch_app(SIMULATOR_UDID, "com.apple.NoSuchApp")
    assert result["component"] is None and result["error"]


def test_provider_boot_device_idempotent() -> None:
    provider = _fake_provider()
    body = provider.boot_device(SIMULATOR_UDID)
    assert body == {"deviceId": SIMULATOR_UDID, "action": "boot", "state": "online"}
    # 未知设备 → 404
    try:
        provider.boot_device("NOPE")
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "code", None) == "device_not_found"
    else:  # pragma: no cover
        raise AssertionError("必须抛 device_not_found")


def test_provider_boot_device_rejects_bootstatus_failure() -> None:
    """A non-zero bootstatus must not be treated as successful empty output."""
    from app.providers import simctl
    from app.providers.simulator import SimulatorProvider

    shutdown_devices = DEVICES_JSON.replace('"state": "Booted"', '"state": "Shutdown"')

    def runner(command: list[str], timeout: float) -> Any:
        args = command[2:]
        if args[:3] == ["list", "devices", "--json"]:
            return _completed(shutdown_devices)
        if args[:1] == ["bootstatus"]:
            return _completed("boot failed\n", returncode=1)
        return _completed("")

    provider = SimulatorProvider(host_cores=8, simctl_runner=runner)
    try:
        provider.boot_device(SIMULATOR_UDID)
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "code", None) == simctl.SIMCTL_UNAVAILABLE
    else:  # pragma: no cover
        raise AssertionError("bootstatus 失败不得返回成功")


def test_provider_boot_device_is_idempotent_when_bootstatus_fails() -> None:
    """An already booted simulator remains successful if bootstatus is transiently unavailable."""
    from app.providers.simulator import SimulatorProvider

    def runner(command: list[str], timeout: float) -> Any:
        args = command[2:]
        if args[:3] == ["list", "devices", "--json"]:
            return _completed(DEVICES_JSON)
        if args[:1] == ["bootstatus"]:
            return _completed("boot failed\n", returncode=1)
        return _completed("")

    provider = SimulatorProvider(host_cores=8, simctl_runner=runner)
    assert provider.boot_device(SIMULATOR_UDID) == {
        "deviceId": SIMULATOR_UDID,
        "action": "boot",
        "state": "online",
    }


def test_provider_metric_ids_are_ios_only() -> None:
    provider = _fake_provider()
    assert provider.list_metric_ids() == ["cpu.proc.host.pct", "memory.rss.mb"]
