"""Excel 导出测试（契约 §13.1/§13.2，DEVELOPMENT_PLAN R39/R40）：openpyxl 回读断言。

覆盖：工作表名与顺序（[I§2.5] S2-img7 可证实的 4 表）、设备信息块（manifest 实有
字段）、Sum 行与统计端点同值（单一事实源，契约 §13.1）、逐秒行口径（该秒内最后
采样点 / 缺秒指标空单元格 / Label-Notes 自 markers.ndjson，§15.4）、FrameInfo 明细
与内核同源、save(exportExcel) 成功/缺省/失败降级/参数校验、404 与 RFC 6266 文件名。
"""

from __future__ import annotations

import time
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.stats import generate_frame_events
from conftest import make_session

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SHEET_NAMES = ["all", "FrameInfo", "FrameallCpuLoggetdata", "Latency"]

FPS = "frame.fps.mean"
FRAME_DETAIL_METRIC_ID = "frame.duration.detail.ms"
TIMELINE_SCHEMA = "perfeval.timeline.v1"


# ---- 夹具工具 ----


def _write_run_manifest(run_dir: Path, run_id: str, metrics: list[dict[str, Any]]) -> None:
    import json

    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": "perfeval.manifest.v1",
                "runId": run_id,
                "caseName": run_dir.name,
                "sessionId": "sess-01",
                "device": "synthetic-001",
                "packageName": "com.example.synthetic.game",
                "metrics": metrics,
                "createdAt": 1798765432101,
                "savedAt": 1798765432102,
            }
        ),
        encoding="utf-8",
    )


def _write_samples(path: Path, rows: list[tuple[int, str, float]]) -> None:
    from perfeval.collectors.android.samplers.base import SampleRecord
    from perfeval.collectors.android.samples import SamplesWriter

    writer = SamplesWriter(path)
    try:
        writer.append(
            [SampleRecord(metric_id=metric_id, value=value, ts_ms=ts) for ts, metric_id, value in rows]
        )
    finally:
        writer.close()


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
                    "jank": False,  # perfeval 口径标记；Excel jank 列用统计内核判定
                    "clockDomain": "frame_relative",
                    "fpsSource": "framestats",
                },
            }
        )
    with path.open("w", encoding="utf-8") as sink:
        write_timeline(payloads, sink)


# ---- 回读定位工具 ----


def _rows(sheet: Any) -> list[list[Any]]:
    return [[cell.value for cell in row] for row in sheet.iter_rows()]


def _device_block(sheet: Any) -> dict[str, Any]:
    """设备信息块：自首行向下取 A 列非空标签直到 Sum 区（契约 §13.1 首部块）。"""
    block: dict[str, Any] = {}
    for row in _rows(sheet):
        label, value = row[0], row[1] if len(row) > 1 else None
        if label in ("Sum", "time"):  # 撞到 Sum 区 / 逐秒网格表头即止
            break
        if isinstance(label, str) and label:
            block[label] = value
    return block


def _sum_map(sheet: Any) -> dict[str, Any]:
    """Sum 标签行 + 数值行（A 列均为 "Sum" 的两行）→ {标签: 值}。"""
    sum_rows = [row for row in _rows(sheet) if row and row[0] == "Sum"]
    assert len(sum_rows) == 2, f"Sum 行应为标签+数值两行，实得 {len(sum_rows)}"
    labels = [value for value in sum_rows[0][1:] if value is not None]
    values = sum_rows[1][1 : 1 + len(labels)]
    return dict(zip(labels, values))


def _grid_header_row(sheet: Any) -> tuple[int, list[Any]]:
    """逐秒网格表头行（A 列 == "time"）→ (0-based 行号, 去尾部 None 的行值)。

    openpyxl iter_rows 会把每行补齐到工作表 max_col（Sum 标签行更宽），需裁掉。"""
    for index, row in enumerate(_rows(sheet)):
        if row and row[0] == "time":
            trimmed = list(row)
            while trimmed and trimmed[-1] is None:
                trimmed.pop()
            return index, trimmed
    raise AssertionError("未找到逐秒网格表头行（time/absTime/Label/Notes…）")


