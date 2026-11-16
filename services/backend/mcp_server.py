#!/usr/bin/env python3
"""perfdog MCP stdio server（R56，契约 §25）。

把后端核心能力包成 MCP 工具，stdio 传输（换行分隔 JSON-RPC 2.0，MCP
2024-11-05 规范的 stdio 帧格式）。**工具面（M8+ 缺口 mcp-report-tools-missing
补齐，2026-11-10）**：8 → 15 工具——原 8 个（设备列表 / 会话起停 / 报告获取）+
新增 7 个报告分析面（LTTB 降采样序列 / 逐帧明细 / 单帧深度分析 / 根因诊断 /
对比 / run 标记 / case 索引），全部沿用既有 tool['http'] 方法+路径映射透传模式，
无新业务逻辑；对标 PerfDog 15 工具的差集说明见 DEVELOPMENT_PLAN R56 行。

**选型（2026-11-04 实测依据，写入 ARCHITECTURE §4.11）**：手写 JSON-RPC，
不引入 `mcp` SDK——`pip install --dry-run mcp` 实测将新装 16 个包
（cryptography 3.9MB C 扩展、rpds-py C 扩展、httpx2、sse-starlette、
jsonschema、PyJWT、python-multipart 等），而本服务只需要 stdio 传输 +
15 个透传工具；后端能力全部经 HTTP 暴露（契约 §4/§6/§9/§12/§14/§15/§19/§22），
本文件只做协议适配，stdlib `urllib.request` 即可，零新增依赖（requirements.txt 不动）。

运行：`.venv/bin/python services/backend/mcp_server.py --base-url http://127.0.0.1:8737`

协议边界（如实声明）：
- 仅实现 initialize / ping / tools/list / tools/call + notifications/initialized；
- 未知 method（带 id）→ -32601；解析失败 → -32700（id=null）；未知工具 → -32602；
- 后端 HTTP 错误体（契约 §3）原样进 result.isError=true 的 text 内容，
  不改写为协议错误（业务失败 ≠ 协议失败）；
- 日志只走 stderr，stdout 是协议通道。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, BinaryIO

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "perfdog-mcp", "version": "1.0.0"}


# ---- 工具定义（契约 §25 表；单一事实源 = 后端 HTTP 契约）----


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "list_devices",
        "description": "列出可测设备（GET /api/devices；mode 标明 real/synthetic/simulator）",
        "inputSchema": _schema({}, []),
        "http": ("GET", "/api/devices"),
    },
    {
        "name": "start_session",
        "description": (
            "创建采集会话（POST /api/sessions）：deviceId+packageName 必填；"
            "metrics 可选（缺省用注册表默认指标与间隔）；launchOnCreate 可选"
        ),
        "inputSchema": _schema(
            {
                "deviceId": {"type": "string"},
                "packageName": {"type": "string"},
                "metrics": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "metricId": {"type": "string"},
                            "intervalMs": {"type": "integer"},
                        },
                        "required": ["metricId"],
                    },
                },
                "launchOnCreate": {"type": "boolean"},
            },
            ["deviceId", "packageName"],
        ),
        "http": ("POST", "/api/sessions"),
    },
    {
        "name": "record_start",
        "description": "开始记录（POST /api/sessions/{id}/record/start，monitoring→recording 落盘）",
        "inputSchema": _schema({"sessionId": {"type": "string"}}, ["sessionId"]),
        "http": ("POST", "/api/sessions/{sessionId}/record/start"),
    },
    {
        "name": "record_stop",
        "description": "停止记录（POST /api/sessions/{id}/record/stop，进入 save/discard 决策点）",
        "inputSchema": _schema({"sessionId": {"type": "string"}}, ["sessionId"]),
        "http": ("POST", "/api/sessions/{sessionId}/record/stop"),
    },
    {
        "name": "save_session",
        "description": (
            "收口保存（POST /api/sessions/{id}/save，写 run 目录并登记 case 索引）；"
            "caseName/exportExcel 可选"
        ),
        "inputSchema": _schema(
            {
                "sessionId": {"type": "string"},
                "caseName": {"type": "string"},
                "exportExcel": {"type": "boolean"},
            },
            ["sessionId"],
        ),
        "http": ("POST", "/api/sessions/{sessionId}/save"),
    },
    {
        "name": "list_runs",
        "description": "列出已保存的运行记录（GET /api/runs）",
        "inputSchema": _schema({}, []),
        "http": ("GET", "/api/runs"),
    },
    {
        "name": "get_run",
        "description": "取单次运行详情（GET /api/runs/{runId}：manifest 摘要 + summary + artifacts 索引）",
        "inputSchema": _schema({"runId": {"type": "string"}}, ["runId"]),
        "http": ("GET", "/api/runs/{runId}"),
    },
    {
        "name": "get_statistics",
        "description": (
            "取 PerfDog 口径统计量（GET /api/runs/{runId}/statistics，perfdog.statistics.v1）；"
            "fpsGe 可选（如 \"18,25\"）"
        ),
        "inputSchema": _schema(
            {"runId": {"type": "string"}, "fpsGe": {"type": "string"}}, ["runId"]
        ),
        "http": ("GET", "/api/runs/{runId}/statistics"),
    },
    # ---- 报告分析面 7 工具（M8+ 缺口 mcp-report-tools-missing，对标 15 工具；§25.3 delta 由契约流 S1 落笔）----
    {
        "name": "get_metric_series",
        "description": (
            "取 run 的 LTTB 降采样连续序列（GET /api/runs/{runId}/replay?maxPoints=，"
            "契约 §19 回放聚合 series 段：metricId/unit/points，maxPoints 上限 600，"
            "触发降采样时 downsampled=true + sampling=lttb）；maxPoints 可选"
        ),
        "inputSchema": _schema(
            {"runId": {"type": "string"}, "maxPoints": {"type": "integer"}}, ["runId"]
        ),
        "http": ("GET", "/api/runs/{runId}/replay"),
    },
    {
        "name": "get_frame_details",
        "description": (
            "取 run 的 FrameTimeline 逐帧明细（GET /api/runs/{runId}/deep-analysis，"
            "契约 §22.1 帧列表概览形态：frames[]/jankCount/jankRate；覆盖窗口以 trace "
            "为准，无 PerfDog ≤120s 限制——口径差异在响应 note/内声明）；"
            "响应另带 simpleperf 火焰图数据面"
        ),
        "inputSchema": _schema({"runId": {"type": "string"}}, ["runId"]),
        "http": ("GET", "/api/runs/{runId}/deep-analysis"),
    },
    {
        "name": "get_deep_analysis",
        "description": (
            "取单帧 Overview 卡 + 该帧附近 CPU 调度片段（GET /api/runs/{runId}/"
            "deep-analysis?frameIndex=，契约 §22.1 单帧形态）；frameIndex 必填"
        ),
        "inputSchema": _schema(
            {"runId": {"type": "string"}, "frameIndex": {"type": "integer"}},
            ["runId", "frameIndex"],
        ),
        "http": ("GET", "/api/runs/{runId}/deep-analysis"),
    },
    {
        "name": "get_diagnostics",
        "description": (
            "取根因诊断卡（GET /api/runs/{runId}/diagnostics，契约 §22.2：下降区间 + "
            "皮尔逊相关系数候选）；anchor/dropThresholdPct/minDurationMs/fromMs/toMs 可选"
        ),
        "inputSchema": _schema(
            {
                "runId": {"type": "string"},
                "anchor": {"type": "string"},
                "dropThresholdPct": {"type": "number"},
                "minDurationMs": {"type": "integer"},
                "fromMs": {"type": "integer"},
                "toMs": {"type": "integer"},
            },
            ["runId"],
        ),
        "http": ("GET", "/api/runs/{runId}/diagnostics"),
    },
    {
        "name": "compare_reports",
        "description": (
            "对比报告（POST /api/reports/compare，契约 §9 perfeval.compare.v1）；"
            "baselineRunIds[] + candidateRunId 必填，mode 可选——透传模式，S3 对比端点"
            "升级（如启用）对客户端透明"
        ),
        "inputSchema": _schema(
            {
                "baselineRunIds": {"type": "array", "items": {"type": "string"}},
                "candidateRunId": {"type": "string"},
                "mode": {"type": "string"},
            },
            ["baselineRunIds", "candidateRunId"],
        ),
        "http": ("POST", "/api/reports/compare"),
    },
    {
        "name": "get_run_markers",
        "description": (
            "取 run 侧标记（GET /api/runs/{runId}/markers，契约 §15.3 读回体；"
            "无标记 → 空数组）"
        ),
        "inputSchema": _schema({"runId": {"type": "string"}}, ["runId"]),
        "http": ("GET", "/api/runs/{runId}/markers"),
    },
    {
        "name": "list_cases",
        "description": "取 case 索引（GET /api/cases，契约 §14；快照/配置类最小切片）",
        "inputSchema": _schema({}, []),
        "http": ("GET", "/api/cases"),
    },
)

_TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}


# ---- 后端 HTTP 客户端（stdlib；错误体原样上抛）----


class BackendError(Exception):
    """后端非 2xx / 不可达；body 为契约 §3 错误体（不可达时为合成错误体）。"""

    def __init__(self, status: int, body: dict[str, Any] | None) -> None:
        self.status = status
        self.body = body or {"error": {"code": "backend_error", "message": f"HTTP {status}"}}
        super().__init__(str(self.body))


class BackendClient:
    def __init__(self, base_url: str, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read() or b"{}")
            except json.JSONDecodeError:
                payload = None
            raise BackendError(exc.code, payload) from None
        except urllib.error.URLError as exc:
            raise BackendError(
                0,
                {
                    "error": {
                        "code": "backend_unreachable",
                        "message": f"后端不可达 {self.base_url}: {exc.reason}",
                    }
                },
            ) from None
        return json.loads(raw) if raw else {}


# ---- MCP 协议层（stdio：换行分隔 JSON-RPC 2.0）----


class McpServer:
    def __init__(self, backend: BackendClient) -> None:
        self.backend = backend

    # -- 协议分派 --

    def handle_message(self, msg: Any) -> dict[str, Any] | None:
        """处理一条已解析的 JSON-RPC 消息；notification 返回 None（不回包）。"""
        if not (
            isinstance(msg, dict)
            and msg.get("jsonrpc") == "2.0"
            and isinstance(msg.get("method"), str)
        ):
            return self._error(None, -32600, "invalid request（缺少 jsonrpc/method）")
        msg_id = msg.get("id")
        method = msg["method"]
        if msg_id is None:
            # notification：initialized 确认；其余未知通知按规范忽略（不回包）
            return None
        params = msg.get("params") or {}
        if method == "initialize":
            return self._result(
                msg_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO,
                },
            )
        if method == "ping":
            return self._result(msg_id, {})
        if method == "tools/list":
            return self._result(msg_id, {"tools": [dict(t) for t in TOOLS]})
        if method == "tools/call":
            return self._tools_call(msg_id, params)
        return self._error(msg_id, -32601, f"method not found: {method}")

    def _tools_call(self, msg_id: Any, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            return self._error(msg_id, -32602, "tools/call params 必须是对象")
        name = params.get("name")
        tool = _TOOLS_BY_NAME.get(name) if isinstance(name, str) else None
        if tool is None:
            return self._error(msg_id, -32602, f"unknown tool: {name!r}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return self._error(msg_id, -32602, "arguments 必须是对象")
        # 必填校验：缺失/None/空串算缺；0 / False / 空数组是合法值（报告分析面工具带
        # 整型 frameIndex=0 与数组 baselineRunIds，不能按字符串真值判断）
        missing = [
            key for key in tool["inputSchema"]["required"]
            if key not in args or args[key] is None or args[key] == ""
        ]
        if missing:
            return self._tool_error(msg_id, {
                "error": {
                    "code": "validation_error",
                    "message": f"缺少必填参数: {missing}",
                    "details": {"field": missing[0]},
                }
            })
        http_method, path_tpl = tool["http"]
        path_params = _PATH_PARAMS[path_tpl]
        path = path_tpl.format(**{
            k: urllib.parse.quote(args[k], safe="") for k in path_params
        })
        rest = {k: v for k, v in args.items() if k not in path_params}
        payload: dict[str, Any] | None = None
        if http_method == "GET":
            if rest:
                path = f"{path}?{urllib.parse.urlencode({k: str(v) for k, v in rest.items()})}"
        else:
            payload = rest
        try:
            body = self.backend.request(http_method, path, payload)
        except BackendError as exc:
            return self._tool_error(msg_id, exc.body)
        except Exception as exc:  # noqa: BLE001 - 未分类异常按工具错误上报，不崩协议循环
            return self._tool_error(msg_id, {
                "error": {"code": "internal_error", "message": str(exc)}
            })
        return self._result(msg_id, {
            "content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}],
            "isError": False,
        })

    # -- 信封 helpers --

    @staticmethod
    def _result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    def _tool_error(self, msg_id: Any, error_body: dict[str, Any]) -> dict[str, Any]:
        """工具级失败 = result.isError=true（业务失败不伪装协议错误）。"""
        return self._result(msg_id, {
            "content": [{"type": "text", "text": json.dumps(error_body, ensure_ascii=False)}],
            "isError": True,
        })

    # -- stdio 循环 --

    def serve(self, stdin: BinaryIO, stdout: BinaryIO) -> None:
        """逐行读 JSON-RPC、逐行写响应；EOF 退出。日志只走 stderr。"""
        for raw in stdin:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                resp: dict[str, Any] | None = self._error(None, -32700, f"parse error: {exc}")
            else:
                resp = self.handle_message(msg)
            if resp is not None:
                stdout.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                stdout.flush()


# 路径模板中的路径参数（不进 query/body）
_PATH_PARAMS = {
    tool["http"][1]: {
        seg[1:-1] for seg in tool["http"][1].split("/") if seg.startswith("{") and seg.endswith("}")
    }
    for tool in TOOLS
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="perfdog MCP stdio server（契约 §25）")
    parser.add_argument(
        "--base-url", default="http://127.0.0.1:8737",
        help="后端地址（默认 http://127.0.0.1:8737，与 run.py 缺省一致）",
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="后端 HTTP 超时（秒）")
    args = parser.parse_args(argv)

    print(
        f"[perfdog-mcp] stdio server ready (backend={args.base_url}, "
        f"tools={len(TOOLS)}); logs go to stderr",
        file=sys.stderr,
    )
    server = McpServer(BackendClient(args.base_url, timeout=args.timeout))
    server.serve(sys.stdin.buffer, sys.stdout.buffer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
