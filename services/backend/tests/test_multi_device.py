"""R22 多设备（契约 §20.4）：synthetic ≥2 台各开一个会话 + device_busy 按 id 判定。"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

import pytest  # noqa: F401 - monkeypatch fixture 类型标注用

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

from conftest import make_session  # noqa: E402


def test_two_devices_concurrent_sessions(client: TestClient) -> None:
    """synthetic ≥2 台可同时各开一个会话（R22 演示口径，无新端点）。"""
    s1 = make_session(client)  # synthetic-001
    s2 = make_session(client, deviceId="synthetic-002")  # 第二台独立会话
    assert s1["deviceId"] == "synthetic-001"
    assert s2["deviceId"] == "synthetic-002"
    assert s1["id"] != s2["id"]
    assert s1["state"] == s2["state"] == "monitoring"
    # 两会话各自独立出点（设备级任务隔离：各自 collector 线程 + 独立缓冲）
    time.sleep(0.25)
    for sid in (s1["id"], s2["id"]):
        body = client.get(f"/api/sessions/{sid}/metrics").json()
        points = [p for entry in body["series"] for p in entry["points"]]
        assert points, f"会话 {sid} 应有采样点"
    # 清理
    client.post(f"/api/sessions/{s1['id']}/discard")
    client.post(f"/api/sessions/{s2['id']}/discard")


def test_device_busy_is_per_device(client: TestClient) -> None:
    """同一设备同时只允许一个活跃会话（device_busy 沿用，按设备 id 独立判定）。"""
    s1 = make_session(client)  # synthetic-001 占用
    busy = client.post("/api/sessions", json={
        "deviceId": "synthetic-001",
        "packageName": "com.example.synthetic.game",
        "interactive": False,
        "launchOnCreate": False,
    })
    assert busy.status_code == 409
    error = busy.json()["error"]
    assert error["code"] == "device_busy"
    assert error["details"]["sessionId"] == s1["id"]
    # 第二台不受影响（R22 设备隔离语义）
    s2 = make_session(client, deviceId="synthetic-002")
    assert s2["state"] == "monitoring"
    # 001 收口后可再建
    client.post(f"/api/sessions/{s1['id']}/discard")
    again = make_session(client)
    assert again["deviceId"] == "synthetic-001"
    client.post(f"/api/sessions/{again['id']}/discard")
    client.post(f"/api/sessions/{s2['id']}/discard")


def test_second_device_apps_inventory(client: TestClient) -> None:
    """第二台设备应用列表独立（R20 子进程演示两台均可经 processName 校验）。"""
    body = client.get("/api/devices/synthetic-002/apps").json()
    assert body["deviceId"] == "synthetic-002"
    game = next(
        a for a in body["apps"]
        if a["packageName"] == "com.example.synthetic.game"
    )
    process_names = [p["name"] for p in game["processes"]]
    assert process_names == [
        "com.example.synthetic.game", "com.example.synthetic.game:sub",
    ]
    # 404：不存在的设备 id
    missing = client.get("/api/devices/ghost/apps")
    assert missing.status_code == 404


# ---- 2026-11-06 多设备矩阵实测回归：并发录制临时目录不得共享 ----


def _cpu_sample_count(run_path: str) -> int:
    lines = (Path(run_path) / "samples.ndjson").read_text(encoding="utf-8").splitlines()
    return sum(1 for line in lines if '"cpu.total.pct"' in line)


def test_concurrent_record_same_second_distinct_run_dirs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两台设备同秒 record/start 不得共享临时 run 目录（多设备矩阵实测缺陷）。

    缺陷（Pixel_9/Pixel_9_2 双模拟器 live 实测，2026-11-06）：临时目录名
    ``PD_%Y%m%d_%H%M%S`` 只到秒级，两台设备同秒 record/start 经
    ``mkdir(exist_ok=True)`` 共享同一目录——双方 writer 追加同一
    samples.ndjson（跨设备样本串写），先 save 的一方 rename 后另一方
    ``session.run_path`` 失效，save 新建空目录 → samples.ndjson 整体丢失。
    """
    from app.services import session_manager as sm_module

    class FrozenDateTime(datetime):
        """固定 now()：强制两次 record/start 落进同一秒（复现重名路径）。"""

        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return cls(2026, 11, 6, 12, 0, 0)

    monkeypatch.setattr(sm_module, "datetime", FrozenDateTime)

    s1 = make_session(client)  # synthetic-001
    s2 = make_session(client, deviceId="synthetic-002")
    assert client.post(f"/api/sessions/{s1['id']}/record/start").status_code == 200
    assert client.post(f"/api/sessions/{s2['id']}/record/start").status_code == 200
    time.sleep(1.0)  # synthetic 60ms 节拍，让两会话各自累积可区分的样本量

    r1 = client.post(f"/api/sessions/{s1['id']}/record/stop").json()
    r2 = client.post(f"/api/sessions/{s2['id']}/record/stop").json()
    cnt1 = r1["stats"]["cpu.total.pct"]["count"]
    cnt2 = r2["stats"]["cpu.total.pct"]["count"]
    assert cnt1 > 10 and cnt2 > 10

    saved1 = client.post(
        f"/api/sessions/{s1['id']}/save", json={"caseName": "mm-collision-a"}
    ).json()
    saved2 = client.post(
        f"/api/sessions/{s2['id']}/save", json={"caseName": "mm-collision-b"}
    ).json()
    assert saved1["runPath"] != saved2["runPath"]

    # 修复前：A 的 run 混入 B 的样本（cpu 计数≈cnt1+cnt2），B 的 run 无
    # samples.ndjson（save 时 run_path 已被 A rename 走，新建空目录）。
    assert _cpu_sample_count(saved1["runPath"]) == cnt1, (
        "run A 样本数应等于其自身缓冲计数（不得混入会话 B 的样本）"
    )
    assert _cpu_sample_count(saved2["runPath"]) == cnt2, (
        "run B 的 samples.ndjson 不得丢失（临时目录重名曾致整体丢失）"
    )


def test_record_start_survives_mkdir_race(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TOCTOU 竞态（复核 2026-11-06）：exists 检查通过后 mkdir 才撞名。

    record/start 端点为同步 def 走线程池可真并发——修复前「先 exists 后
    mkdir(exist_ok=False)」在检查通过与占位之间的窗口内，后到请求会以未捕获
    FileExistsError 变 500。修复以 mkdir 为原子占位，FileExistsError 顺延序号
    重试。本测试 monkeypatch Path.mkdir 在首次 exist_ok=False 调用时抛
    FileExistsError（确定性模拟「exists 通过后目录被并发抢先」），断言请求
    200 且临时目录顺延到 _2。
    """
    from app.services import session_manager as sm_module

    s1 = make_session(client)
    original_mkdir = Path.mkdir
    state = {"raced": False}

    def racing_mkdir(self, mode=0o777, parents=False, exist_ok=False):
        if not state["raced"] and exist_ok is False:
            # 仅拦 start_record 的原子占位调用（其余 mkdir 均 exist_ok=True）
            state["raced"] = True
            raise FileExistsError(self)
        return original_mkdir(self, mode, parents, exist_ok)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)
    resp = client.post(f"/api/sessions/{s1['id']}/record/start")
    assert resp.status_code == 200, resp.text
    assert state["raced"]
    client.post(f"/api/sessions/{s1['id']}/record/stop")
    saved = client.post(
        f"/api/sessions/{s1['id']}/save", json={"caseName": "mm-race-survivor"}
    ).json()
    assert Path(saved["runPath"]).is_dir()