# ---- 工作簿结构（契约 §13.1）----


def test_workbook_sheet_names_and_device_block(client: TestClient) -> None:
    """4 工作表（R40 写 5 表但第 5 表无截图证据，契约 §13.1 如实声明只交付 4 表）+
    设备信息块只写 manifest 实有字段。"""
    run_dir = client.runs_dir / "PD_EXCEL_GRID"
    run_dir.mkdir(parents=True)
    _write_run_manifest(
        run_dir,
        "run-PD_EXCEL_GRID",
        [
            {"metricId": FPS, "intervalMs": 500},
            {"metricId": "cpu.total.pct", "intervalMs": 1000},
            {"metricId": "net.rx.kbps", "intervalMs": 1000},  # 声明但零采样 → 空列
        ],
    )
    _write_samples(
        run_dir / "samples.ndjson",
        [
            (0, FPS, 60.0), (500, FPS, 59.0), (800, FPS, 61.0),
            (1000, FPS, 58.0), (1200, FPS, 57.0), (2000, FPS, 55.0),
            (0, "cpu.total.pct", 20.5), (1000, "cpu.total.pct", 21.5),
            (2500, "cpu.total.pct", 22.5),
            (500, "custom.metric.x", 1.5),  # 未注册指标：列追加在注册指标之后
        ],
    )
    payload, filename = _export_bytes(client, "run-PD_EXCEL_GRID")
    assert filename == "PD_EXCEL_GRID.xlsx"
    workbook = load_workbook(BytesIO(payload))
    assert workbook.sheetnames == SHEET_NAMES

    block = _device_block(workbook["all"])
    assert block["Device"] == "synthetic-001"
    assert block["Package"] == "com.example.synthetic.game"
    assert block["CaseName"] == "PD_EXCEL_GRID"
    assert block["RunId"] == "run-PD_EXCEL_GRID"
    # OS/CPU/GPU/Resolution/Ram：manifest 未承载设备详情（ARCH §4.5 不变），不写不编造
    for absent in ("OS", "CPU", "GPU", "Resolution", "Ram"):
        assert absent not in block


def test_all_sheet_sum_row_matches_statistics_endpoint(client: TestClient) -> None:
    """Sum 行 == GET /api/runs/{runId}/statistics（单一事实源，契约 §13.1）。"""
    run_dir = client.runs_dir / "PD_SUM_SRC"
    run_dir.mkdir(parents=True)
    _write_run_manifest(run_dir, "run-PD_SUM_SRC", [{"metricId": FPS, "intervalMs": 500}])
    _write_samples(
        run_dir / "samples.ndjson",
        [(i * 500, FPS, 60.0 + (i % 4) - 1.5) for i in range(9)],
    )
    workbook = _load_export(client, "run-PD_SUM_SRC")
    stats = client.get("/api/runs/run-PD_SUM_SRC/statistics").json()

    sum_map = _sum_map(workbook["all"])
    assert list(sum_map)[:7] == [
        "Avg(FPS)", "Var(FPS)", "Std(FPS)", "Drop(FPS)",
        "Min(FPS)", "Median(FPS)", "MedRange(FPS)",
    ]
    assert list(sum_map)[-1] == "Duration(ms)"
    assert sum_map["Avg(FPS)"] == stats["fps"]["avg"]
    assert sum_map["Var(FPS)"] == stats["fps"]["var"]
    assert sum_map["Std(FPS)"] == stats["fps"]["std"]
    assert sum_map["Drop(FPS)"] == stats["fps"]["drop"]
    assert sum_map["Median(FPS)"] == stats["fps"]["median"]
    assert sum_map["FPS>=18(%)"] == stats["fps"]["ge"]["18"]
    assert sum_map["FPS>=25(%)"] == stats["fps"]["ge"]["25"]
    assert sum_map["Duration(ms)"] == stats["durationMs"]
    # fpsSeries 基线：逐帧组不可计算 → 空单元格（不填 0），与端点 null 同值
    for label, group, key in (
        ("Avg(FTime)", "frameTime", "avg"),
        ("Jank", "jank", "jank"),
        ("Jank(/10min)", "jank", "per10min"),
        ("Smooth", "smooth", "value"),
        ("1%Low(FPS)", "lowFps", "p1"),
    ):
        assert sum_map[label] == stats[group][key]


