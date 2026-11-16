"""MCP stdio server（R56 最小切片，契约 §25）。

- 协议层单测：initialize/ping/tools/list/tools/call 分派、notification 不回包、
  -32601/-32602/-32700、工具错误走 result.isError（业务失败 ≠ 协议失败）；
- e2e：本进程内起 uvicorn（synthetic provider，随机端口）→ McpServer.serve()
  全链路（JSON-RPC 解析 → stdlib urllib HTTP → 后端契约端点）。

子进程形态（`python mcp_server.py` 的 __main__ + argparse 薄壳）不在 e2e 内——
serve() 全路径已被 e2e 覆盖，__main__ 仅拼装 argparse + serve，如实声明。
"""

from __future__ import annotations

import io
import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import mcp_server as mcp  # noqa: E402
from mcp_server import BackendClient, BackendError, McpServer  # noqa: E402


# ---- Fake 后端（协议层单测；记录请求并按应答表返回）----


class FakeBackend:
    def __init__(self, results: dict[tuple[str, str], Any] | None = None) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.results = results or {}

    def request(self, method: str, path: str, body: Any = None) -> Any:
        self.calls.append((method, path, body))
        key = (method, path.split("?")[0])
        if key not in self.results:
            raise BackendError(404, {"error": {"code": "report_not_found", "message": path}})
        return self.results[key]


