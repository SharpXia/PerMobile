"""M4/M5 区间统计（契约 §18：§12 兼容扩展，M2 统计内核复用，不另算第二份）。

一致性主张：fromMs/toMs 覆盖全程时，fps/帧统计组与无参数全量完全一致
（fpsSeries 基线逐字段相等；frameEvents 基线 durationMs 按 §18 改为区间内
首尾点差，其余组相等——口径差异由契约 §18 明示）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from perfeval.core.timeline import write_timeline

from app.stats.synthetic import generate_frame_events
from conftest import make_session

TIMELINE_SCHEMA = "perfeval.timeline.v1"
FRAME_METRIC = "frame.duration.detail.ms"
FPS_METRIC = "frame.fps.mean"


def _write_fps_samples(path: Path, points: list[tuple[int, float]]) -> None:
    from perfeval.collectors.android.samplers.base import SampleRecord
    from perfeval.collectors.android.samples import SamplesWriter

    writer = SamplesWriter(path)
    try:
        writer.append(
            [SampleRecord(metric_id=FPS_METRIC, value=v, ts_ms=ts) for ts, v in points]
        )
    finally:
        writer.close()


def _write_frame_timeline(path: Path, events, run_id: str) -> None:
    payloads = []
    for seq, frame in enumerate(events, start=1):
        payloads.append(
            {
                "schemaVersion": TIMELINE_SCHEMA,
                "seq": seq,
                "runId": run_id,
                "eventId": f"{run_id}-frame-{seq:03d}",
                "type": "counter",
                "name": FRAME_METRIC,
                "tsMonotonicNs": int(frame.ts_ms * 1_000_000),
                "source": {"platform": "android", "collector": "synthetic.fixture", "evidenceLevel": "L1"},
                "data": {"value": float(frame.duration_ms), "unit": "ms", "direction": "lower_better"},
            }
        )
    with path.open("w", encoding="utf-8") as sink:
        write_timeline(payloads, sink)


# ---- fpsSeries 基线（timeline tsMonotonicNs 按差值消费，时戳域不敏感；
# sim-3 修复后后端自产 run 为 run 内相对纳秒域）----


def _fps_run(client: TestClient) -> tuple[str, list[tuple[int, float]]]:
    points = [(1000, 60.0), (2000, 55.0), (3000, 58.0), (4000, 40.0), (5000, 61.0)]
    run_dir = client.runs_dir / "PD_INTERVAL_FPS"
    run_dir.mkdir(parents=True)
    _write_fps_samples(run_dir / "samples.ndjson", points)
    return "run-PD_INTERVAL_FPS", points


def test_interval_full_range_equals_unparametrized(client: TestClient) -> None:
    run_id, points = _fps_run(client)
    full = client.get(f"/api/runs/{run_id}/statistics").json()
    covered = client.get(
        f"/api/runs/{run_id}/statistics", params={"fromMs": points[0][0], "toMs": points[-1][0]}
    ).json()
    # 区间=全量（覆盖全程）逐字段一致 + 仅多出回显字段
    base = {k: v for k, v in full.items()}
    sliced = {k: v for k, v in covered.items() if k not in ("fromMs", "toMs")}
    assert sliced == base
    assert covered["fromMs"] == 1000 and covered["toMs"] == 5000
    assert covered["durationMs"] == full["durationMs"] == 4000
    # 无参数时不回显（§12 golden 形态不变）
    assert "fromMs" not in full and "toMs" not in full


def test_interval_sub_range_recomputes_with_same_kernel(client: TestClient) -> None:
    from app.stats import kernel

    run_id, points = _fps_run(client)
    body = client.get(
        f"/api/runs/{run_id}/statistics", params={"fromMs": 2000, "toMs": 3000}
    ).json()
    assert body["fromMs"] == 2000 and body["toMs"] == 3000
    assert body["durationMs"] == 1000
    # 与内核对同一输入直接对拍（单一事实源，无第二份实现）
    expected = kernel.fps_group([55.0, 58.0], 1000, [18, 25])
    assert body["fps"] == expected


def test_interval_outside_data_yields_none_baseline(client: TestClient) -> None:
    from app.services.statistics_service import (
        FPS_NOTE_NONE,
        FTIME_NOTE_NONE,
        JANK_NOTE_NONE,
        LOW_FPS_NOTE_NONE,
        SMOOTH_NOTE_NONE,
    )

    run_id, _ = _fps_run(client)
    body = client.get(
        f"/api/runs/{run_id}/statistics", params={"fromMs": 0, "toMs": 500}
    ).json()
    assert body["fromMs"] == 0 and body["toMs"] == 500
    assert body["frameDataBasis"] == "none"
    assert body["durationMs"] == 0
    assert body["fps"]["note"] == FPS_NOTE_NONE
    assert body["frameTime"]["note"] == FTIME_NOTE_NONE
    assert body["jank"]["note"] == JANK_NOTE_NONE
    assert body["smooth"] == {"value": None, "note": SMOOTH_NOTE_NONE}
    assert body["lowFps"] == {"p1": None, "note": LOW_FPS_NOTE_NONE}


def test_interval_validation_errors(client: TestClient) -> None:
    run_id, _ = _fps_run(client)
    url = f"/api/runs/{run_id}/statistics"
    # fromMs > toMs
    resp = client.get(url, params={"fromMs": 3000, "toMs": 2000})
    assert resp.status_code == 400
    assert resp.json()["error"]["details"]["field"] == "fromMs"
    # 非整数
    resp = client.get(url, params={"fromMs": "abc"})
    assert resp.status_code == 400
    assert resp.json()["error"]["details"]["field"] == "fromMs"
    # 负值
    resp = client.get(url, params={"toMs": -1})
    assert resp.status_code == 400
    assert resp.json()["error"]["details"]["field"] == "toMs"
    # run 不存在先于参数校验？——参数校验在前（路由层解析参数后再查目录）
    resp = client.get("/api/runs/run-PD_GHOST/statistics", params={"fromMs": 9, "toMs": 1})
    assert resp.status_code == 400


# ---- frameEvents 基线（帧序列按区间过滤；§18 durationMs=区间首尾点差）----


def test_frame_events_interval_filters_frames_and_keeps_kernel(client: TestClient) -> None:
    from app.stats import kernel

    run_dir = client.runs_dir / "PD_INTERVAL_FRAMES"
    run_dir.mkdir(parents=True)
    events = generate_frame_events("interval-frames", 120)
    _write_frame_timeline(run_dir / "timeline.ndjson", events, "run-PD_INTERVAL_FRAMES")
    run_id = "run-PD_INTERVAL_FRAMES"

    full = client.get(f"/api/runs/{run_id}/statistics").json()
    assert full["frameDataBasis"] == "frameEvents"
    # 帧时戳为 float：上界取 ceil 才能含末帧（闭区间含端点）
    import math

    last_ts = math.ceil(events[-1].ts_ms)
    covered = client.get(
        f"/api/runs/{run_id}/statistics", params={"fromMs": 0, "toMs": last_ts}
    ).json()
    # 覆盖全程：参与计算的帧序列一致 → 同一帧集合进同一内核。
    # 与 duration 相关的组字段按各自口径现算（full=桶数×1000、covered=首尾点差），
    # 与 duration 无关的组字段（均值/方差/计数/占比）必须逐字段一致（契约 §18）。
    frames = [(f.ts_ms, f.duration_ms) for f in events if 0 <= f.ts_ms <= last_ts]
    durations = [d for _, d in frames]
    ts = [t for t, _ in frames]
    expected_duration = int(ts[-1] - ts[0])
    assert covered["durationMs"] == expected_duration
    assert covered["frameTime"]["avg"] == kernel.frame_time_group(durations, expected_duration)["avg"]
    assert covered["lowFps"] == {"p1": kernel.low_fps_p1(durations)}
    for key in ("avg", "min", "median", "medRange", "ge"):
        assert covered["fps"][key] == full["fps"][key], key
    for key in ("avg", "std", "var"):
        assert covered["frameTime"][key] == full["frameTime"][key], key
    for key in ("small", "big", "jank"):
        assert covered["jank"][key] == full["jank"][key], key
    assert covered["smooth"] == full["smooth"]
    assert covered["fromMs"] == 0 and covered["toMs"] == last_ts

    # 子区间：与内核对过滤后帧直接对拍
    sub = client.get(
        f"/api/runs/{run_id}/statistics", params={"fromMs": 500, "toMs": 1000}
    ).json()
    sub_frames = [
        (f.ts_ms, f.duration_ms) for f in events if 500 <= f.ts_ms <= 1000
    ]
    assert sub_frames, "子区间应含帧"
    sub_durations = [d for _, d in sub_frames]
    sub_ts = [t for t, _ in sub_frames]
    assert sub["frameTime"] == kernel.frame_time_group(sub_durations, int(sub_ts[-1] - sub_ts[0]))
    assert sub["lowFps"] == {"p1": kernel.low_fps_p1(sub_durations)}
    assert sub["durationMs"] == int(sub_ts[-1] - sub_ts[0])

    # 区间内无帧 → none 基线同形
    empty = client.get(
        f"/api/runs/{run_id}/statistics", params={"fromMs": 10_000_000, "toMs": 10_000_500}
    ).json()
    assert empty["frameDataBasis"] == "none"
    assert empty["durationMs"] == 0


# ---- 会话路径（§18 session 端点）----


def _session_with_known_points(client: TestClient) -> tuple[str, list[tuple[int, float]]]:
    session = make_session(client, metrics=[{"metricId": FPS_METRIC, "intervalMs": 60}])
    manager = client.app.state.sessions
    session_obj = manager.get(session["id"])
    # 停采集线程，向缓冲注入确定性点（epoch 域 tsMs，晚于 created_at 免换算；
    # timeline 落盘时经 ts_origin_ms 归一为 run 内相对纳秒——sim-3 修复）
    session_obj.collector.stop()
    created = session_obj.created_at
    offsets = [(0, 60.0), (1000, 55.0), (2000, 58.0), (3000, 40.0), (4000, 61.0)]
    from perfeval.collectors.android.samplers.base import SampleRecord

    for offset, value in offsets:
        session_obj.append_record(
            SampleRecord(metric_id=FPS_METRIC, value=value, ts_ms=created + offset, tags={})
        )
    return session["id"], [(created + o, v) for o, v in offsets]


def test_session_interval_full_range_equals_unparametrized(client: TestClient) -> None:
    sid, points = _session_with_known_points(client)
    full = client.get(f"/api/sessions/{sid}/statistics").json()
    covered = client.get(
        f"/api/sessions/{sid}/statistics",
        params={"fromMs": points[0][0], "toMs": points[-1][0]},
    ).json()
    sliced = {k: v for k, v in covered.items() if k not in ("fromMs", "toMs")}
    assert sliced == full
    assert covered["fromMs"] == points[0][0] and covered["toMs"] == points[-1][0]


def test_session_interval_sub_range(client: TestClient) -> None:
    sid, points = _session_with_known_points(client)
    body = client.get(
        f"/api/sessions/{sid}/statistics",
        params={"fromMs": points[1][0], "toMs": points[2][0]},
    ).json()
    assert body["fps"]["avg"] == 56.5  # (55+58)/2
    assert body["durationMs"] == 1000
    assert body["fromMs"] == points[1][0] and body["toMs"] == points[2][0]
    # 区间外无点 → none 基线同形 + 回显
    empty = client.get(
        f"/api/sessions/{sid}/statistics",
        params={"fromMs": points[-1][0] + 10_000, "toMs": points[-1][0] + 20_000},
    ).json()
    assert empty["frameDataBasis"] == "none"
    assert empty["durationMs"] == 0
    assert empty["fromMs"] == points[-1][0] + 10_000


def test_session_interval_validation(client: TestClient) -> None:
    sid, _ = _session_with_known_points(client)
    resp = client.get(
        f"/api/sessions/{sid}/statistics", params={"fromMs": 200, "toMs": 100}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["details"]["field"] == "fromMs"
    resp = client.get(f"/api/sessions/{sid}/statistics", params={"fromMs": "x"})
    assert resp.status_code == 400
