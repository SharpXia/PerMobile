"""R32 数据面：label 段切分统计（`GET /api/runs/{runId}/statistics?byLabelSegment=1`，
契约 §18 实现期扩展；§11.1 新增可选参数/响应字段不升版本，不带参数 = §12/§18 现行为）。

切分边界用例（本任务验收项）：
- 段间/段前空隙：首个 label 前的数据不入段；段右端 = 下一 label tsMs - 1；
  零宽段（同刻双 label）不出行；
- 无 label：``labelSegments: []``，顶层 body 与不带参数时逐字段一致；
- 首尾截断：请求 fromMs/toMs 与段边界求交（无交集段不出行）；末段右端 =
  数据结束时刻（帧/fps 末点较大者，帧浮点时戳上界取 ceil 含末帧）。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from perfeval.core.timeline import write_timeline

from app.services.marker_service import write_markers_sidecar
from app.stats import kernel
from app.stats.synthetic import generate_frame_events

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


def _write_frame_timeline_epoch(
    path: Path, frames: list[tuple[int, float]], run_id: str
) -> None:
    """逐帧 timeline（epoch 域输入形态：无 tsMonotonicNs，data.tsEpochMs 兜底 →
    ts_ms=epoch；statistics 按差值/排序消费，两种时戳域均兼容——sim-3 修复后
    后端自产 run 的 tsMonotonicNs 为 run 内相对纳秒域）。

    与 sidecar label 同为 epoch ms——后端自产 run 的实际形态，段边界可直接求交。
    """
    payloads = []
    for seq, (ts, duration) in enumerate(frames, start=1):
        payloads.append(
            {
                "schemaVersion": TIMELINE_SCHEMA,
                "seq": seq,
                "runId": run_id,
                "eventId": f"{run_id}-frame-{seq:03d}",
                "type": "counter",
                "name": FRAME_METRIC,
                # 后端自产 run 形态：tsMonotonicNs 承载 epoch（parse_samples_events
                # 语义），data.tsEpochMs 同值——read_frame_detail 投影后 ts_ms=epoch
                "tsMonotonicNs": int(ts) * 1_000_000,
                "source": {"platform": "android", "collector": "synthetic.fixture", "evidenceLevel": "L1"},
                "data": {"value": float(duration), "unit": "ms", "tsEpochMs": int(ts)},
            }
        )
    with path.open("w", encoding="utf-8") as sink:
        write_timeline(payloads, sink)


def _write_labels(run_dir: Path, labels: list[dict]) -> None:
    write_markers_sidecar(run_dir, labels)


def _make_fps_run(
    client: TestClient, case: str, points: list[tuple[int, float]], labels: list[dict]
) -> str:
    run_dir = client.runs_dir / case
    run_dir.mkdir(parents=True)
    _write_fps_samples(run_dir / "samples.ndjson", points)
    if labels:
        _write_labels(run_dir, labels)
    return f"run-{case}"


# ---- 主路径：切分 + 与内核对拍 + 段前空隙 ----


def test_by_label_segment_rows_split_and_match_kernel(client: TestClient) -> None:
    points = [(1000, 60.0), (2000, 55.0), (3000, 58.0), (4000, 40.0), (5000, 61.0), (6000, 45.0)]
    labels = [
        {"tsMs": 2000, "kind": "label", "name": "场景A", "color": "#FF5252"},
        {"tsMs": 4000, "kind": "label", "name": "场景B"},
    ]
    run_id = _make_fps_run(client, "PD_SEG_FPS", points, labels)

    plain = client.get(f"/api/runs/{run_id}/statistics").json()
    body = client.get(
        f"/api/runs/{run_id}/statistics", params={"byLabelSegment": "1"}
    ).json()

    # §11.1：顶层 body 仍为 §12 整段统计（分段表的 All 行），仅多出 labelSegments
    assert {k: v for k, v in body.items() if k != "labelSegments"} == plain
    segments = body["labelSegments"]
    assert [s["name"] for s in segments] == ["场景A", "场景B"]
    assert segments[0]["color"] == "#FF5252"
    assert "color" not in segments[1]
    # 段右端 = 下一 label tsMs - 1；末段右端 = 数据结束（6000）
    assert (segments[0]["fromMs"], segments[0]["toMs"]) == (2000, 3999)
    assert (segments[1]["fromMs"], segments[1]["toMs"]) == (4000, 6000)
    # 段内统计 = 同一内核对段内点列现算（不另算第二份）
    assert segments[0]["statistics"]["fps"] == kernel.fps_group([55.0, 58.0], 1000, [18, 25])
    assert segments[1]["statistics"]["fps"] == kernel.fps_group([40.0, 61.0, 45.0], 2000, [18, 25])
    assert segments[1]["statistics"]["durationMs"] == 2000
    # 段 statistics 为完整 §18 区间体（含回显与基线声明）
    assert segments[0]["statistics"]["fromMs"] == 2000
    assert segments[0]["statistics"]["toMs"] == 3999
    assert segments[0]["statistics"]["frameDataBasis"] == "fpsSeries"
    # 段前空隙：t=1000 点不属于任何段（两段 fps 组均不含 60.0）
    for segment in segments:
        assert segment["statistics"]["fps"]["min"] != 60.0


def test_by_label_segment_without_labels_is_empty_array(client: TestClient) -> None:
    points = [(1000, 60.0), (2000, 55.0)]
    run_id = _make_fps_run(client, "PD_SEG_NOLABEL", points, [])

    body = client.get(
        f"/api/runs/{run_id}/statistics", params={"byLabelSegment": "1"}
    ).json()
    assert body["labelSegments"] == []
    # 顶层 body 与不带参数时逐字段一致（无段可切，不伪造段）
    plain = client.get(f"/api/runs/{run_id}/statistics").json()
    assert {k: v for k, v in body.items() if k != "labelSegments"} == plain


# ---- 首尾截断 ----


def test_by_label_segment_clips_to_request_interval(client: TestClient) -> None:
    points = [(1000, 60.0), (2000, 55.0), (3000, 58.0), (4000, 40.0), (5000, 61.0), (6000, 45.0)]
    labels = [
        {"tsMs": 2000, "kind": "label", "name": "场景A"},
        {"tsMs": 4000, "kind": "label", "name": "场景B"},
    ]
    run_id = _make_fps_run(client, "PD_SEG_CLIP", points, labels)

    # fromMs=4500：场景A 与请求区间无交集 → 不出行；场景B 裁剪为 [4500, 6000]
    body = client.get(
        f"/api/runs/{run_id}/statistics",
        params={"byLabelSegment": "1", "fromMs": 4500},
    ).json()
    segments = body["labelSegments"]
    assert [s["name"] for s in segments] == ["场景B"]
    assert (segments[0]["fromMs"], segments[0]["toMs"]) == (4500, 6000)
    # durationMs = 段内点列首尾点差（6000-5000=1000，§12 规则 3），非段边界差
    assert segments[0]["statistics"]["fps"] == kernel.fps_group([61.0, 45.0], 1000, [18, 25])

    # toMs=3500：场景B 无交集 → 不出行；场景A 裁剪为 [2000, 3500]
    body = client.get(
        f"/api/runs/{run_id}/statistics",
        params={"byLabelSegment": "1", "toMs": 3500},
    ).json()
    segments = body["labelSegments"]
    assert [s["name"] for s in segments] == ["场景A"]
    assert (segments[0]["fromMs"], segments[0]["toMs"]) == (2000, 3500)
    assert segments[0]["statistics"]["fps"] == kernel.fps_group([55.0, 58.0], 1000, [18, 25])


def test_by_label_segment_last_segment_ends_at_data_end(client: TestClient) -> None:
    # 单 label 居中：末段右端 = 数据结束（5000），label 后的点全部入段
    points = [(1000, 60.0), (2000, 55.0), (3000, 58.0), (4000, 40.0), (5000, 61.0)]
    run_id = _make_fps_run(
        client, "PD_SEG_TAIL", points, [{"tsMs": 3000, "kind": "label", "name": "尾段"}]
    )
    body = client.get(
        f"/api/runs/{run_id}/statistics", params={"byLabelSegment": "1"}
    ).json()
    segments = body["labelSegments"]
    assert len(segments) == 1
    assert (segments[0]["fromMs"], segments[0]["toMs"]) == (3000, 5000)
    assert segments[0]["statistics"]["fps"] == kernel.fps_group([58.0, 40.0, 61.0], 2000, [18, 25])


def test_by_label_segment_without_data_reports_none_and_open_end(client: TestClient) -> None:
    # 有 label 无任何数据：末段 toMs=null（无数据不可知结束时刻，不伪造），段内 none 基线
    run_dir = client.runs_dir / "PD_SEG_NODATA"
    run_dir.mkdir(parents=True)
    _write_labels(run_dir, [{"tsMs": 2000, "kind": "label", "name": "空段"}])
    body = client.get(
        f"/api/runs/run-PD_SEG_NODATA/statistics", params={"byLabelSegment": "1"}
    ).json()
    segments = body["labelSegments"]
    assert len(segments) == 1
    assert segments[0]["name"] == "空段"
    assert segments[0]["fromMs"] == 2000
    assert segments[0]["toMs"] is None
    stats = segments[0]["statistics"]
    assert stats["frameDataBasis"] == "none"
    assert stats["durationMs"] == 0
    assert stats["fromMs"] == 2000
    assert "toMs" not in stats  # 开区间右端不回显（§18：未给出时不回显）


def test_by_label_segment_zero_width_segment_skipped(client: TestClient) -> None:
    # 同刻双 label：前段零宽 [2000, 1999] 不出行，后段从该刻起完整承接
    points = [(1000, 60.0), (2000, 55.0), (3000, 58.0)]
    labels = [
        {"tsMs": 2000, "kind": "label", "name": "瞬切段"},
        {"tsMs": 2000, "kind": "label", "name": "承接段"},
    ]
    run_id = _make_fps_run(client, "PD_SEG_ZERO", points, labels)
    body = client.get(
        f"/api/runs/{run_id}/statistics", params={"byLabelSegment": "1"}
    ).json()
    segments = body["labelSegments"]
    assert [s["name"] for s in segments] == ["承接段"]
    assert (segments[0]["fromMs"], segments[0]["toMs"]) == (2000, 3000)


# ---- frameEvents 基线（逐帧序列按段过滤；durationMs = 段内首尾帧差）----


def test_by_label_segment_frame_events_basis(client: TestClient) -> None:
    frames = [(1000, 8.0), (1016, 9.0), (2000, 40.0), (2016, 8.5), (3000, 8.2), (3500, 8.4)]
    labels = [
        {"tsMs": 2000, "kind": "label", "name": "帧段A"},
        {"tsMs": 3000, "kind": "label", "name": "帧段B"},
    ]
    run_dir = client.runs_dir / "PD_SEG_FRAMES"
    run_dir.mkdir(parents=True)
    _write_frame_timeline_epoch(run_dir / "timeline.ndjson", frames, "run-PD_SEG_FRAMES")
    _write_labels(run_dir, labels)
    run_id = "run-PD_SEG_FRAMES"

    body = client.get(
        f"/api/runs/{run_id}/statistics", params={"byLabelSegment": "1"}
    ).json()
    segments = body["labelSegments"]
    assert [s["name"] for s in segments] == ["帧段A", "帧段B"]
    assert (segments[0]["fromMs"], segments[0]["toMs"]) == (2000, 2999)
    assert (segments[1]["fromMs"], segments[1]["toMs"]) == (3000, 3500)  # 末帧 ceil 含端
    seg_a = segments[0]["statistics"]
    assert seg_a["frameDataBasis"] == "frameEvents"
    assert seg_a["durationMs"] == 16  # 段内首尾帧差（2016-2000）
    assert seg_a["frameTime"] == kernel.frame_time_group([40.0, 8.5], 16)
    assert seg_a["lowFps"] == {"p1": kernel.low_fps_p1([40.0, 8.5])}
    seg_b = segments[1]["statistics"]
    assert seg_b["frameTime"] == kernel.frame_time_group([8.2, 8.4], 500)
    # 段前帧（1000/1016）不入段
    assert seg_a["frameTime"]["avg"] != kernel.frame_time_group([8.0, 9.0], 16)["avg"]


# ---- 参数校验（§3 validation_error，details.field）----


def test_by_label_segment_param_validation(client: TestClient) -> None:
    run_id = _make_fps_run(
        client, "PD_SEG_VALIDATE", [(1000, 60.0)], [{"tsMs": 1000, "kind": "label", "name": "L"}]
    )
    url = f"/api/runs/{run_id}/statistics"
    resp = client.get(url, params={"byLabelSegment": "abc"})
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"]["field"] == "byLabelSegment"
    # 参数校验先于 404（与 §18 fromMs/toMs 同序）
    resp = client.get("/api/runs/run-PD_GHOST/statistics", params={"byLabelSegment": "xyz"})
    assert resp.status_code == 400
    # 0/false = 关：与不带参数同形（无 labelSegments 键）
    for off in ("0", "false", ""):
        body = client.get(url, params={"byLabelSegment": off}).json()
        assert "labelSegments" not in body


# ---- sidecar 亲和：落盘行（无 id）即可切分；坏行跳过不致 500 ----


def test_by_label_segment_reads_sidecar_lines_directly(client: TestClient) -> None:
    run_dir = client.runs_dir / "PD_SEG_SIDECAR"
    run_dir.mkdir(parents=True)
    _write_fps_samples(run_dir / "samples.ndjson", [(1000, 60.0), (2000, 55.0)])
    sidecar_lines = [
        json.dumps({"tsMs": 1000, "kind": "note", "text": "不切分"}, ensure_ascii=False),
        json.dumps({"tsMs": 1000, "kind": "label", "name": "行读段"}, ensure_ascii=False),
        "{broken-json",
        json.dumps({"tsMs": 1, "kind": "unknown-kind"}, ensure_ascii=False),
    ]
    (run_dir / "markers.ndjson").write_text("\n".join(sidecar_lines) + "\n", encoding="utf-8")
    body = client.get(
        f"/api/runs/run-PD_SEG_SIDECAR/statistics", params={"byLabelSegment": "1"}
    ).json()
    segments = body["labelSegments"]
    assert [s["name"] for s in segments] == ["行读段"]
    assert (segments[0]["fromMs"], segments[0]["toMs"]) == (1000, 2000)