def test_all_sheet_per_second_grid_semantics(client: TestClient) -> None:
    """逐秒行：该秒内各指标最后一个采样点；缺秒指标空单元格；无 markers.ndjson 时
    Label/Notes 空串（§15.4 无标记是常态）；整秒无采样的行不造。"""
    run_dir = client.runs_dir / "PD_EXCEL_GRID"
    run_dir.mkdir(parents=True)
    _write_run_manifest(
        run_dir,
        "run-PD_EXCEL_GRID",
        [
            {"metricId": FPS, "intervalMs": 500},
            {"metricId": "cpu.total.pct", "intervalMs": 1000},
            {"metricId": "net.rx.kbps", "intervalMs": 1000},
        ],
    )
    _write_samples(
        run_dir / "samples.ndjson",
        [
            (0, FPS, 60.0), (500, FPS, 59.0), (800, FPS, 61.0),
            (1000, FPS, 58.0), (1200, FPS, 57.0), (2000, FPS, 55.0),
            (0, "cpu.total.pct", 20.5), (1000, "cpu.total.pct", 21.5),
            (2500, "cpu.total.pct", 22.5),
            (500, "custom.metric.x", 1.5),
        ],
    )
    workbook = _load_export(client, "run-PD_EXCEL_GRID")
    sheet = workbook["all"]
    header_index, header = _grid_header_row(sheet)
    # 列 = manifest 声明集 ∪ 实际采样集；注册表序在前、未注册按名追加在尾
    assert header == [
        "time", "absTime", "Label", "Notes",
        "cpu.total.pct", "net.rx.kbps", FPS, "custom.metric.x",
    ]
    width = len(header)
    data = [
        row[:width]
        for row in _rows(sheet)[header_index + 1 :]
        if any(v is not None for v in row[:width])
    ]
    # 注：openpyxl 把空串单元格序列化为空单元格（读回 None），且整数值浮点读回 int
    # （61.0 → 61，== 语义等价）——断言的是「空/同值」而非单元格类型。
    assert data == [
        [0, 0, None, None, 20.5, None, 61.0, 1.5],  # 秒 0 [0,1000)：fps 最后=800ms 点，cpu 20.5
        [1, 1000, None, None, 21.5, None, 57.0, None],  # 秒 1 [1000,2000)：fps 57.0（1200ms），cpu 21.5
        [2, 2000, None, None, 22.5, None, 55.0, None],  # 秒 2 [2000,3000)：fps 55.0（恰在秒界，floor 归后秒），cpu 22.5
    ]
    # 第 3 秒（2500ms 落在秒 2）之后无采样 → 无第 3 秒行（整秒无采样不造行）
    assert all(row[0] != 3 for row in data)
    for row in data:
        # 本 run 无 markers.ndjson → Label/Notes 空串（契约 §15.4 无标记是常态）
        # ——写 ""，openpyxl 读回空单元格
        assert row[2] in ("", None) and row[3] in ("", None)


def _write_markers_sidecar(run_dir: Path, markers: list[dict[str, Any]]) -> None:
    """按 §15.1 落盘形状写 markers.ndjson（marker_service.write_markers_sidecar 同款：
    元素无 id、json.dumps sort_keys、坏行容错归读取侧）。"""
    import json

    with (run_dir / "markers.ndjson").open("w", encoding="utf-8") as sink:
        for marker in markers:
            sink.write(json.dumps(marker, ensure_ascii=False, sort_keys=True) + "\n")


