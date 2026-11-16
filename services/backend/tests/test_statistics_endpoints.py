"""statistics 端点装配测试（契约 §12 两个 GET 端点：runId / sessionId 两条路径）。

覆盖：frameEvents / fpsSeries / none 三种数据基线、fpsGe 参数解析、404 错误码、
空值规则（不填假零值）与单一事实源（端点输出 == 内核对同一输入的输出）。
契约 §12 golden 的结构同步在 test_contract_examples.py（§11.5）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.services.statistics_service import (
    FPS_METRIC_ID,
    FTIME_NOTE_FPS_SERIES,
    FTIME_NOTE_NONE,
    FPS_NOTE_NONE,
    JANK_NOTE_FPS_SERIES,
    JANK_NOTE_NONE,
    LOW_FPS_NOTE_FPS_SERIES,
    LOW_FPS_NOTE_NONE,
    SMOOTH_NOTE_COMPUTED,
    SMOOTH_NOTE_FPS_SERIES,
    SMOOTH_NOTE_NONE,
    parse_fps_ge,
)
from app.stats import (
    fps_group,
    frame_statistics,
    generate_frame_events,
)
from conftest import make_session

TIMELINE_SCHEMA = "perfeval.timeline.v1"
FRAME_DETAIL_METRIC_ID = "frame.duration.detail.ms"


def _write_frame_timeline(path: Path, events: list[Any], run_id: str) -> None:
    """按 build_frame_detail_events（gfxinfo.py:412-478）的事件形状写 timeline.ndjson。"""
    from perfeval.core.timeline import write_timeline

    payloads = []
    for seq, frame in enumerate(events, start=1):
        payloads.append(
            {
                "schemaVersion": TIMELINE_SCHEMA,
                "seq": seq,
                "runId": run_id,
                "eventId": f"{run_id}-frame-{seq:03d}",
                "type": "counter",
                "name": FRAME_DETAIL_METRIC_ID,
                "tsMonotonicNs": int(frame.ts_ms * 1_000_000),
                "source": {
                    "platform": "android",
                    "collector": "synthetic.fixture",
                    "evidenceLevel": "L1",
                },
                "data": {
                    "value": float(frame.duration_ms),
                    "unit": "ms",
                    "direction": "lower_better",
                    "frameIndex": seq - 1,
                    "jank": False,  # perfeval 口径标记；statistics 内核不复用（见 parity 测试）
                    "clockDomain": "frame_relative",
                    "fpsSource": "framestats",
                },
            }
        )
    with path.open("w", encoding="utf-8") as sink:
        write_timeline(payloads, sink)


def _write_fps_samples(path: Path, points: list[tuple[int, float]]) -> None:
    from perfeval.collectors.android.samplers.base import SampleRecord
    from perfeval.collectors.android.samples import SamplesWriter

    writer = SamplesWriter(path)
    try:
        writer.append(
            [SampleRecord(metric_id=FPS_METRIC_ID, value=value, ts_ms=ts) for ts, value in points]
        )
    finally:
        writer.close()


def _bucket_counts(events: list[Any]) -> list[float]:
    """1s 桶帧数（与 statistics_service frameEvents 基线同口径的独立重算，供对拍）。"""
    counts: dict[int, int] = {}
    for frame in events:
        index = int(frame.ts_ms // 1000)
        counts[index] = counts.get(index, 0) + 1
    return [float(count) for _, count in sorted(counts.items())]


# ---- runId 路径 ----


def test_run_statistics_frame_events_baseline(client: TestClient) -> None:
    run_dir = client.runs_dir / "PD_FRAME_TEST"
    run_dir.mkdir(parents=True)
    events = generate_frame_events("endpoint-frame", 180)
    _write_frame_timeline(run_dir / "timeline.ndjson", events, "run-PD_FRAME_TEST")

    resp = client.get("/api/runs/run-PD_FRAME_TEST/statistics")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schemaVersion"] == "perfdog.statistics.v1"
    assert body["runId"] == "run-PD_FRAME_TEST"
    assert body["frameDataBasis"] == "frameEvents"
    assert body["frameSeriesIntervalMs"] == 1000

    durations = [frame.duration_ms for frame in events]
    ts_ms = [frame.ts_ms for frame in events]
    buckets = _bucket_counts(events)
    duration_ms = len(buckets) * 1000
    assert body["durationMs"] == duration_ms

    # 单一事实源：端点输出 == 内核对同一输入的输出
    assert body["fps"] == fps_group(buckets, duration_ms, [18, 25])
    expected = frame_statistics(durations, duration_ms, ts_ms)
    assert body["frameTime"] == expected["frameTime"]
    assert body["jank"] == expected["jank"]
    assert body["lowFps"] == expected["lowFps"]
    assert body["smooth"]["value"] == expected["smooth"]["value"]
    # frameEvents 基线：可精确复刻组不带 note；smooth 恒带近似口径 note（契约 §12 规则 1）
    assert body["smooth"]["note"] == SMOOTH_NOTE_COMPUTED
    assert "note" not in body["fps"]
    assert "note" not in body["frameTime"]
    assert "note" not in body["jank"]
    assert "note" not in body["lowFps"]


def test_run_statistics_fps_ge_custom_thresholds(client: TestClient) -> None:
    run_dir = client.runs_dir / "PD_GE_TEST"
    run_dir.mkdir(parents=True)
    events = generate_frame_events("endpoint-ge", 60)
    _write_frame_timeline(run_dir / "timeline.ndjson", events, "run-PD_GE_TEST")
    body = client.get("/api/runs/run-PD_GE_TEST/statistics?fpsGe=30,18,90").json()
    assert list(body["fps"]["ge"]) == ["18", "30", "90"]


def test_run_statistics_fps_series_baseline(client: TestClient) -> None:
    run_dir = client.runs_dir / "PD_FPS_SERIES_TEST"
    run_dir.mkdir(parents=True)
    _write_fps_samples(
        run_dir / "samples.ndjson",
        [(1000 + i * 500, 116.0 + (i % 3)) for i in range(5)],
    )
    body = client.get("/api/runs/run-PD_FPS_SERIES_TEST/statistics").json()
    assert body["frameDataBasis"] == "fpsSeries"
    assert body["frameSeriesIntervalMs"] == 500  # 实测中位差（run 路径无配置可依）
    assert body["durationMs"] == 2000
    # fpsSeries：帧数据缺失组全部 null + 组级 note（不填假零值，[R§8.6]）
    assert body["frameTime"] == {
        "avg": None, "std": None, "var": None, "delta": None, "note": FTIME_NOTE_FPS_SERIES,
    }
    assert body["jank"]["small"] is None and body["jank"]["note"] == JANK_NOTE_FPS_SERIES
    assert body["smooth"] == {"value": None, "note": SMOOTH_NOTE_FPS_SERIES}
    assert body["lowFps"] == {"p1": None, "note": LOW_FPS_NOTE_FPS_SERIES}
    assert FPS_METRIC_ID  # 常量自检，避免误改引用


def test_run_statistics_fps_series_prefers_manifest_declared_interval(
    client: TestClient,
) -> None:
    run_dir = client.runs_dir / "PD_MANIFEST_TEST"
    run_dir.mkdir(parents=True)
    _write_fps_samples(run_dir / "samples.ndjson", [(0, 60.0), (500, 59.0), (1000, 58.0)])
    # 本后端 save 的 run 目录 manifest.json 记录会话配置节拍（session_manager.py:443-461）；
    # 声明节拍优先于实测（与会话路径同语义），旧 run 目录无声明时回退实测
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": "perfeval.manifest.v1",
                "runId": "run-PD_MANIFEST_TEST",
                "metrics": [{"metricId": FPS_METRIC_ID, "intervalMs": 250}],
            }
        ),
        encoding="utf-8",
    )
    body = client.get("/api/runs/run-PD_MANIFEST_TEST/statistics").json()
    assert body["frameDataBasis"] == "fpsSeries"
    assert body["frameSeriesIntervalMs"] == 250  # 声明节拍，非实测 500


def test_run_statistics_frame_events_wins_over_fps_series(client: TestClient) -> None:
    run_dir = client.runs_dir / "PD_BOTH_TEST"
    run_dir.mkdir(parents=True)
    events = generate_frame_events("endpoint-both", 40)
    _write_frame_timeline(run_dir / "timeline.ndjson", events, "run-PD_BOTH_TEST")
    _write_fps_samples(run_dir / "samples.ndjson", [(0, 60.0), (500, 59.0)])
    body = client.get("/api/runs/run-PD_BOTH_TEST/statistics").json()
    assert body["frameDataBasis"] == "frameEvents"  # 逐帧数据优先（契约 §12 基线表）


def test_run_statistics_corrupt_timeline_falls_back_to_fps_series(client: TestClient) -> None:
    run_dir = client.runs_dir / "PD_CORRUPT_TEST"
    run_dir.mkdir(parents=True)
    (run_dir / "timeline.ndjson").write_text("not-json\n", encoding="utf-8")
    _write_fps_samples(run_dir / "samples.ndjson", [(0, 60.0), (1000, 58.0)])
    body = client.get("/api/runs/run-PD_CORRUPT_TEST/statistics").json()
    # timeline 解析失败 → 如实降级到 fpsSeries（不 500、不伪造帧数据）
    assert body["frameDataBasis"] == "fpsSeries"


def test_run_statistics_none_baseline(client: TestClient) -> None:
    (client.runs_dir / "PD_EMPTY_TEST").mkdir(parents=True)
    body = client.get("/api/runs/run-PD_EMPTY_TEST/statistics").json()
    assert body["schemaVersion"] == "perfdog.statistics.v1"
    assert body["frameDataBasis"] == "none"
    assert body["frameSeriesIntervalMs"] is None
    assert body["durationMs"] == 0
    assert body["fps"]["avg"] is None
    assert body["fps"]["ge"] == {"18": None, "25": None}
    assert body["fps"]["note"] == FPS_NOTE_NONE
    for key, note in (("frameTime", FTIME_NOTE_NONE), ("jank", JANK_NOTE_NONE),
                      ("lowFps", LOW_FPS_NOTE_NONE)):
        assert body[key]["note"] == note
        assert all(v is None for k, v in body[key].items() if k != "note")
    assert body["smooth"] == {"value": None, "note": SMOOTH_NOTE_NONE}


def test_run_statistics_not_found_and_traversal_guard(client: TestClient) -> None:
    resp = client.get("/api/runs/run-NO_SUCH_RUN/statistics")
    assert resp.status_code == 404
    error = resp.json()["error"]
    assert error["code"] == "report_not_found"
    # 路径穿越防护：`run-..` 与空名不得映射到 runs 目录本身（裸 ".." 会被 HTTP 客户端
    # 按 RFC 3986 规约为 /api/statistics，故仅测不出 URL 归约的形态）
    for run_id in ("run-..", "run-", "run-%2E%2E"):
        resp = client.get(f"/api/runs/{run_id}/statistics")
        assert resp.status_code == 404, run_id
        assert resp.json()["error"]["code"] == "report_not_found", run_id


def test_parse_fps_ge_validation() -> None:
    assert parse_fps_ge(None) == [18, 25]
    assert parse_fps_ge("25,18,18") == [18, 25]  # 去重 + 升序
    for bad in ("", "abc", "0", "-1", "18,,25", "18.5"):
        try:
            parse_fps_ge(bad)
        except Exception as exc:  # noqa: BLE001
            assert getattr(exc, "code", None) == "validation_error", bad
        else:  # pragma: no cover
            raise AssertionError(f"fpsGe={bad!r} 应为 400 validation_error")


# ---- sessionId 路径 ----


def test_session_statistics_fps_series_baseline(client: TestClient) -> None:
    sid = make_session(
        client, metrics=[{"metricId": "frame.fps.mean", "intervalMs": 500}]
    )["id"]
    time.sleep(1.3)  # 500ms 节拍攒 ≥3 点
    body = client.get(f"/api/sessions/{sid}/statistics").json()
    assert body["schemaVersion"] == "perfdog.statistics.v1"
    assert body["sessionId"] == sid
    assert body["frameDataBasis"] == "fpsSeries"
    assert body["frameSeriesIntervalMs"] == 500  # 声明会话配置节拍（契约 §12 规则 2）
    assert isinstance(body["durationMs"], int) and body["durationMs"] > 0
    fps = body["fps"]
    assert list(fps["ge"]) == ["18", "25"]
    assert isinstance(fps["avg"], float)
    assert isinstance(fps["drop"], (int, float))
    assert fps["note"] == (
        "fps 点列间隔 500ms（frame.fps.mean 轮询），与 PerfDog 1 秒点列口径存在节拍差；"
        "Drop(FPS) 等相邻点统计受节拍影响，报告须声明间隔（[F§3.1]）"
    )
    assert body["frameTime"]["note"] == FTIME_NOTE_FPS_SERIES
    assert body["frameTime"]["avg"] is None
    assert body["jank"]["note"] == JANK_NOTE_FPS_SERIES
    assert body["jank"]["jank"] is None
    assert body["smooth"] == {"value": None, "note": SMOOTH_NOTE_FPS_SERIES}
    assert body["lowFps"] == {"p1": None, "note": LOW_FPS_NOTE_FPS_SERIES}

    # 单一事实源：端点 fps 组 == 内核对缓冲点列的输出
    points = sorted(
        (row["tsMs"], row["value"])
        for row in client.app.state.sessions.get(sid).records()
        if row["metricId"] == FPS_METRIC_ID and row["value"] is not None
    )
    values = [value for _, value in points]
    assert fps["avg"] == fps_group(values, points[-1][0] - points[0][0], [18, 25])["avg"]


def test_session_statistics_no_points_none_baseline(client: TestClient) -> None:
    # 不启动采集器（manager 直建）：缓冲无 fps 点 → none 基线（含 sessionId）
    session = client.app.state.sessions.create(
        device_id="synthetic-001",
        package_name="com.example.synthetic.game",
        metric_entries=[{"metricId": FPS_METRIC_ID, "intervalMs": 500}],
    )
    body = client.get(f"/api/sessions/{session.id}/statistics").json()
    assert body["sessionId"] == session.id
    assert body["frameDataBasis"] == "none"
    assert body["durationMs"] == 0
    assert body["fps"]["note"] == FPS_NOTE_NONE
    assert body["fps"]["ge"] == {"18": None, "25": None}
    assert body["smooth"]["note"] == SMOOTH_NOTE_NONE
    assert body["frameTime"]["note"] == FTIME_NOTE_NONE


def test_session_statistics_null_points_excluded(client: TestClient) -> None:
    # value:null 瞬时缺口不入统计（契约 §1：持久层只存数值点）
    session = client.app.state.sessions.create(
        device_id="synthetic-001",
        package_name="com.example.synthetic.game",
        metric_entries=[{"metricId": FPS_METRIC_ID, "intervalMs": 500}],
    )
    session.append_null_record(FPS_METRIC_ID)
    body = client.get(f"/api/sessions/{session.id}/statistics").json()
    assert body["frameDataBasis"] == "none"  # 仅 null 点 → 视为无帧数据


def test_session_statistics_fps_ge_custom(client: TestClient) -> None:
    sid = make_session(
        client, metrics=[{"metricId": "frame.fps.mean", "intervalMs": 200}]
    )["id"]
    time.sleep(0.7)
    body = client.get(f"/api/sessions/{sid}/statistics?fpsGe=90").json()
    assert list(body["fps"]["ge"]) == ["90"]


def test_session_statistics_errors(client: TestClient) -> None:
    resp = client.get("/api/sessions/sess-99/statistics")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "session_not_found"
    resp = client.get("/api/sessions/sess-99/statistics?fpsGe=bad")
    assert resp.status_code == 404  # 404 先于参数校验（会话不存在）


def test_session_statistics_fps_ge_invalid(client: TestClient) -> None:
    sid = make_session(client)["id"]
    for bad in ("", "abc", "0"):
        resp = client.get(f"/api/sessions/{sid}/statistics?fpsGe={bad}")
        assert resp.status_code == 400, bad
        assert resp.json()["error"]["code"] == "validation_error", bad
