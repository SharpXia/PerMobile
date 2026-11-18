"""WiFi 行会话的 adb serial 映射（契约 §21.1，2026-11-04 WiFi 数据面实测暴露）。

WiFi 行 device_id（f"{usb_serial}-wifi"）不是 adb serial；采样上下文、时钟锚点、
pidof 三条命令路径都必须经 provider.adb_serial 映射回 host:port，否则命令打到
不存在的设备上、采样静默无输出（sampler 内部诊断按设计不进 SSE，无任何可见报错）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.sse import SseBus
from app.services import collector_service as mod
from app.services.collector_service import RealCollector, create_collector  # noqa: E402


class _RecordingAdb:
    """记录 command()/run() 收到的 serial，不发起真实 adb 子进程。"""

    def __init__(self) -> None:
        self.serials: list[str | None] = []

    def command(self, *args: str, serial: str | None = None, **kwargs):
        self.serials.append(serial)
        return ["adb", "-s", serial or "", *args]

    def run(self, *args: str, serial: str | None = None, **kwargs):
        self.serials.append(serial)
        return SimpleNamespace(returncode=1, stdout="", stderr="")


def _make(device_id: str, adb, *, serial=None, package_name="com.app.game"):
    session = SimpleNamespace(
        id="sess-w1",
        device_id=device_id,
        metrics={},
        package_name=package_name,
        process_name=None,
        sampler_clock_anchor=None,
    )
    return RealCollector(session, SseBus("sess-w1"), adb=adb, ping_s=60.0, serial=serial)


def test_wifi_session_commands_use_mapped_serial(monkeypatch):
    """WiFi 行三条命令路径（锚点/上下文/pidof）都映射到 host:port serial。"""
    adb = _RecordingAdb()
    collector = _make("ANDROID-TEST-01-wifi", adb, serial="192.0.2.10:5555")
    assert collector._serial == "192.0.2.10:5555"

    # 1) 时钟锚点命令（_default_command_runner 打桩，防真实子进程）
    monkeypatch.setattr(
        mod, "_default_command_runner",
        lambda command, timeout: SimpleNamespace(
            returncode=0, stdout="1793760274720\n12345.67"
        ),
    )
    collector._read_sampler_clock_anchor()

    # 2) pidof 进程探测（需 scheduler 非空，同 RealCollector._on_start 后形态）
    collector._scheduler = SimpleNamespace()
    collector._check_process(now=1000.0)

    assert adb.serials == ["192.0.2.10:5555", "192.0.2.10:5555"]


def test_usb_session_falls_back_to_device_id():
    """USB 行不传 serial：device_id 即 serial，行为与修复前一致。"""
    adb = _RecordingAdb()
    collector = _make("ANDROID-TEST-01", adb)
    assert collector._serial == "ANDROID-TEST-01"
    collector._scheduler = SimpleNamespace()
    collector._check_process(now=1000.0)
    assert adb.serials == ["ANDROID-TEST-01"]


def test_create_collector_passes_serial_through():
    """工厂把 serial 透传给 RealCollector；synthetic 模式不受影响。"""
    session = SimpleNamespace(
        id="sess-w2",
        device_id="SER1-wifi",
        metrics={},
        package_name="com.app",
        process_name=None,
    )
    bus = SseBus("sess-w2")
    collector = create_collector(session, "real", bus, serial="10.0.0.5:5555")
    assert isinstance(collector, RealCollector)
    assert collector._serial == "10.0.0.5:5555"

    synthetic = create_collector(session, "synthetic", bus, serial="10.0.0.5:5555")
    assert not isinstance(synthetic, RealCollector)