def test_all_sheet_label_notes_from_markers_sidecar(client: TestClient) -> None:
    """契约 §15.4：Label/Notes 自 markers.ndjson 接管——Label 段内逐行填充（段 =
    label tsMs 所在秒起至下一个 label 或数据结束）、Notes 该秒内多 note 以 "; "
    连接、mark 不导出、段边界秒正确换段。"""
    run_dir = client.runs_dir / "PD_MARKERS_EXPORT"
    run_dir.mkdir(parents=True)
    _write_run_manifest(run_dir, "run-PD_MARKERS_EXPORT", [{"metricId": FPS, "intervalMs": 500}])
    _write_samples(
        run_dir / "samples.ndjson",
        [(i * 500, FPS, 60.0 - i) for i in range(6)],  # 0..2500ms → 秒桶 0/1/2
    )
    _write_markers_sidecar(
        run_dir,
        [
            {"tsMs": 0, "kind": "label", "name": "登录场景", "color": "#FF5252"},  # 段起点 = 秒 0
            {"tsMs": 500, "kind": "note", "text": "冷启动"},
            {"tsMs": 800, "kind": "note", "text": "加载卡顿"},  # 同秒双 note → "; " 连接
            {"tsMs": 1200, "kind": "mark"},  # mark 不导出（§15.4，不臆测 Mark 列）
            {"tsMs": 2000, "kind": "label", "name": "战斗场景"},  # 段起点 = 秒 2（恰在秒界）
            {"tsMs": 2100, "kind": "note", "text": "掉帧"},
        ],
    )

    workbook = _load_export(client, "run-PD_MARKERS_EXPORT")
    sheet = workbook["all"]
    header_index, header = _grid_header_row(sheet)
    assert header[:4] == ["time", "absTime", "Label", "Notes"]
    width = len(header)
    data = [
        row[:width]
        for row in _rows(sheet)[header_index + 1 :]
        if any(v is not None for v in row[:width])
    ]
    assert data == [
        # 秒 0 [0,1000)：label「登录场景」；双 note 连接；fps 最后采样 = 500ms → 59.0
        [0, 0, "登录场景", "冷启动; 加载卡顿", 59.0],
        [1, 1000, "登录场景", None, 57.0],  # 段内逐行填充；秒 1 无 note
        [2, 2000, "战斗场景", "掉帧", 55.0],  # tsMs=2000 恰在秒界归秒 2 → 新段
    ]


def test_all_sheet_label_segment_before_first_sample(client: TestClient) -> None:
    """契约 §15.4：label tsMs 早于数据首秒 → 段起点为负秒，自首行起覆盖；
    note 落在无行秒（无采样）不出现；无适用 note 的秒为空。"""
    run_dir = client.runs_dir / "PD_MARKERS_EARLY"
    run_dir.mkdir(parents=True)
    _write_run_manifest(run_dir, "run-PD_MARKERS_EARLY", [{"metricId": FPS, "intervalMs": 500}])
    _write_samples(
        run_dir / "samples.ndjson",
        [(500, FPS, 60.0), (1000, FPS, 59.0), (1500, FPS, 58.0)],  # origin=500 → 桶 0/0/1
    )
    _write_markers_sidecar(
        run_dir,
        [
            {"tsMs": 100, "kind": "label", "name": "开局"},  # (100-500)//1000 = -1 → 覆盖全部行
            {"tsMs": 400, "kind": "note", "text": "早于首秒"},  # 落在无行秒 [0,500) → 不出现
            {"tsMs": 1500, "kind": "note", "text": "晚到note"},  # 秒 1 [1500,2500)
        ],
    )

    workbook = _load_export(client, "run-PD_MARKERS_EARLY")
    sheet = workbook["all"]
    header_index, _ = _grid_header_row(sheet)
    width = 5  # time/absTime/Label/Notes + FPS 一列
    data = [
        row[:width]
        for row in _rows(sheet)[header_index + 1 :]
        if any(v is not None for v in row[:width])
    ]
    assert data == [
        [0, 500, "开局", None, 59.0],  # fps 最后采样 = 1000ms → 59.0；早于首秒的 note 不落
        [1, 1500, "开局", "晚到note", 58.0],
    ]


