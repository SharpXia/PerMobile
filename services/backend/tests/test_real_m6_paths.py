"""M6 real 路径 fixture 测试：profile 探测（kgsl/meminfo 明细段）、real 会话的
fpsWindow 诊断（§21.5）、real log 流（§21.7 logcat -v time 级别过滤）。

如实标注：真机 adb I/O 无设备不可测——本文件以 FakeAdb 应答表 / monkeypatch 行源
驱动**生产代码**（RealProvider 探测、SessionManager 诊断与日志流接线）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from m4_fixtures import make_real_client, make_recording_session  # noqa: E402
from m6_fixtures import (  # noqa: E402
    MEMINFO_DETAIL_FIXTURE,
    MEMINFO_DETAIL_PARTIAL_FIXTURE,
    FakeAdb,
    default_probe_responses,
)


# ---- R21：real profile 探测（机型特性 / gpu 驱动节点）----


def _profile_of(kgsl: bool, meminfo_fixture: str | None) -> dict:
    """构造 RealProvider + FakeAdb 应答表，读 R3CN80ABCDEF 的 profile。

    meminfo 探测先同步预热（实机验证修复后 get_profile 走缓存或后台线程，
    同步预热保证断言确定性）。
    """
    from app.providers.real import RealProvider

    responses = default_probe_responses(kgsl=kgsl, meminfo_detail=meminfo_fixture or "")
    adb = FakeAdb(responses)
    provider = RealProvider(adb=adb)
    provider._probe_meminfo_detail("R3CN80ABCDEF")
    return provider.get_profile("R3CN80ABCDEF")


def test_real_profile_kgsl_present() -> None:
    profile = _profile_of(kgsl=True, meminfo_fixture=MEMINFO_DETAIL_FIXTURE)
    assert profile["gpuSource"] == "kgsl"
    assert profile["meminfoDetail"] is True
    assert profile["cpuCores"] == 8
    assert profile["refreshRateHz"] == 120
    assert profile["usb"] is True and profile["wifi"] is False


def test_real_profile_kgsl_absent() -> None:
    profile = _profile_of(kgsl=False, meminfo_fixture=MEMINFO_DETAIL_FIXTURE)
    assert profile["gpuSource"] is None  # 无 kgsl → gpu.busy.pct 判 kgsl_missing


def test_real_profile_meminfo_detail_partial_graphics_passes() -> None:
    """§21.3 门禁修订（2026-11-16）：明细段缺图形行（无 GL/Graphics）但
    native/dalvik 可解析 → meminfoDetail=True（通道存在，整组可勾；旧判据误禁）。"""
    profile = _profile_of(kgsl=True, meminfo_fixture=MEMINFO_DETAIL_PARTIAL_FIXTURE)
    assert profile["meminfoDetail"] is True


def test_real_profile_power_supply_blocked() -> None:
    """实机验证新增（2026-11-03）：power_supply sysfs 被屏蔽 → powerSupply=False。

    SM_F9560 实测 `/sys/class/power_supply/` Permission denied —— power.* 三指标
    无数据源，判 reason=power_sysfs_blocked（契约 §20.1 枚举 §11.1 扩展），
    不静默出空图。
    """
    from app.providers.real import RealProvider
    from app.services.capability_matrix import judge_metric
    from app.registry import get_metric

    responses = default_probe_responses(kgsl=True, meminfo_detail=MEMINFO_DETAIL_FIXTURE)
    # 默认应答表无 capacity 匹配 → cat 空输出 rc=0 → False（节点被屏蔽读数）
    adb = FakeAdb(responses)
    provider = RealProvider(adb=adb)
    provider._probe_meminfo_detail("R3CN80ABCDEF")
    profile = provider.get_profile("R3CN80ABCDEF")
    assert profile["powerSupply"] is False
    # wire 形状不含内部字段
    from app.services.capability_matrix import wire_profile
    assert "powerSupply" not in wire_profile(profile)

    for metric_id in ("power.current.ma", "power.level.pct", "power.energy.mah"):
        available, reason = judge_metric(get_metric(metric_id), "real", profile)
        assert available is False
        assert reason == "power_sysfs_blocked"
    # powerSupply=True → 不受此门禁
    profile_ok = dict(profile, powerSupply=True)
    available, reason = judge_metric(get_metric("power.current.ma"), "real", profile_ok)
    assert available is True and reason is None
    # 探测失败 None 同落 blocked（不猜）
    profile_unknown = dict(profile, powerSupply=None)
    available, reason = judge_metric(get_metric("power.current.ma"), "real", profile_unknown)
    assert available is False and reason == "power_sysfs_blocked"


def test_real_power_supply_probe_cached() -> None:
    """power_supply 探测按 serial 缓存（list_devices 高频调用不重复探测）。"""
    from app.providers.real import RealProvider

    responses = default_probe_responses() + [
        FakeAdb.when("cat /sys/class/power_supply/battery/capacity", "87\n"),
    ]
    provider = RealProvider(adb=FakeAdb(responses))
    assert provider._probe_power_supply("S1") is True
    assert provider._probe_power_supply("S1") is True
    cat_calls = [c for c in provider._adb.commands if "capacity" in " ".join(c)]
    assert len(cat_calls) == 1


def test_real_profile_probe_failure_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """探测命令抛错（adb 异常）→ meminfoDetail=None → 判定按缺失处理（不猜）。"""
    from app.providers.real import RealProvider

    class _BoomAdb:
        def available(self) -> bool:
            return True

        def command(self, *args: str, serial: str | None = None) -> list[str]:
            return ["adb", *args]

        def run(self, *args: str, serial: str | None = None, timeout: float | None = None):
            raise OSError("adb exploded")

    provider = RealProvider(adb=_BoomAdb())
    provider._probe_meminfo_detail("R3CN80ABCDEF")  # 同步预热（异常路径 → None 入缓存）
    profile = provider.get_profile("R3CN80ABCDEF")
    assert profile["gpuSource"] is None  # 探测失败置 null（契约 §20.2）
    assert profile["meminfoDetail"] is None
    assert profile["cpuCores"] is None
    assert profile["refreshRateHz"] is None


def test_real_profile_gpu_probe_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """kgsl 探测结果按 serial 缓存（list_devices 高频调用不重复探测）。"""
    from app.providers.real import RealProvider

    adb = FakeAdb(default_probe_responses(kgsl=True))
    provider = RealProvider(adb=adb)
    assert provider._probe_gpu_source("S1") == "kgsl"
    assert provider._probe_gpu_source("S1") == "kgsl"
    ls_calls = [
        c for c in adb.commands
        if c and "kgsl" in " ".join(str(part) for part in c)
    ]
    assert len(ls_calls) == 1


def test_real_gpu_probe_command_no_sh_c() -> None:
    """实机验证修复（2026-11-03）：kgsl 探测命令不得经 `sh -c`。

    原构造 `sh -c "ls … 2>/dev/null"` 经 adb 拼接引号丢失 → 设备侧 `sh -c ls` 无参
    执行列出根目录、恒非空 → 恒报 "kgsl"（模拟器实机踩中）。单元素直传后设备
    shell 自行解析重定向。

    真机回归更新（2026-11-11，小米 2405CPX3DC/Android 16）：探测命令从
    `ls kgsl-3d0`（目录存在性，该机 SELinux 拒目录列 → 恒空 → false-negative）
    升级为 `cat kgsl-3d0/gpubusy`（与 gpu.busy.pct 数据面 GPUBUSY_PATH 同源）。
    """
    from app.providers.real import RealProvider

    adb = FakeAdb(default_probe_responses(kgsl=False))
    provider = RealProvider(adb=adb)
    assert provider._probe_gpu_source("S1") is None  # 空输出（重定向后）→ null 不猜
    kgsl_calls = [
        c for c in adb.commands
        if c and "kgsl" in " ".join(str(part) for part in c)
    ]
    assert len(kgsl_calls) == 1
    cmd = kgsl_calls[0]
    assert "sh" not in cmd[: cmd.index("shell")], f"不得经 sh -c: {cmd}"
    shell_idx = cmd.index("shell")
    assert cmd[shell_idx] == "shell" and len(cmd) - shell_idx == 3  # shell + 脚本 + serial
    # 探测手段与数据面同源（GPU busy 差分的实际读点），不再用目录列存在性
    assert cmd[shell_idx + 1].startswith("cat /sys/class/kgsl/kgsl-3d0/gpubusy 2>/dev/null")


# ---- 实机 golden 回填（2026-11-03，SM_F9560 / Android 16 真机 dump）----


def test_real_display_info_from_real_sm9560_dump() -> None:
    """真机 `dumpsys display` 样本（tests/fixtures/real_sm9560_display.txt）。

    Android 16 OneUI 的 DisplayDeviceInfo 为复合行：`fps=` 嵌在 supportedModes
    数组内、行尾是 displayShape 复合键——`rsplit("=")` 解析失败（恒 null），
    正则 `fps=<float>` 取首个可解析值（supportedModes[0] = 默认模式 120）。
    """
    from pathlib import Path

    from app.providers.real import RealProvider

    real_display = (
        Path(__file__).resolve().parent
        / "fixtures"
        / "real_sm9560_display.txt"
    ).read_text(encoding="utf-8")
    # display 段只放真机原文（不带任何合成 fps 行），确保断言消费的是真实样本
    responses = [
        FakeAdb.when(
            "getprop",
            "[ro.product.model]: [SM_F9560]\n"
            "[ro.build.version.release]: [16]\n"
            "[ro.product.cpu.abi]: [arm64-v8a]\n",
        ),
        FakeAdb.when(
            "__S__",
            "8\n"
            "__S__\n"
            "Physical size: 1856x2160\n"
            "__B__\n"
            "level: 100\n"
            "__D__\n" + real_display,
        ),
        FakeAdb.when("cat /sys/class/kgsl/kgsl-3d0/gpubusy", "0 0\n"),
        FakeAdb.when("dumpsys meminfo system", MEMINFO_DETAIL_FIXTURE),
        FakeAdb.when("cat /sys/class/power_supply/battery/capacity", "100\n"),
    ]
    provider = RealProvider(adb=FakeAdb(responses))
    provider._probe_meminfo_detail("R3CN80ABCDEF")
    info = provider._device_info("R3CN80ABCDEF")
    assert info["refreshRateHz"] == 120  # supportedModes[0].fps=120.00001（真机原文）


# ---- 实机验证修复（2026-11-02）：设备信息批量化 + TTL / 应用列表单次 ps ----


def test_real_device_info_batched_and_cached() -> None:
    """_device_info 批量化：2 次 adb 往返出全量信息，10s TTL 内不重复探测。"""
    from app.providers.real import RealProvider

    adb = FakeAdb(default_probe_responses())
    provider = RealProvider(adb=adb)
    info = provider._device_info("S1")
    assert info["os"] == "Android 14"
    assert info["cpu"] == "8 cores"
    assert info["resolution"] == "1080x2400"
    assert info["batteryLevel"] == 87
    assert info["refreshRateHz"] == 120
    assert info["abi"] == "arm64-v8a"
    provider._device_info("S1")  # TTL 内 → 0 次新往返
    getprop_calls = [c for c in adb.commands if c and "getprop" in c]  # 记录形如 ["shell","getprop",serial]
    combined_calls = [
        c for c in adb.commands
        if c and "__S__" in " ".join(str(p) for p in c)
    ]
    assert len(getprop_calls) == 1
    assert len(combined_calls) == 1


def test_real_list_apps_single_ps_scan_and_subprocesses() -> None:
    """list_apps 单次 `ps -A` 建表（修复逐包 pidof 的超时根因）+ R20 子进程补全。"""
    from app.providers.real import RealProvider

    adb = FakeAdb(default_probe_responses())
    provider = RealProvider(adb=adb)
    body = provider.list_apps("R3CN80ABCDEF")
    assert body is not None
    apps = {a["packageName"]: a for a in body["apps"]}
    game = apps["com.example.game"]
    assert game["running"] is True
    assert game["pid"] == 1234
    assert game["isForeground"] is True  # topResumedActivity 直接解析包名
    names = [p["name"] for p in game["processes"]]
    assert names == ["com.example.game", "com.example.game:push"]
    assert [p["isMain"] for p in game["processes"]] == [True, False]
    joined = [" ".join(str(p) for p in c) for c in adb.commands]
    assert not any("pidof" in j for j in joined)  # 逐包 pidof 是超时根因，已移除
    assert sum("ps -A" in j for j in joined) == 1


def test_real_foreground_name_resumed_line_fallback() -> None:
    """实机验证修复（2026-11-03）：Android 16 真机无 topResumedActivity 行。

    只有 `Resumed: ActivityRecord{… u0 <pkg>/…}`（SM_F9560 实测格式）；解析顺序
    topResumedActivity 优先、Resumed: 兜底。
    """
    from app.providers.real import RealProvider

    # 仅 Resumed: 行（真机形态）
    text = (
        "  Resumed activities in task display areas (from top to bottom):\n"
        "    Resumed: ActivityRecord{171130277 u0 com.perfeval.gamebox/"
        "com.qiyukf.unicorn.ui.activity.ServiceMessageActivity t177}\n"
    )
    assert RealProvider._parse_foreground_name(text) == "com.perfeval.gamebox"
    # topResumedActivity 行优先（模拟器形态，既有 golden）
    text_top = "    topResumedActivity=ActivityRecord{50538932 u0 com.example.game/.Main t434}\n"
    assert RealProvider._parse_foreground_name(text_top) == "com.example.game"
    # 两行并存时 topResumedActivity 优先
    both = text_top + text
    assert RealProvider._parse_foreground_name(both) == "com.example.game"
    # 无前台行 → None 不猜
    assert RealProvider._parse_foreground_name("  Display #0:\n") is None


# ---- §21.5：real 模式 fpsWindow 接受+回显 + not_applied 诊断 ----


def _ring_events(client: TestClient, sid: str) -> list[dict]:
    """读会话 bus 环形缓冲（SSE 传输层等价；real 会话无采样流、SSE 阻塞读不可行）。

    环形缓冲即 SSE 端点的重放源（sse.py ring buffer），transport 语义由
    test_session_options_m6 的 synthetic SSE 流覆盖。
    """
    internal = client.app.state.sessions._sessions[sid]
    with internal.bus._lock:
        return [dict(env) for _seq, env in internal.bus._ring]


def test_real_fps_window_diagnostic_not_applied(tmp_path, monkeypatch) -> None:
    """real 模式窗口级帧率选择未落地：接受请求并回显，但广播 warn 诊断。"""
    from m4_fixtures import fake_screenrecord_runner

    runner = fake_screenrecord_runner([], {})
    with make_real_client(tmp_path, monkeypatch, recorder_command_runner=runner) as client:
        resp = client.post("/api/sessions", json={
            "deviceId": "fake-real-001",
            "packageName": "com.example.fakegame",
            "metrics": [],
            "interactive": False,
            "launchOnCreate": False,
            "fpsWindow": {"type": "surface_view"},
        })
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["fpsWindow"] == {"type": "surface_view"}  # 接受并回显
        sid = body["id"]
        diagnostics = [
            e for e in _ring_events(client, sid) if e["type"] == "diagnostic"
        ]
        assert any(
            d["code"] == "fps_window.not_applied" and d["severity"] == "warn"
            for d in diagnostics
        ), [e["type"] for e in diagnostics]
        client.post(f"/api/sessions/{sid}/discard")


# ---- §21.7：real log 流（logcat -v time 持续流 + level 过滤）----


def test_real_log_stream_filters_by_level(tmp_path, monkeypatch) -> None:
    from app.services import log_stream as log_stream_module

    fake_lines = iter([
        "11-01 10:00:00.000  1234  5678 I ActivityManager: Start proc for fakegame\n",
        "11-01 10:00:00.100  1234  5678 W ActivityManager: Background start not allowed\n",
        "11-01 10:00:00.200  1234  5678 E FakeGame: Failed to load texture\n",
        "11-01 10:00:00.300  1234  5678 D FakeGame: debug noise should be filtered\n",
        "    at com.example.FakeGame.loop(FakeGame.java:1)\n",  # 续行（无级别）
    ])

    def fake_source(serial: str, adb_path: str = "adb"):
        return lambda: fake_lines

    monkeypatch.setattr(log_stream_module, "adb_logcat_time_source", fake_source)

    runner = fake_screenrecord_runner_stub()
    with make_real_client(tmp_path, monkeypatch, recorder_command_runner=runner) as client:
        resp = client.post("/api/sessions", json={
            "deviceId": "fake-real-001",
            "packageName": "com.example.fakegame",
            "metrics": [],
            "interactive": False,
            "launchOnCreate": False,
            "logLevel": "warn",
        })
        assert resp.status_code == 201, resp.text
        sid = resp.json()["id"]
        # 行源为有限迭代器：通道线程消费完即退出；事件经环形缓冲断言（SSE 传输
        # 语义由 synthetic 流测试覆盖）
        events = _ring_events(client, sid)
        assert any(e["type"] == "log" for e in events), [e["type"] for e in events]
        logs = [e for e in events if e["type"] == "log"]
        levels = [(e["level"], e["source"], e["line"]) for e in logs]
        # warn 及以上放行；debug 与无级别续行被过滤
        assert ("warn", "ActivityManager", "Background start not allowed") in levels
        assert ("error", "FakeGame", "Failed to load texture") in levels
        assert all(l[0] in ("warn", "error") for l in levels)
        assert len(logs) == 2
        client.post(f"/api/sessions/{sid}/discard")


def fake_screenrecord_runner_stub():
    from m4_fixtures import fake_screenrecord_runner

    return fake_screenrecord_runner([], {})


# ---- 2026-11-04 体检修复回归（mem-2：探测目标假阴性补探）----


def test_meminfo_detail_probe_channel_partial_graphics_missing_passes_first_probe():
    """§21.3 门禁修订（2026-11-16）：system 明细段缺图形行（SM_F9560/模拟器实测
    形态，native/dalvik 可解析）→ 通道判据直接放行，**不**再走三方包补探——
    图形池行只在有 GL 上下文的进程出现，探测目标缺席不代表被测应用缺席
    （被测游戏四项齐全时 gfx/gl 照常出点，旧 detail_complete 判据整组假阴性）。"""
    from app.providers.real import RealProvider
    from m6_fixtures import MEMINFO_DETAIL_PARTIAL_FIXTURE, FakeAdb

    adb = FakeAdb([
        FakeAdb.when("dumpsys meminfo system", MEMINFO_DETAIL_PARTIAL_FIXTURE),
        # 故意不注册 pm list packages——走到 fallback 即 FakeAdb 未匹配报错
    ])
    provider = RealProvider(adb=adb)
    assert provider._probe_meminfo_detail("R3CN80ABCDEF") is True


def test_meminfo_detail_probe_falls_back_when_no_section():
    """mem-2 修复（判据同步修订）：system 明细段**整段缺失** → 补探三方包，
    任一 detail_channel_available → True（探测目标不再否定被测应用侧能力）。"""
    from app.providers.real import RealProvider
    from m6_fixtures import MEMINFO_DETAIL_PARTIAL_FIXTURE, FakeAdb

    adb = FakeAdb([
        FakeAdb.when("dumpsys meminfo system", "No process found for service\n"),
        FakeAdb.when("pm list packages", "package:com.game.a\npackage:com.game.b\n"),
        # 第一个三方包明细段通道存在（被测应用形态）
        FakeAdb.when("dumpsys meminfo com.game.a", MEMINFO_DETAIL_PARTIAL_FIXTURE),
    ])
    provider = RealProvider(adb=adb)
    assert provider._probe_meminfo_detail("R3CN80ABCDEF") is True


def test_meminfo_detail_probe_fallback_all_incomplete_keeps_false():
    """补探包也无明细段（未运行/无明细段不计入判定）→ 维持首选探测 False。"""
    from app.providers.real import RealProvider
    from m6_fixtures import FakeAdb

    adb = FakeAdb([
        FakeAdb.when("dumpsys meminfo system", "No process found for service\n"),
        FakeAdb.when("pm list packages", "package:com.game.a\n"),
        FakeAdb.when("dumpsys meminfo com.game.a", "No process found\n"),
    ])
    provider = RealProvider(adb=adb)
    assert provider._probe_meminfo_detail("R3CN80ABCDEF") is False


def test_meminfo_detail_probe_fallback_unlistable_keeps_first_result():
    """无法列包（pm 失败）→ 不覆盖首选探测结论（None/False 语义保留，不猜）。"""
    from app.providers.real import RealProvider
    from m6_fixtures import FakeAdb

    adb = FakeAdb([
        FakeAdb.when("dumpsys meminfo system", "No process found for service\n"),
        FakeAdb.when("pm list packages", "Error: java.lang.SecurityException\n"),
    ])
    # FakeAdb 应答恒 rc=0，用空应答模拟列包为空 → 无可探包 → None 维持 False
    adb2 = FakeAdb([
        FakeAdb.when("dumpsys meminfo system", "No process found for service\n"),
        FakeAdb.when("pm list packages", ""),
    ])
    provider = RealProvider(adb=adb)
    assert provider._probe_meminfo_detail("R3CN80ABCDEF") is False
    provider2 = RealProvider(adb=adb2)
    assert provider2._probe_meminfo_detail("R3CN80ABCDEF") is False
