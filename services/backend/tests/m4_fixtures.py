"""M4 测试 fixture（契约 §16/§17/§19）：假 adb runner 驱动**真实** perfeval
``_ScreenRecordSession`` 生命周期（与 perfeval tests/test_screen_record_watchdog.py
同款注入接缝——command_runner + 注入时钟），fixture mp4 bytes 充当设备产物。

如实标注：真机路径（probe/clock/启动/收尾/pull 的 adb I/O）无设备不可测，这里
只替身 adb 传输层；分段/watchdog/rollover/判废/锚点/落盘映射逻辑全部走生产代码。
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path
from typing import Any, Iterator

from app.providers.real import RealProvider
from app.registry import available_metric_ids
from app.config import Settings
from app.main import create_app
from app.services.screen_capture import create_session_recorder_for_test
from perfeval.collectors.android.adb import AdbClient
from fastapi.testclient import TestClient

# ---- fixture 工件字节 ----

# 最小合法 mp4 头（ftyp box，perfeval 同款 fixture 形态）+ 确定性 padding
FIXTURE_MP4_HEADER = b"\x00\x00\x00\x18ftypmp42"


def make_segment_bytes(tag: str, size: int) -> bytes:
    """确定性 mp4 fixture：header + tag + padding，共 size 字节（size>=8）。"""
    body = FIXTURE_MP4_HEADER + tag.encode()
    return body + b"\x00" * max(0, size - len(body))


# 最小 PNG fixture：8 字节签名 + IHDR 长度/类型 + 少量字节
FIXTURE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + b"\x00" * 16


class FakeClock:
    """注入时钟（段内耗时由测试推进，驱动 watchdog 中途 rollover）。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> Any:
    result = type("_R", (), {})()
    result.stdout = stdout
    result.returncode = returncode
    result.stderr = stderr
    return result


def fake_screenrecord_runner(
    executed: list[str], segment_sizes: dict[str, int]
) -> Any:
    """假 adb 传输层（perfeval watchdog 测试同款）：

    - probe/clock/启动/pkill 原样应答；
    - pull 把 fixture mp4 字节写到命令最后一个参数（本地路径），字节数按
      ``segment_sizes``（本地文件名 → 大小；缺省 4096）注入——0 字节/512B 即
      模拟 ROM 限制空录屏（<1KB 判废路径）。
    """

    def runner(command: list[str], timeout: float | None) -> Any:
        joined = " ".join(command)
        if "command -v screenrecord" in joined:
            executed.append("probe")
            return _completed("/system/bin/screenrecord\n")
        if "date +%s%3N" in joined:
            executed.append("clock")
            return _completed("1757300002000\n812345.678 12345.6\n")
        if "nohup screenrecord" in joined:
            remote = next(
                token for token in command[-1].split() if token.startswith("/sdcard/perfeval-")
            )
            executed.append(f"start:{remote}")
            return _completed("")
        if "pkill -INT screenrecord" in joined:
            executed.append("stop")
            return _completed("")
        if len(command) >= 3 and command[-3] == "pull":
            local_name = Path(command[-1]).name
            executed.append(f"pull:{local_name}")
            size = segment_sizes.get(local_name, 4096)
            Path(command[-1]).write_bytes(make_segment_bytes(local_name, size))
            return _completed("pulled")
        return _completed("")

    return runner


class _StubCollectorAdb:
    """RealCollector 用 stub：不发起真实 adb 子进程（fixture 无真机）。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def available(self) -> bool:
        return True

    def command(self, *args: str, serial: str | None = None) -> list[str]:
        return ["adb", *(["-s", serial] if serial else []), *args]

    def run(self, *args: str, serial: str | None = None, timeout: float | None = None) -> Any:
        return _completed("12345")


def fake_collector_command_runner(command: list[str], timeout: float | None) -> Any:
    """_default_command_runner 替身：date 命令回固定设备时钟读数（B1 锚点）。"""
    joined = " ".join(command)
    if "date +%s%3N" in joined:
        return _completed("1757300001000\n812345.678 12345.6\n")
    return _completed("12345")


class FakeRealProvider(RealProvider):
    """real 模式 provider 替身：设备枚举固定一台在线设备，不触真实 adb。"""

    def __init__(self) -> None:
        super().__init__(adb=AdbClient("/fake-adb"))

    def list_devices(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "fake-real-001",
                "platform": "android",
                "model": "Fake Real Device",
                "serial": "fake-real-001",
                "state": "online",
                "connection": "usb",
                "info": {},
            }
        ]

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        for device in self.list_devices():
            if device["id"] == device_id:
                return device
        return None

    def list_metric_ids(self) -> list[str]:
        return available_metric_ids("real")


@contextlib.contextmanager
def make_real_client(
    tmp_path: Path,
    monkeypatch: Any,
    *,
    recorder_command_runner: Any,
    clock: FakeClock | None = None,
    watchdog_interval_s: float = 0.01,
) -> Iterator[TestClient]:
    """real 模式测试客户端：fixture 录屏工厂 + stub 采集 adb（无真机，如实标注）。"""
    import app.services.collector_service as collector_service

    clock = clock or FakeClock()
    monkeypatch.setattr(collector_service, "AdbClient", _StubCollectorAdb)
    monkeypatch.setattr(
        collector_service, "_default_command_runner", fake_collector_command_runner
    )

    def factory(session: Any, run_path: Path) -> Any:
        return create_session_recorder_for_test(
            session,
            run_path,
            command_runner=recorder_command_runner,
            clock=clock,
            watchdog_interval_s=watchdog_interval_s,
        )

    settings = Settings(
        provider="real",
        runs_dir=tmp_path / "runs",
        cases_db_path=tmp_path / "data" / "cases.db",
    )
    app = create_app(settings, provider=FakeRealProvider(), screen_recorder_factory=factory)
    with TestClient(app) as client:
        client.runs_dir = settings.runs_dir  # type: ignore[attr-defined]
        yield client


def make_recording_session(
    client: TestClient, *, record_screen: bool = True, metrics: list | None = None
) -> dict:
    body: dict[str, Any] = {
        "deviceId": "fake-real-001",
        "packageName": "com.example.fakegame",
        "metrics": metrics if metrics is not None else [],
        "interactive": False,
        "launchOnCreate": False,
        "recordScreen": record_screen,
    }
    resp = client.post("/api/sessions", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def wait_until(predicate: Any, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met before timeout")