def _msg(method: str, msg_id: int = 1, params: Any = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        payload["params"] = params
    return payload


def _call(server: McpServer, msg: dict[str, Any]) -> dict[str, Any]:
    resp = server.handle_message(msg)
    assert resp is not None
    return resp


# ---- 协议层 ----


def test_initialize_handshake() -> None:
    resp = _call(McpServer(FakeBackend()), _msg("initialize"))
    assert resp["jsonrpc"] == "2.0"
    assert resp["result"]["protocolVersion"] == mcp.PROTOCOL_VERSION == "2024-11-05"
    assert resp["result"]["capabilities"] == {"tools": {}}
    assert resp["result"]["serverInfo"]["name"] == "perfdog-mcp"


def test_initialized_notification_no_reply() -> None:
    assert McpServer(FakeBackend()).handle_message({
        "jsonrpc": "2.0", "method": "notifications/initialized",
    }) is None


def test_ping() -> None:
    assert _call(McpServer(FakeBackend()), _msg("ping"))["result"] == {}


def test_tools_list_shape() -> None:
    resp = _call(McpServer(FakeBackend()), _msg("tools/list"))
    tools = resp["result"]["tools"]
    names = [t["name"] for t in tools]
    # 15 工具（M8+ 缺口 mcp-report-tools-missing 补齐报告分析面 7 工具，对标
    # PerfDog 15；契约 §25.3 golden 8→N delta 由 S1 统一落笔）
    assert names == [
        "list_devices", "start_session", "record_start", "record_stop",
        "save_session", "list_runs", "get_run", "get_statistics",
        "get_metric_series", "get_frame_details", "get_deep_analysis",
        "get_diagnostics", "compare_reports", "get_run_markers", "list_cases",
    ]
    for tool in tools:
        assert tool["description"]
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert set(schema["required"]) <= set(schema["properties"])
        assert tool["http"][0] in ("GET", "POST")
        assert tool["http"][1].startswith("/api/")


def test_unknown_method_32601() -> None:
    resp = _call(McpServer(FakeBackend()), _msg("resources/list"))
    assert resp["error"]["code"] == -32601


def test_invalid_request_32600() -> None:
    for bad in ("not-a-dict", {"id": 1}, {"jsonrpc": "1.0", "id": 1, "method": "x"}):
        resp = McpServer(FakeBackend()).handle_message(bad)
        assert resp is not None and resp["error"]["code"] == -32600


def test_parse_error_32700_in_serve_loop() -> None:
    server = McpServer(FakeBackend())
    out = io.BytesIO()
    server.serve(io.BytesIO(b"{not json}\n"), out)
    resp = json.loads(out.getvalue())
    assert resp["error"]["code"] == -32700
    assert resp["id"] is None


# ---- tools/call 分派（Fake 后端）----


def test_tools_call_list_devices() -> None:
    backend = FakeBackend({("GET", "/api/devices"): {"mode": "synthetic", "devices": []}})
    resp = _call(McpServer(backend), _msg("tools/call", params={
        "name": "list_devices", "arguments": {},
    }))
    assert resp["result"]["isError"] is False
    assert json.loads(resp["result"]["content"][0]["text"]) == {"mode": "synthetic", "devices": []}
    assert backend.calls == [("GET", "/api/devices", None)]


def test_tools_call_start_session_body_shape() -> None:
    backend = FakeBackend({("POST", "/api/sessions"): {"id": "sess-01", "state": "monitoring"}})
    server = McpServer(backend)
    resp = _call(server, _msg("tools/call", params={
        "name": "start_session",
        "arguments": {"deviceId": "synthetic-001", "packageName": "com.example.synthetic.game",
                      "launchOnCreate": False},
    }))
    assert resp["result"]["isError"] is False
    # 路径参数不进 body；body 只含会话创建字段
    assert backend.calls == [(
        "POST", "/api/sessions",
        {"deviceId": "synthetic-001", "packageName": "com.example.synthetic.game",
         "launchOnCreate": False},
    )]


def test_tools_call_path_param_quoting() -> None:
    """路径参数 URL 编码（runId 含特殊字符不破坏路径结构）。"""
    backend = FakeBackend()
    server = McpServer(backend)
    server.handle_message(_msg("tools/call", params={
        "name": "get_run", "arguments": {"runId": "run/a b"},
    }))
    assert backend.calls[0][1] == "/api/runs/run%2Fa%20b"


def test_tools_call_get_statistics_query_param() -> None:
    backend = FakeBackend()
    server = McpServer(backend)
    server.handle_message(_msg("tools/call", params={
        "name": "get_statistics", "arguments": {"runId": "run-X", "fpsGe": "18,25"},
    }))
    assert backend.calls[0][:2] == ("GET", "/api/runs/run-X/statistics?fpsGe=18%2C25")


# ---- 报告分析面 7 工具（M8+ 缺口 mcp-report-tools-missing；透传映射冒烟）----


def test_tools_call_get_metric_series_maps_replay_query() -> None:
    """LTTB 降采样序列（§19 回放聚合）：maxPoints 进 query。"""
    backend = FakeBackend({("GET", "/api/runs/run-X/replay"): {"series": [], "downsampled": False}})
    resp = _call(McpServer(backend), _msg("tools/call", params={
        "name": "get_metric_series",
        "arguments": {"runId": "run-X", "maxPoints": 600},
    }))
    assert resp["result"]["isError"] is False
    assert backend.calls == [("GET", "/api/runs/run-X/replay?maxPoints=600", None)]


def test_tools_call_get_frame_details_maps_deep_analysis() -> None:
    """逐帧明细（§22.1 概览形态，含 simpleperf 火焰图数据面）：无 query 直透。"""
    backend = FakeBackend({("GET", "/api/runs/run-X/deep-analysis"): {"frameCount": 0}})
    resp = _call(McpServer(backend), _msg("tools/call", params={
        "name": "get_frame_details", "arguments": {"runId": "run-X"},
    }))
    assert resp["result"]["isError"] is False
    assert backend.calls == [("GET", "/api/runs/run-X/deep-analysis", None)]


def test_tools_call_get_deep_analysis_frame_index_zero_is_valid() -> None:
    """单帧 Overview 卡：frameIndex=0 是合法必填值（0 不按缺失处理，整型必填回归）。"""
    backend = FakeBackend({("GET", "/api/runs/run-X/deep-analysis"): {"frame": {}}})
    resp = _call(McpServer(backend), _msg("tools/call", params={
        "name": "get_deep_analysis",
        "arguments": {"runId": "run-X", "frameIndex": 0},
    }))
    assert resp["result"]["isError"] is False
    assert backend.calls == [("GET", "/api/runs/run-X/deep-analysis?frameIndex=0", None)]

    missing = _call(McpServer(FakeBackend()), _msg("tools/call", params={
        "name": "get_deep_analysis", "arguments": {"runId": "run-X"},
    }))
    assert missing["result"]["isError"] is True
    assert json.loads(missing["result"]["content"][0]["text"])["error"]["code"] == "validation_error"


def test_tools_call_get_diagnostics_query_mapping() -> None:
    """根因诊断（§22.2）：可选参数进 query（数值按字符串化 URL 编码）。"""
    backend = FakeBackend({("GET", "/api/runs/run-X/diagnostics"): {"dropIntervals": []}})
    resp = _call(McpServer(backend), _msg("tools/call", params={
        "name": "get_diagnostics",
        "arguments": {"runId": "run-X", "anchor": "cpu.total.pct", "dropThresholdPct": 20},
    }))
    assert resp["result"]["isError"] is False
    path = backend.calls[0][1]
    assert path.startswith("/api/runs/run-X/diagnostics?")
    assert "anchor=cpu.total.pct" in path and "dropThresholdPct=20" in path


def test_tools_call_compare_reports_posts_body() -> None:
    """对比（§9 POST /reports/compare）：baselineRunIds 数组必填进 body（透传）。"""
    backend = FakeBackend({("POST", "/api/reports/compare"): {"schemaVersion": "perfeval.compare.v1"}})
    resp = _call(McpServer(backend), _msg("tools/call", params={
        "name": "compare_reports",
        "arguments": {"baselineRunIds": ["run-A", "run-B"], "candidateRunId": "run-C"},
    }))
    assert resp["result"]["isError"] is False
    assert backend.calls == [(
        "POST", "/api/reports/compare",
        {"baselineRunIds": ["run-A", "run-B"], "candidateRunId": "run-C"},
    )]


def test_tools_call_run_markers_and_cases() -> None:
    """run 标记（§15.3）与 case 索引（§14）：快照/配置类最小切片透传。"""
    backend = FakeBackend({
        ("GET", "/api/runs/run-X/markers"): {"markers": []},
        ("GET", "/api/cases"): {"cases": []},
    })
    server = McpServer(backend)
    resp_markers = _call(server, _msg("tools/call", params={
        "name": "get_run_markers", "arguments": {"runId": "run-X"},
    }))
    resp_cases = _call(server, _msg("tools/call", params={
        "name": "list_cases", "arguments": {},
    }))
    assert resp_markers["result"]["isError"] is False
    assert resp_cases["result"]["isError"] is False
    assert backend.calls == [
        ("GET", "/api/runs/run-X/markers", None),
        ("GET", "/api/cases", None),
    ]


def test_tools_call_backend_error_is_tool_error_not_protocol_error() -> None:
    """后端 404（如 report_not_found）→ result.isError=true + 契约 §3 错误体原样，
    不改写为 JSON-RPC error（业务失败 ≠ 协议失败）。"""
    resp = _call(McpServer(FakeBackend()), _msg("tools/call", params={
        "name": "get_run", "arguments": {"runId": "run-ghost"},
    }))
    assert resp["result"]["isError"] is True
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["error"]["code"] == "report_not_found"
    assert "error" not in resp  # 协议层无 error 字段


def test_tools_call_missing_required_args() -> None:
    resp = _call(McpServer(FakeBackend()), _msg("tools/call", params={
        "name": "record_start", "arguments": {},
    }))
    assert resp["result"]["isError"] is True
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["error"]["code"] == "validation_error"


def test_tools_call_unknown_tool_32602() -> None:
    resp = _call(McpServer(FakeBackend()), _msg("tools/call", params={
        "name": "delete_everything", "arguments": {},
    }))
    assert resp["error"]["code"] == -32602


def test_backend_unreachable_is_tool_error() -> None:
    """后端不可达 → 合成 backend_unreachable 错误体走 isError（协议循环不崩）。"""

    class DeadBackend(FakeBackend):
        def request(self, method: str, path: str, body: Any = None) -> Any:
            raise BackendError(0, {"error": {"code": "backend_unreachable", "message": "down"}})

    resp = _call(McpServer(DeadBackend()), _msg("tools/call", params={
        "name": "list_devices", "arguments": {},
    }))
    assert resp["result"]["isError"] is True
    assert json.loads(resp["result"]["content"][0]["text"])["error"]["code"] == "backend_unreachable"


# ---- e2e：本进程 uvicorn（synthetic）+ serve() 全链路 ----


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def live_backend(tmp_path: Path):
    import uvicorn

    from app.config import Settings
    from app.main import create_app
    from app.providers.synthetic import SyntheticProvider

    settings = Settings(
        provider="synthetic",
        runs_dir=tmp_path / "runs",
        cases_db_path=tmp_path / "data" / "cases.db",
        synthetic_log_pace_s=60.0,
    )
    app = create_app(settings, provider=SyntheticProvider())
    port = _free_tcp_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn 未能在时限内启动"
    yield f"http://127.0.0.1:{port}", settings.runs_dir
    server.should_exit = True
    thread.join(timeout=10)


def _serve_lines(server: McpServer, *messages: dict[str, Any]) -> list[dict[str, Any]]:
    payload = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages).encode()
    out = io.BytesIO()
    server.serve(io.BytesIO(payload), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_e2e_full_device_to_report_flow(live_backend) -> None:
    """全链路：initialize → list_devices → start_session → record_start →
    record_stop → save_session → list_runs → get_statistics（synthetic 会话）。"""
    base_url, _ = live_backend
    server = McpServer(BackendClient(base_url, timeout=10.0))
    resps = _serve_lines(
        server,
        _msg("initialize", 1),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        _msg("tools/call", 2, {"name": "list_devices", "arguments": {}}),
        _msg("tools/call", 3, {
            "name": "start_session",
            "arguments": {"deviceId": "synthetic-001", "packageName": "com.example.synthetic.game"},
        }),
    )
    assert resps[0]["result"]["protocolVersion"] == "2024-11-05"
    assert len(resps) == 3  # notification 不回包
    devices = json.loads(resps[1]["result"]["content"][0]["text"])
    assert devices["devices"][0]["id"] == "synthetic-001"
    session = json.loads(resps[2]["result"]["content"][0]["text"])
    assert session["state"] == "monitoring"
    session_id = session["id"]

    resps = _serve_lines(
        server,
        _msg("tools/call", 10, {"name": "record_start", "arguments": {"sessionId": session_id}}),
        _msg("tools/call", 11, {"name": "record_stop", "arguments": {"sessionId": session_id}}),
        _msg("tools/call", 12, {"name": "save_session", "arguments": {"sessionId": session_id}}),
    )
    assert all(r["result"]["isError"] is False for r in resps), resps
    saved = json.loads(resps[2]["result"]["content"][0]["text"])
    assert saved["state"] == "saved"
    run_id = saved["runId"]

    resps = _serve_lines(
        server,
        _msg("tools/call", 20, {"name": "list_runs", "arguments": {}}),
        _msg("tools/call", 21, {"name": "get_run", "arguments": {"runId": run_id}}),
        _msg("tools/call", 22, {
            "name": "get_statistics",
            "arguments": {"runId": run_id, "fpsGe": "18,25"},
        }),
    )
    assert all(r["result"]["isError"] is False for r in resps), resps
    runs = json.loads(resps[0]["result"]["content"][0]["text"])
    assert [r["runId"] for r in runs["runs"]] == [run_id]
    stats = json.loads(resps[2]["result"]["content"][0]["text"])
    assert stats["schemaVersion"] == "perfdog.statistics.v1"


def test_e2e_record_start_twice_is_tool_error(live_backend) -> None:
    """后端 409 session_state_conflict → MCP 工具错误（契约 §3 错误体透传）。"""
    base_url, _ = live_backend
    server = McpServer(BackendClient(base_url, timeout=10.0))
    (created,) = _serve_lines(server, _msg("tools/call", 1, {
        "name": "start_session",
        "arguments": {"deviceId": "synthetic-001", "packageName": "com.example.synthetic.game"},
    }))
    session_id = json.loads(created["result"]["content"][0]["text"])["id"]
    resps = _serve_lines(
        server,
        _msg("tools/call", 2, {"name": "record_start", "arguments": {"sessionId": session_id}}),
        _msg("tools/call", 3, {"name": "record_start", "arguments": {"sessionId": session_id}}),
    )
    assert resps[0]["result"]["isError"] is False
    assert resps[1]["result"]["isError"] is True
    body = json.loads(resps[1]["result"]["content"][0]["text"])
    assert body["error"]["code"] == "session_state_conflict"


def test_e2e_report_analysis_tools_after_save(live_backend) -> None:
    """报告分析面工具 e2e 冒烟（synthetic run）：LTTB 序列 / 逐帧明细（含 simpleperf
    数据面如实缺失）/ 根因诊断 / run 标记 / case 索引——无 trace/simpleperf 工件的
    run 走 200 空结构 + note（不伪造），isError=false。"""
    base_url, _ = live_backend
    server = McpServer(BackendClient(base_url, timeout=10.0))
    (created,) = _serve_lines(server, _msg("tools/call", 1, {
        "name": "start_session",
        "arguments": {"deviceId": "synthetic-001", "packageName": "com.example.synthetic.game"},
    }))
    session_id = json.loads(created["result"]["content"][0]["text"])["id"]
    resps = _serve_lines(
        server,
        _msg("tools/call", 2, {"name": "record_start", "arguments": {"sessionId": session_id}}),
        _msg("tools/call", 3, {"name": "record_stop", "arguments": {"sessionId": session_id}}),
        _msg("tools/call", 4, {"name": "save_session", "arguments": {"sessionId": session_id}}),
    )
    run_id = json.loads(resps[2]["result"]["content"][0]["text"])["runId"]

    resps = _serve_lines(
        server,
        _msg("tools/call", 10, {"name": "get_metric_series", "arguments": {"runId": run_id}}),
        _msg("tools/call", 11, {"name": "get_frame_details", "arguments": {"runId": run_id}}),
        _msg("tools/call", 12, {"name": "get_diagnostics", "arguments": {"runId": run_id}}),
        _msg("tools/call", 13, {"name": "get_run_markers", "arguments": {"runId": run_id}}),
        _msg("tools/call", 14, {"name": "list_cases", "arguments": {}}),
    )
    assert all(r["result"]["isError"] is False for r in resps), resps
    replay = json.loads(resps[0]["result"]["content"][0]["text"])
    assert isinstance(replay["series"], list) and replay["downsampled"] is False
    frames = json.loads(resps[1]["result"]["content"][0]["text"])
    assert frames["traceAvailable"] is False
    assert "不回退伪造" in frames["simpleperf"]["note"]  # simpleperf 数据面如实缺失
    diagnostics = json.loads(resps[2]["result"]["content"][0]["text"])
    assert "dropIntervals" in diagnostics  # §22.2 体（synthetic 样本无下降 → 空）
    markers = json.loads(resps[3]["result"]["content"][0]["text"])
    assert markers == [] or "markers" in markers  # §15.3 读回体（无标记 → []）
    cases = json.loads(resps[4]["result"]["content"][0]["text"])
    assert isinstance(cases, dict) or isinstance(cases, list)
