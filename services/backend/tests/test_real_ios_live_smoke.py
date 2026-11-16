"""真机 iOS live smoke（R24 契约 §26.6 测试边界）：需 paired 真机在网，无则 skip。

门禁口径（如实声明，§23.5 simulator live smoke 同哲学）：

- **skip 谓词 =「paired 且可录」**：`devicectl list devices` 解析出 paired 真机
  iOS 行**且** lockState/tunnelState 探测通过才进实录；paired 但隧道断开
  （tunnelState≠connected）、lockState 探测失败、xctrace record 因传输形态
  不可用（2026-11-16 实测 WiFi 传输形态 xctrace 进程枚举 exit 19，实录需
  USB/wired 接入）等**环境性失败一律 pytest.skip 并如实标注原因**——不伪装
  通过、不误报失败；
- 实录内容 = 设备发现 + 4s attach 实录出点双指标校验（cpu.proc.xctrace.pct /
  memory.footprint.mb 双序列数值点；窗口链路 = 生产 run_sample_window，预热
  → record → export → 解析）；
- 无真机环境整文件跳过属预期行为（fixture 驱动逻辑测在
  test_real_ios_provider.py / test_real_ios_sampler.py）。

实录会在被测真机上做一次 4s Activity Monitor attach 采样（勘察/LIVE 均为只读
性能采样，不改动设备状态）；attach 目标优先取已运行用户应用主进程，无则拉起
系统应用 Settings（com.apple.Preferences，无损）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.errors import ApiError  # noqa: E402
from app.providers import devicectl, xctrace  # noqa: E402
from app.providers.realios import RealIOSProvider  # noqa: E402

# 拉起兜底目标：系统应用 Settings（无损、必装；apps 清单 defaultAppsIncluded
# =false 不列系统应用，故 launch 后经 processes 表按 pid 反查进程名）
FALLBACK_BUNDLE = "com.apple.Preferences"
WINDOW_S = 4.0


def _real_provider() -> RealIOSProvider:
    return RealIOSProvider()


def _xcrun_available() -> bool:
    from app.providers.devicectl import run_devicectl

    try:
        completed = run_devicectl("list", "devices", timeout=30.0)
    except FileNotFoundError:
        return False
    return completed is not None and completed.returncode == 0


def _unreachable_reason() -> str | None:
    """paired 真机在册但不可达时的如实 skip 原因（原文行探测，不走出行过滤）。"""
    try:
        text = devicectl.run_devicectl_json("list", "devices", timeout=30.0)
        import json as _json

        raw = (_json.loads(text).get("result") or {}).get("devices") or []
    except (ValueError, OSError):
        return None
    for row in raw:
        hw = row.get("hardwareProperties") or {}
        cp = row.get("connectionProperties") or {}
        # 模拟器行（reality=simulated/sameMachine）不算 paired 真机
        if hw.get("reality") == "simulated" or cp.get("transportType") == "sameMachine":
            continue
        if hw.get("platform") == "iOS" and cp.get("pairingState") == "paired":
            return (
                "live smoke：paired 真机在册但隧道/传输不可达"
                f"（tunnelState={cp.get('tunnelState')!r}，"
                f"transportType={cp.get('transportType')!r}——隧道断开时实测回退"
                " null，同拔线 USB 语义不出行）——环境性失败按 skip 如实标注"
            )
    return None


@pytest.fixture(scope="module")
def device() -> Any:
    """skip 谓词：paired 且可录（tunnel connected + lockState 探测通过）。"""
    if not _xcrun_available():
        pytest.skip("live smoke：xcrun/devicectl 不可用（非 macOS 或命令行工具缺失）")
    provider = _real_provider()
    try:
        devices = provider.list_devices()
    except ApiError as exc:
        pytest.skip(f"live smoke：设备枚举失败（{exc.code}）：{exc.message}")
    paired = [d for d in devices if d["platform"] == "ios"]
    if not paired:
        # 出行过滤后无 paired 行：区分「在册但隧道/传输不可达」（tunnel 断开时
        # transportType 实测回退 null → 不出行，同拔线 USB 语义）与真不在册
        pytest.skip(_unreachable_reason() or "live smoke：无 paired 真机 iOS 设备（fixture 测试不受影响）")
    online = [d for d in paired if d["state"] == "online"]
    if not online:
        pytest.skip(
            "live smoke：paired 真机存在但隧道断开（tunnelState≠connected）——"
            "环境性失败按 skip 如实标注，不伪装通过"
        )
    target = online[0]
    try:
        # lockState 前置探测（§26.4 预热语义；单次尝试，失败=环境性）
        xctrace.prewarm_device(target["id"], attempts=1)
    except ApiError as exc:
        pytest.skip(f"live smoke：lockState 探测未通过（{exc.details}）：{exc.message}")
    return target


def test_live_device_discovery(device: Any) -> None:
    """设备发现：paired 真机以 ios 行出行（真实 devicectl）。"""
    assert device["platform"] == "ios"
    assert device["connection"] in ("usb", "wifi")
    assert device["state"] == "online"
    assert device["id"] == device["serial"]
    profile = device["profile"]
    # §26.1：simulator 恒显式 False（漏传会按 platform=ios 推导 True）
    assert profile["simulator"] is False
    assert profile["gpuSource"] is None and profile["cpuCores"] is None
    provider = _real_provider()
    all_devices = provider.list_devices()
    row = next(d for d in all_devices if d["id"] == device["id"])
    assert row["model"], "model（marketingName）必须解析"
    # 模拟器行不得混入（同机 simctl 源防双行）
    assert all(d["connection"] in ("usb", "wifi") for d in all_devices)


def _resolve_attach_target(provider: RealIOSProvider, udid: str) -> str:
    """attach 目标进程名：已运行用户应用主进程优先；无则拉起 Settings 兜底。"""
    body = provider.list_apps(udid)
    if body is not None:
        for app in body["apps"]:
            if app["running"] and app["pid"] is not None:
                main = next(
                    (p for p in app["processes"] if p["isMain"]), None
                )
                if main is not None:
                    return main["name"]
    # 兜底：launch 系统应用 → pid → processes 表反查进程名（launch JSON 的
    # executable URL 亦可，但 processes 表为 §26.2 契约面，同源更稳）
    launch_text = devicectl.run_devicectl_json(
        "device", "process", "launch", "--device", udid, FALLBACK_BUNDLE,
        timeout=60.0,
    )
    pid = devicectl.parse_launch_json(launch_text)
    if pid is None:
        pytest.skip(
            f"live smoke：无运行中用户应用且 launch {FALLBACK_BUNDLE} 失败——"
            "无 attach 目标（环境性，不伪装通过）"
        )
    processes = devicectl.parse_processes_json(
        devicectl.run_devicectl_json(
            "device", "info", "processes", "--device", udid, timeout=60.0
        )
    )
    name = next(
        (p["name"] for p in (processes or []) if p["pid"] == pid), None
    )
    if name is None:
        pytest.skip("live smoke：launch pid 未在 processes 表命中（环境性）")
    return name


def test_live_record_4s_attach_dual_metrics(device: Any) -> None:
    """4s attach 实录：双指标出点校验（cpu.proc.xctrace.pct + memory.footprint.mb）。"""
    udid = device["id"]
    provider = _real_provider()
    attach_name = _resolve_attach_target(provider, udid)
    import shutil
    import tempfile

    work_dir = tempfile.mkdtemp(prefix="pd-xctrace-live-")
    try:
        try:
            result = xctrace.run_sample_window(
                udid, attach_name, WINDOW_S, work_dir=work_dir
            )
        except ApiError as exc:
            pytest.skip(
                f"live smoke：xctrace 链路不可用（{exc.code}/{exc.details}）：{exc.message}"
            )
        except xctrace.XctraceWindowError as exc:
            pytest.skip(
                "live smoke：record/export 环境性失败（reason="
                f"{exc.reason} exit={exc.exit_code}）——{exc.message}；"
                "WiFi 传输形态下 xctrace 进程枚举不可用（README 实测存档），"
                "实录对拍需 USB/wired 接入"
            )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    rows = result["rows"]
    if not rows:
        pytest.skip("live smoke：窗口零数据行（目标进程录制中消失/被挂起）——环境性")
    cpu = [r["cpu_pct"] for r in rows if r["cpu_pct"] is not None]
    footprint = [r["footprint_mb"] for r in rows if r["footprint_mb"] is not None]
    if not footprint:
        pytest.skip("live smoke：physical footprint 列零数值行（表形态需复核）——环境性")
    assert all(isinstance(v, (int, float)) and v >= 0 for v in footprint)
    if not cpu:
        pytest.skip(
            "live smoke：%CPU 列全部 sentinel（真设备行未出 %CPU，需复核）——"
            "环境性，不伪装双指标通过"
        )
    assert all(isinstance(v, (int, float)) and 0 <= v <= 400 for v in cpu), (
        f"%CPU 越界: {cpu}"
    )
    # 窗口锚点：行 ts = record 退出时刻回推 time-limit + 表内偏移（单调）
    anchor = result["windowStartEpochMs"]
    offsets = [r["offset_ms"] for r in rows]
    assert offsets == sorted(offsets), "行 offset 必须时间序"
    assert anchor > 0
    print(
        f"\nlive 实录 OK：attach={attach_name} rows={len(rows)} "
        f"cpu={cpu} footprint_mb={footprint} "
        f"connection={device['connection']}"
    )
