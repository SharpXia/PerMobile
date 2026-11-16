"""M7 deep-analysis 端点测试（契约 §22.1，R50）。

fixture 说明（如实标注）：perfeval golden 47KB frame-timeline.pftrace 原样字节作
run 工件；本机无真 trace_processor 二进制——SQL 执行以假二进制回放 canned CSV
驱动（perfeval tests/test_perfetto_trace.py 同款零真二进制模式），FrameTimeline
解析/换算/装配全部走生产代码（perfeval ``query_frame_timeline`` + ``parse_frame_
timeline``）。真 trace_processor 对真实 trace 的查询未实测。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from m7_fixtures import (  # noqa: E402
    FRAME1_WINDOW_END_NS,
    FRAME1_WINDOW_START_NS,
    GOLDEN_TRACE_PATH,
    build_fake_trace_processor,
    last_sql,
    make_trace_run,
)


@pytest.fixture()
def trace_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """假 trace_processor 注入进程环境（发现链第 1 级 env 命中）。"""
    fake_dir = tmp_path / "tp"
    binary = build_fake_trace_processor(fake_dir)
    monkeypatch.setenv("PERFEVAL_TRACE_PROCESSOR", str(binary))
    return fake_dir


def _frame_keys() -> set[str]:
    from perfeval.collectors.android.perfetto_trace import FrameRecord

    return set(FrameRecord(0, 0, None, 0.0, False).to_dict())


# ---- 帧列表概览 ----


def test_deep_analysis_lists_frames_from_fixture(client, trace_env) -> None:
    run_id = make_trace_run(client)
    resp = client.get(f"/api/runs/{run_id}/deep-analysis")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["runId"] == run_id
    assert body["traceAvailable"] is True
    assert body["source"] == "perfetto.frame_timeline"
    assert body["vsyncPeriodMs"] == 8.333  # Expected 时长去重中位数（parse_frame_timeline）
    assert body["frameCount"] == 3 and body["frameCount"] == len(body["frames"])
    assert body["jankCount"] == 2  # App Deadline Missed + SurfaceFlinger Stuffing
    assert body["jankRate"] == round(2 / 3, 3)
    assert body["note"] is None
    keys = _frame_keys()  # FrameRecord.to_dict 键集原样（不加壳不改名）
    assert keys >= {
        "frameIndex", "tsEpochMs", "durationMs", "jank", "jankType", "presentType",
        "onTimeFinish", "expectedDurationMs", "processName", "pid",
    }
    for frame in body["frames"]:
        assert set(frame) == keys
    frames = body["frames"]
    assert frames[0]["tsEpochMs"] == 1_798_765_430_000  # clock 对 BOOTTIME→REALTIME 换算
    assert frames[0]["durationMs"] == 8.9 and frames[0]["jank"] is False
    assert frames[1]["tsEpochMs"] == 1_798_765_430_700
    assert frames[1]["jank"] is True
    assert frames[1]["jankType"] == "App Deadline Missed"
    assert frames[1]["onTimeFinish"] is False
    assert frames[1]["expectedDurationMs"] == 8.333
    assert frames[1]["processName"] == "com.example.game" and frames[1]["pid"] == 12345
    assert frames[2]["jankType"] == "SurfaceFlinger Stuffing"


# ---- 单帧 Overview 卡 + CPU 调度片段 ----


def test_deep_analysis_frame_detail_with_cpu_scheduling(client, trace_env) -> None:
    run_id = make_trace_run(client)
    resp = client.get(f"/api/runs/{run_id}/deep-analysis?frameIndex=1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["runId"] == run_id and body["traceAvailable"] is True
    # 窗口构造：SQL WHERE 边界 = 帧1 [ts, ts+duration]（boottime ns）——先读回
    # fake 二进制转存的调度 SQL（后续列表请求的 clock 查询会覆盖转存文件）
    sql = last_sql(trace_env)
    assert "thread_state" in sql
    assert f">= {FRAME1_WINDOW_START_NS}" in sql
    assert f"< {FRAME1_WINDOW_END_NS}" in sql
    listing = client.get(f"/api/runs/{run_id}/deep-analysis").json()
    assert body["frame"] == listing["frames"][1]  # 单帧卡与列表同源
    slices = body["cpuScheduling"]
    assert len(slices) == 3
    running = slices[0]
    assert running == {
        "core": 4,
        "tid": 24271,
        "threadName": "GameThread",
        "pid": 12345,
        "state": "Running",  # thread_state on-CPU 原样
        "startEpochMs": 1_798_765_430_702,  # §1 epoch ms 口径（clock 对换算）
        "durationMs": 12.4,
    }
    assert slices[1]["state"] == "Runnable" and slices[1]["tid"] == 24272  # R → Runnable
    assert slices[2]["state"] == "Sleeping"  # S → Sleeping
    for slice_ in slices:
        assert isinstance(slice_["startEpochMs"], int)
        assert slice_["durationMs"] > 0
    assert body["note"] is None


# ---- 空结构 / 错误路径 ----


def test_deep_analysis_without_trace_processor_returns_empty_with_note(
    client, tmp_path, monkeypatch
) -> None:
    """trace 工件在但本机无 trace_processor → 空结构 + note（不报错不伪造）。"""
    from perfeval.collectors.android.perfetto_trace import TraceProcessorResolution

    run_id = make_trace_run(client)
    # hermetic：屏蔽 env 并强制发现链不可用（防开发机 PATH 上存在真二进制）
    monkeypatch.delenv("PERFEVAL_TRACE_PROCESSOR", raising=False)
    monkeypatch.setattr(
        "perfeval.collectors.android.perfetto_trace.resolve_trace_processor",
        lambda *a, **k: TraceProcessorResolution(
            None, None, (("env", "未设置"), ("path", "未找到"))
        ),
    )
    resp = client.get(f"/api/runs/{run_id}/deep-analysis")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["traceAvailable"] is False
    assert body["source"] is None and body["frames"] == []
    assert body["frameCount"] == 0 and body["jankCount"] == 0 and body["jankRate"] is None
    assert isinstance(body["note"], str) and "不回退伪造" in body["note"]


def test_deep_analysis_run_without_trace_dir(client) -> None:
    (client.runs_dir / "PD_EMPTY").mkdir(parents=True)
    resp = client.get("/api/runs/run-PD_EMPTY/deep-analysis")
    assert resp.status_code == 200
    body = resp.json()
    assert body["traceAvailable"] is False and body["frames"] == []
    assert "不回退伪造" in body["note"]


def test_deep_analysis_trace_processor_unavailable_is_empty(
    client, tmp_path, monkeypatch
) -> None:
    """trace 工件在但 trace_processor 不可用 → 同一空结构 note（契约 §22.1）。"""
    from perfeval.collectors.android.perfetto_trace import TraceProcessorResolution

    make_trace_run(client)
    monkeypatch.setattr(
        "perfeval.collectors.android.perfetto_trace.resolve_trace_processor",
        lambda *a, **k: TraceProcessorResolution(
            None, None, (("env", "未设置"), ("path", "未找到"))
        ),
    )
    resp = client.get("/api/runs/run-PD_M7_TRACE/deep-analysis")
    assert resp.status_code == 200
    body = resp.json()
    assert body["traceAvailable"] is False and body["frames"] == []
    assert "trace_processor 不可用" in body["note"]


def test_deep_analysis_frame_index_out_of_range(client, trace_env) -> None:
    run_id = make_trace_run(client)
    resp = client.get(f"/api/runs/{run_id}/deep-analysis?frameIndex=99")
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"]["field"] == "frameIndex"
    resp = client.get(f"/api/runs/{run_id}/deep-analysis?frameIndex=abc")
    assert resp.status_code == 400
    assert resp.json()["error"]["details"]["field"] == "frameIndex"


def test_deep_analysis_run_not_found(client, trace_env) -> None:
    resp = client.get("/api/runs/run-PD_NOWHERE/deep-analysis")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "report_not_found"


# ---- simpleperf 火焰图数据面（M8+ 缺口 flamegraph-compare-missing，§22 扩展）----
# fixture 说明（如实标注）：synthetic simpleperf report 文本按 simpleperf report
# 实测输出格式构造（emulator-5554 simpleperf record/report 实测，2026-11-10 抓取
# flat 表与 -g 简报调用树两种真实格式；数值为脱敏合成，手算口径见断言）。

SIMPLEPERF_FLAT_REPORT = (
    "Cmdline: simpleperf record -e cpu-clock -f 4000 -g --duration 5 --app com.example.game\n"
    "Arch: arm64\n"
    "Event: cpu-clock (type 1, config 0)\n"
    "Samples: 4\n"
    "Event count: 8000000\n"
    "\n"
    "Overhead  Command  Pid    Tid    Shared Object               Symbol\n"
    "50.00%    com.game  1234  1234  libgame.so                  GameThread::Update\n"
    "30.00%    com.game  1234  1234  libgame.so                  RenderScene\n"
    "20.00%    com.game  1234  1234  [kernel.kallsyms]           freeze_slab\n"
)

SIMPLEPERF_CALLGRAPH_REPORT = (
    "Cmdline: simpleperf record -e cpu-clock -f 4000 -g --duration 5 --app com.example.game\n"
    "Arch: arm64\n"
    "Event: cpu-clock (type 1, config 0)\n"
    "Samples: 4\n"
    "Event count: 8000000\n"
    "\n"
    "Children  Self    Command  Pid    Tid    Shared Object        Symbol\n"
    "100.00%   0.00%   com.game  1234  1234  libgame.so           GameThread::Update\n"
    "       |\n"
    "       -- GameThread::Update\n"
    "          |\n"
    "           -- RenderScene\n"
    "               |\n"
    "               |--60.00%-- BuildDrawCalls\n"
    "               |    |\n"
    "               |     --40.00%-- SubmitCmds\n"
    "               |\n"
    "               |--30.00%-- SortDrawCalls\n"
    "               |           SortQueue\n"
)


def _make_trace_run_with_simpleperf(client, report_text: str, case_name: str = "PD_M7_TRACE") -> str:
    run_path = client.runs_dir / case_name
    artifacts = run_path / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "simpleperf-report.txt").write_text(report_text, encoding="utf-8")
    import shutil

    shutil.copyfile(GOLDEN_TRACE_PATH, artifacts / "trace.pftrace")
    return f"run-{case_name}"


def test_deep_analysis_overview_includes_simpleperf_flamegraph_face(
    client, trace_env
) -> None:
    """帧列表概览带 simpleperf 数据面：stat 卡 + flat overhead 分布树（手算口径）。"""
    run_id = _make_trace_run_with_simpleperf(client, SIMPLEPERF_FLAT_REPORT)
    resp = client.get(f"/api/runs/{run_id}/deep-analysis")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    face = body["simpleperf"]
    assert face["available"] is True
    assert face["source"] == "artifacts/simpleperf-report.txt"
    assert face["note"] is None
    stat = face["stat"]
    assert stat["arch"] == "arm64"
    assert stat["events"] == [{"event": "cpu-clock", "samples": 4, "eventCount": 8000000}]
    assert stat["mode"] == "flat"
    assert stat["entryCount"] == 3 and stat["maxDepth"] == 4  # ALL→command→dso→symbol
    assert stat["unparsedLines"] == 0
    tree = face["flamegraph"]["tree"]
    assert tree["name"] == "ALL" and tree["value"] == 100.0
    (command,) = tree["children"]
    assert command["name"] == "com.game" and command["value"] == 100.0
    dso_by_name = {child["name"]: child for child in command["children"]}
    assert dso_by_name["libgame.so"]["value"] == 80.0
    symbols = {c["name"]: c["value"] for c in dso_by_name["libgame.so"]["children"]}
    assert symbols == {"GameThread::Update": 50.0, "RenderScene": 30.0}
    assert dso_by_name["[kernel.kallsyms]"]["value"] == 20.0
    assert face["flamegraph"]["basis"].startswith("flat:")


def test_deep_analysis_overview_callgraph_report_builds_stack_tree(
    client, trace_env
) -> None:
    """-g 简报调用树：分支按缩进成树、链上延续帧挂 value=0 叶（首 entry 成树）。"""
    run_id = _make_trace_run_with_simpleperf(client, SIMPLEPERF_CALLGRAPH_REPORT)
    body = client.get(f"/api/runs/{run_id}/deep-analysis").json()
    face = body["simpleperf"]
    assert face["stat"]["mode"] == "callgraph"
    assert face["stat"]["entryCount"] == 1
    tree = face["flamegraph"]["tree"]
    assert tree["name"] == "GameThread::Update" and tree["value"] == 100.0
    branches = {c["name"]: c for c in tree["children"]}
    # 链上延续帧（RenderScene 等）为 value=0 叶；分支节点带 entry 内相对百分比
    assert branches["RenderScene"]["value"] == 0.0
    assert branches["BuildDrawCalls"]["value"] == 60.0
    assert branches["BuildDrawCalls"]["children"][0] == {
        "name": "SubmitCmds", "value": 40.0, "children": [],
    }
    sort_branch = branches["SortDrawCalls"]
    assert sort_branch["value"] == 30.0
    assert sort_branch["children"] == [{"name": "SortQueue", "value": 0.0, "children": []}]
    assert face["flamegraph"]["maxDepth"] == 3  # root→BuildDrawCalls→SubmitCmds


def test_deep_analysis_without_simpleperf_artifact_notes_missing(client, trace_env) -> None:
    """无 simpleperf 工件 → available=false + note（如实缺失，不伪造）。"""
    run_id = make_trace_run(client)
    body = client.get(f"/api/runs/{run_id}/deep-analysis").json()
    face = body["simpleperf"]
    assert face["available"] is False
    assert face["source"] is None and face["stat"] is None and face["flamegraph"] is None
    assert "不回退伪造" in face["note"]


def test_deep_analysis_empty_trace_body_still_carries_simpleperf_face(client) -> None:
    """trace 与 simpleperf 独立采集：空 trace 结构 ≠ 空 simpleperf 数据面。"""
    run_path = client.runs_dir / "PD_SIMPLEPERF_ONLY"
    artifacts = run_path / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "simpleperf-report.txt").write_text(SIMPLEPERF_FLAT_REPORT, encoding="utf-8")
    resp = client.get("/api/runs/run-PD_SIMPLEPERF_ONLY/deep-analysis")
    assert resp.status_code == 200
    body = resp.json()
    assert body["traceAvailable"] is False
    assert body["note"] and "不回退伪造帧数据" in body["note"]
    face = body["simpleperf"]
    assert face["available"] is True
    assert face["flamegraph"]["tree"]["value"] == 100.0


def test_deep_analysis_unparseable_simpleperf_report_notes_unparsed(client, trace_env) -> None:
    """report 存在但无样本表行（失败占位文本）→ available=true 但 stat/flamegraph
    为 None + note（不回退伪造）。"""
    run_id = _make_trace_run_with_simpleperf(
        client,
        "simpleperf E record_file_reader.cpp:88] failed to open record file: No such file or directory\n",
    )
    body = client.get(f"/api/runs/{run_id}/deep-analysis").json()
    face = body["simpleperf"]
    assert face["available"] is True
    assert face["source"] == "artifacts/simpleperf-report.txt"
    assert face["stat"] is None and face["flamegraph"] is None
    assert "不回退伪造" in face["note"]
