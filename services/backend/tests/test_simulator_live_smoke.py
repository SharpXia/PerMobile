"""simulator live smoke（M-iOS §23.5）：需本机 booted 模拟器，无则 skip。

门禁口径（如实声明）：本文件跑真 simctl 子进程（设备发现 + 5s 录屏 + 宿主采样
三步）；无 booted 模拟器或非 macOS 环境 → `pytest.skip`，不伪装通过。CI/无
模拟器环境下整文件跳过属预期行为（fixture 驱动逻辑测在
test_simulator_provider.py / test_simulator_flow.py）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.providers.simulator import SimulatorProvider  # noqa: E402

def _booted_udid() -> str | None:
    """当前 booted iOS 模拟器 UDID；无则 None。"""
    try:
        completed = subprocess.run(
            ["xcrun", "simctl", "list", "devices", "--json"],
            text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        body = json.loads(completed.stdout)
    except ValueError:
        return None
    for runtime_key, rows in (body.get("devices") or {}).items():
        if "SimRuntime.iOS" not in runtime_key:
            continue
        for row in rows or []:
            if row.get("state") == "Booted" and row.get("isAvailable", True):
                return row.get("udid")
    return None


@pytest.fixture(scope="module")
def booted_udid() -> str:
    udid = _booted_udid()
    if udid is None:
        pytest.skip("live smoke：无 booted iOS 模拟器（fixture 测试不受影响）")
    return udid


@pytest.fixture(scope="module")
def live_client(tmp_path_factory: pytest.TempPathFactory) -> TestClient:
    settings = Settings(
        provider="simulator",
        runs_dir=tmp_path_factory.mktemp("sim-live") / "runs",
        cases_db_path=tmp_path_factory.mktemp("sim-live") / "data" / "cases.db",
    )
    app = create_app(settings, provider=SimulatorProvider())
    with TestClient(app) as client:
        client.runs_dir = settings.runs_dir  # type: ignore[attr-defined]
        yield client


def test_live_device_discovery(live_client: TestClient, booted_udid: str) -> None:
    """设备发现：booted 模拟器以 online 行出现（真实 simctl）。"""
    body = live_client.get("/api/devices").json()
    assert body["mode"] == "simulator"
    booted = [d for d in body["devices"] if d["id"] == booted_udid]
    assert booted, f"booted 设备 {booted_udid} 未出现在列表"
    device = booted[0]
    assert device["platform"] == "ios"
    assert device["connection"] == "simulator"
    assert device["state"] == "online"
    assert device["info"]["os"].endswith("(Simulator)")
    assert device["profile"]["simulator"] is True


def test_live_apps_and_session_sampling(live_client: TestClient, booted_udid: str) -> None:
    """应用列表 + 会话宿主口径采样（真实 launchctl/ps 读取）。"""
    apps = live_client.get(f"/api/devices/{booted_udid}/apps").json()
    assert apps["deviceId"] == booted_udid
    assert apps["apps"], "模拟器出厂必有系统应用"

    # 拉起 Settings（保证目标进程存在），建会话采样 ~2s
    subprocess.run(
        ["xcrun", "simctl", "launch", booted_udid, "com.apple.Preferences"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False,
    )
    session = live_client.post("/api/sessions", json={
        "deviceId": booted_udid,
        "packageName": "com.apple.Preferences",
        "metrics": [
            {"metricId": "cpu.proc.host.pct", "intervalMs": 500},
            {"metricId": "memory.rss.mb", "intervalMs": 500},
        ],
    })
    assert session.status_code == 201, session.text
    sid = session.json()["id"]
    try:
        deadline = time.monotonic() + 6
        points: list[dict] = []
        while time.monotonic() < deadline:
            body = live_client.get(f"/api/sessions/{sid}/metrics").json()
            cpu = next(s for s in body["series"] if s["metricId"] == "cpu.proc.host.pct")
            points = cpu.get("points", [])
            if points:
                break
            time.sleep(0.3)
        assert points, "6s 内宿主口径 CPU 采样未出点"
        assert all(isinstance(p["value"], (int, float)) for p in points)
    finally:
        live_client.post(f"/api/sessions/{sid}/discard")


def test_live_record_5s_video(live_client: TestClient, booted_udid: str) -> None:
    """5 秒录屏：recordVideo → SIGINT 收尾 → video.mp4 落盘 + video 锚点。"""
    session = live_client.post("/api/sessions", json={
        "deviceId": booted_udid,
        "packageName": "com.apple.Preferences",
        "metrics": [],
        "recordScreen": True,
    })
    assert session.status_code == 201, session.text
    sid = session.json()["id"]
    live_client.post(f"/api/sessions/{sid}/record/start")
    time.sleep(5.0)
    live_client.post(f"/api/sessions/{sid}/record/stop")
    saved = live_client.post(f"/api/sessions/{sid}/save", json={}).json()
    run_dir = Path(saved["runPath"])
    video = run_dir / "artifacts" / "video.mp4"
    assert video.exists(), "5s 录屏产物 video.mp4 未落盘"
    assert video.stat().st_size > 1024, "录屏产物 <1KB（判废口径）"
    manifest = json.loads((run_dir / "manifest.json").read_text())
    anchors = manifest["context"]["clockAnchors"] or {}
    assert anchors.get("video", {}).get("method", "").startswith("simctl.io.recordVideo")
    assert anchors.get("video", {}).get("clockSource") == "host_epoch"