def test_frameinfo_sheet_detail_and_kernel_jank(client: TestClient) -> None:
    """FrameInfo：frameIndex/tsEpochMs/durationMs/jank 四列；jank 列与 Sum 行 Jank
    同一内核判定（单源）；frame_relative 事件无 epoch → tsEpochMs 留空不编造。"""
    run_dir = client.runs_dir / "PD_FRAME_EXPORT"
    run_dir.mkdir(parents=True)
    events = generate_frame_events("excel-frameinfo", 230)  # 序列含 Jank 尖刺（i%211==210）
    _write_frame_timeline(run_dir / "timeline.ndjson", events, "run-PD_FRAME_EXPORT")

    workbook = _load_export(client, "run-PD_FRAME_EXPORT")
    sheet = workbook["FrameInfo"]
    rows = _rows(sheet)
    assert rows[0][:4] == ["frameIndex", "tsEpochMs", "durationMs", "jank"]
    data = [row for row in rows[1:] if any(v is not None for v in row)]
    assert len(data) == len(events)
    assert [row[0] for row in data] == list(range(len(events)))
    assert [row[1] for row in data] == [None] * len(events)  # fixture 无 tsEpochMs → 全空
    assert [row[2] for row in data] == [event.duration_ms for event in events]

    stats = client.get("/api/runs/run-PD_FRAME_EXPORT/statistics").json()
    assert stats["frameDataBasis"] == "frameEvents"
    assert stats["jank"]["jank"] >= 1  # 序列设计含 Jank 尖刺，判定路径真实走到
    assert sum(bool(row[3]) for row in data) == stats["jank"]["jank"]
    assert _sum_map(workbook["all"])["Jank"] == stats["jank"]["jank"]  # 跨表同源


def test_frameinfo_header_only_without_frames(client: TestClient) -> None:
    run_dir = client.runs_dir / "PD_NO_FRAMES"
    run_dir.mkdir(parents=True)
    _write_run_manifest(run_dir, "run-PD_NO_FRAMES", [{"metricId": FPS, "intervalMs": 500}])
    _write_samples(run_dir / "samples.ndjson", [(0, FPS, 60.0), (500, FPS, 59.0)])
    workbook = _load_export(client, "run-PD_NO_FRAMES")
    sheet = workbook["FrameInfo"]
    assert _rows(sheet)[0][:4] == ["frameIndex", "tsEpochMs", "durationMs", "jank"]
    assert sheet.max_row == 1  # 无逐帧数据仅写表头（契约 §13.1）


def test_none_baseline_run_exports_empty_cells(client: TestClient) -> None:
    """无任何数据的 run：Sum 行全空单元格（不填 0，[R§8.6]）、网格仅表头。"""
    (client.runs_dir / "PD_EMPTY_EXPORT").mkdir(parents=True)
    workbook = _load_export(client, "run-PD_EMPTY_EXPORT")
    sum_map = _sum_map(workbook["all"])
    assert sum_map["Avg(FPS)"] is None
    assert sum_map["Jank"] is None
    assert sum_map["Duration(ms)"] == 0  # durationMs=0 是统计层 none 基线声明值
    _, header = _grid_header_row(workbook["all"])
    assert header == ["time", "absTime", "Label", "Notes"]  # 无指标列、无数据行


# ---- §13.1 端点行为 ----


def _export_bytes(client: TestClient, run_id: str) -> tuple[bytes, str]:
    resp = client.get(f"/api/runs/{run_id}/export.xlsx")
    assert resp.status_code == 200, resp.text
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith("attachment; filename=\"")
    filename = unquote(disposition.split('filename="', 1)[1].split('"', 1)[0])
    return resp.content, filename


def _load_export(client: TestClient, run_id: str):
    payload, _ = _export_bytes(client, run_id)
    return load_workbook(BytesIO(payload))


