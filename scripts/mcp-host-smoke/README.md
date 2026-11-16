# MCP 真实宿主侧冒烟（scripts/mcp-host-smoke）

MCP server（`services/backend/mcp_server.py`，stdio JSON-RPC）的**宿主侧独立客户端**全链冒烟：
官方 MCP TypeScript SDK 客户端（`@modelcontextprotocol/sdk`，Claude Desktop/Code 宿主侧同款
客户端实现）经 stdio 拉起 server，逐个调用全部 8 个工具（list_devices → start_session →
record_start/stop → list_runs → save_session → get_statistics → get_run）。

## 运行

```bash
# 1) 启动后端（synthetic 即可驱动全部 8 工具；real 需设备在位）
.venv/bin/python services/backend/run.py --port 8737 --provider synthetic

# 2) 首次安装 SDK（本目录内；node_modules 不入库）
npm --prefix scripts/mcp-host-smoke install

# 3) 驱动（缺省 --base-url http://127.0.0.1:8737）
node scripts/mcp-host-smoke/host-driver.mjs

# 成功标志：末行「全链通过：initialize + tools/list(8) + tools/call ×8 经官方 MCP SDK 客户端完成」
# 退出码 0=通过；任一步 isError/协议失败即退出码 1
```

## 边界（如实声明）

- 宿主为**官方 SDK 客户端实现**，非 Claude Desktop/Code 产品本身——产品级宿主（GUI 配置
  JSON + 真实模型调用链）未在本仓库自动化覆盖（本机 claude CLI 挂起无法驱动）。
- `save_session` 会向后端 runs 目录写入一条 `mcp-host-smoke` run（synthetic 数据，
  可经 `DELETE /api/cases/{runId}` 或删除目录清理）。
- 服务端命令可用 `PD_MCP_SERVER_CMD` / `PD_MCP_SERVER_ARGS` 替换（默认仓库 .venv）。
