"""R18 StartupTiming（契约 §21.4）：GET /api/runs/{runId}/startup——TotalTime
透出 + TTID/TTFD（S3 startup.ndjson 捕获回填）+ note 口径声明。

TotalTime 事件语料与 perfeval runner `_derive_startup_event`（runner.py:864-895，
[R§2.5]）产物同形（timeline.ndjson 的 startup.cold.ms metric 事件）；会话内
测量触发不在本端点范围（契约：默认不在实时监控会话内自动执行，只读 run 产物）。
S3（compare-startup-report 流）：TTID/TTFD 由 POST /runs/{id}/startup/capture
旁路作业落盘 startup.ndjson 后回填（详见 test_startup_capture.py）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.routers.reports import STARTUP_NOTE, STARTUP_NOTE_NO_CAPTURE, STARTUP_NOTE_NO_DATA  # noqa: E402


def _write_startup_timeline(runs_dir: Path, case_name: str, total_ms: int) -> None:
    """startup.cold.ms 事件（runner._derive_startup_event 产物同形）落盘。"""
    from perfeval.core.timeline import write_timeline

    run_dir = runs_dir / case_name
    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"run-{case_name}"
    event = {
        "schemaVersion": "perfeval.timeline.v1",
        "seq": 1,
        "runId": run_id,
        "eventId": f"{run_id}-android-startup-001",
        "type": "metric",
        "name": "startup.cold.ms",
        "tsMonotonicNs": 0,
        "source": {"platform": "android", "collector": "am_start", "evidenceLevel": "L0"},
        "data": {
            "value": float(total_ms),
            "unit": "ms",
            "direction": "lower_better",
            "launchState": "cold",
            "source": "am_start -W TotalTime",
        },
    }
    with (run_dir / "timeline.ndjson").open("w", encoding="utf-8") as sink:
        write_timeline([event], sink)


def test_startup_total_time_with_data(client: TestClient) -> None:
    """有 TotalTime 无捕获产物：golden 形态（TTID/TTFD null + 无捕获 note 增补）。"""
    _write_startup_timeline(client.runs_dir, "PD_20261101_100001", 842)
    resp = client.get("/api/runs/run-PD_20261101_100001/startup")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # golden 形态（S3 后：note 增补「未执行启动捕获作业」增补段，契约 delta 待 S1 落笔）
    assert body == {
        "runId": "run-PD_20261101_100001",
        "totalTimeMs": 842,
        "ttidMs": None,
        "ttfdMs": None,
        "note": STARTUP_NOTE + STARTUP_NOTE_NO_CAPTURE,
    }
    # note 必须显式声明口径差异（矩阵 M6 行既定）
    for token in ("reportFullyDrawn", "[F§3.11]", "am start -W", "null"):
        assert token in body["note"]


def test_startup_with_capture_backfills_ttid_ttfd(client: TestClient) -> None:
    """startup.ndjson 捕获产物 → GET /startup 回填 ttidMs/ttfdMs（TotalTime 仍走 timeline）。"""
    from app.services.startup_capture import capture_startup

    case = "PD_20261101_100003"
    _write_startup_timeline(client.runs_dir, case, 842)
    _FakeRunner, _FakeSource = _capture_fakes()
    capture_startup(
        client.runs_dir,
        f"run-{case}",
        "FAKE001",
        line_source_factory=_FakeSource(
            "06-25 10:30:00.500  12345  12345 I ActivityManager: "
            "Displayed com.perfeval.fixture/.MainActivity: +1s234ms",
            "06-25 10:30:02.345  12345  12345 I ActivityTaskManager: "
            "Fully drawn com.perfeval.fixture/.MainActivity: +2s345ms",
        ).factory(),
        command_runner=_FakeRunner(),
        total_deadline_s=2.0,
        ttfd_grace_s=0.2,
    )
    resp = client.get(f"/api/runs/run-{case}/startup")
    assert resp.status_code == 200
    body = resp.json()
    assert body["totalTimeMs"] == 842  # timeline 事件口径不变
    assert body["ttidMs"] == 1234 and body["ttfdMs"] == 2345
    assert body["note"] == STARTUP_NOTE


def test_startup_no_data_is_200_with_null_and_note(client: TestClient) -> None:
    (client.runs_dir / "PD_20261101_100002").mkdir(parents=True)
    resp = client.get("/api/runs/run-PD_20261101_100002/startup")
    assert resp.status_code == 200  # 无数据是常态不是错误
    body = resp.json()
    assert body["totalTimeMs"] is None  # 字段缺失 ≠ 0
    assert body["ttidMs"] is None and body["ttfdMs"] is None
    assert body["note"] == STARTUP_NOTE_NO_DATA
    assert "am start -W 未执行" in body["note"]


def test_startup_synthetic_run_without_startup_data(client: TestClient) -> None:
    """synthetic 会话 save 产物无启动测量（am start -W 未执行）→ totalTimeMs null。"""
    from conftest import make_session

    sid = make_session(client)["id"]
    time.sleep(0.2)
    saved = client.post(f"/api/sessions/{sid}/save", json={}).json()
    resp = client.get(f"/api/runs/{saved['runId']}/startup")
    assert resp.status_code == 200
    assert resp.json()["totalTimeMs"] is None


def test_startup_missing_run_404(client: TestClient) -> None:
    resp = client.get("/api/runs/run-NOPE/startup")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "report_not_found"


def _capture_fakes() -> tuple[type, type]:
    """复用 test_startup_capture 的 fake adb（同文件互导避免 fixture 重复）。"""
    from test_startup_capture import _FakeRunner, _FakeSource

    return _FakeRunner, _FakeSource