def test_export_endpoint_headers_and_404(client: TestClient) -> None:
    sid = make_session(client)["id"]
    time.sleep(0.4)
    saved = client.post(
        f"/api/sessions/{sid}/save", json={"caseName": "PD_EXPORT_ENDPOINT"}
    ).json()
    resp = client.get(f"/api/runs/{saved['runId']}/export.xlsx")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == XLSX_MEDIA_TYPE
    assert resp.headers["content-disposition"] == 'attachment; filename="PD_EXPORT_ENDPOINT.xlsx"'
    workbook = load_workbook(BytesIO(resp.content))
    assert workbook.sheetnames == SHEET_NAMES

    error = client.get("/api/runs/run-NO_SUCH_RUN/export.xlsx")
    assert error.status_code == 404
    assert error.json()["error"]["code"] == "report_not_found"
    # 路径穿越防护与 §12 统计端点同款
    for run_id in ("run-..", "run-", "run-%2E%2E"):
        resp = client.get(f"/api/runs/{run_id}/export.xlsx")
        assert resp.status_code == 404, run_id
        assert resp.json()["error"]["code"] == "report_not_found", run_id


def test_export_filename_non_ascii_rfc6266(client: TestClient) -> None:
    """非 ASCII 案例名：filename= 为 ASCII 兜底 + filename*=UTF-8''（契约 §13.1）。"""
    run_dir = client.runs_dir / "PD_中文回归"
    run_dir.mkdir(parents=True)
    _write_run_manifest(run_dir, "run-PD_中文回归", [])
    resp = client.get("/api/runs/run-PD_中文回归/export.xlsx")
    assert resp.status_code == 200
    disposition = resp.headers["content-disposition"]
    assert "filename*=UTF-8''" in disposition
    assert unquote(disposition.split("filename*=UTF-8''", 1)[1]) == "PD_中文回归.xlsx"
    assert disposition.split(";")[0].isascii()  # filename= 兜底为 ASCII


# ---- §13.2 save 导出扩展 ----


def test_save_export_excel_true_writes_xlsx(client: TestClient) -> None:
    sid = make_session(client, metrics=[{"metricId": FPS, "intervalMs": 200}])["id"]
    time.sleep(1.1)
    resp = client.post(
        f"/api/sessions/{sid}/save",
        json={"caseName": "PD_EXPORT_FLOW", "exportExcel": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "exportError" not in body  # exportPath/exportError 互斥（契约 §13.2）
    export_path = Path(body["exportPath"])
    assert export_path == client.runs_dir / "PD_EXPORT_FLOW.xlsx"
    assert export_path.is_file()
    workbook = load_workbook(export_path)
    assert workbook.sheetnames == SHEET_NAMES
    # 与 §13.1 重导出同一 writer：Sum 行存在且 Avg(FPS) 与统计端点同值
    stats = client.get("/api/runs/run-PD_EXPORT_FLOW/statistics").json()
    assert _sum_map(workbook["all"])["Avg(FPS)"] == stats["fps"]["avg"]


def test_save_without_export_excel_has_no_export_fields(client: TestClient) -> None:
    for payload in ({}, {"exportExcel": False}):
        sid = make_session(client)["id"]
        time.sleep(0.2)
        body = client.post(f"/api/sessions/{sid}/save", json=payload).json()
        assert "exportPath" not in body  # 仅生成成功时出现（契约 §13.2）
        assert "exportError" not in body


def test_save_export_excel_type_validation(client: TestClient) -> None:
    sid = make_session(client)["id"]
    resp = client.post(f"/api/sessions/{sid}/save", json={"exportExcel": "yes"})
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"]["field"] == "exportExcel"


def test_save_export_excel_failure_degrades_to_export_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(runs_dir: Path, run_id: str) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr("app.services.excel_export.write_run_export_xlsx", _boom)
    sid = make_session(client)["id"]
    time.sleep(0.2)
    resp = client.post(
        f"/api/sessions/{sid}/save", json={"caseName": "PD_EXPORT_FAIL", "exportExcel": True}
    )
    assert resp.status_code == 200  # 导出失败不使 save 失败（契约 §13.2）
    body = resp.json()
    assert "exportPath" not in body
    assert "excel 导出失败" in body["exportError"]
    assert "disk full" in body["exportError"]
    assert Path(body["runPath"]).is_dir()  # run 目录已落盘即 save 成功
    # 派生产物可经 §13.1 重试
    assert client.get(f"/api/runs/{body['runId']}/export.xlsx").status_code == 200
