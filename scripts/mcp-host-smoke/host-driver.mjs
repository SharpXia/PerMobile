/**
 * MCP 真实宿主侧冒烟驱动（2026-11-06 体检 mcp-host-integration 的可复现工件）。
 *
 * 使用官方 MCP TypeScript SDK 客户端（@modelcontextprotocol/sdk，Claude
 * Desktop/Code 宿主侧同款客户端实现）以**独立进程**经 stdio 拉起
 * services/backend/mcp_server.py，完成 initialize → tools/list → tools/call
 * 全链（8 工具逐个调用），验证协议互操作——非仓库自测代码回环。
 *
 * 运行（需后端已启动；仓库根执行）：
 *
 *     # 1) 启动后端（synthetic 即可驱动全部 8 工具）
 *     .venv/bin/python services/backend/run.py --port 8737 --provider synthetic
 *     # 2) 首次安装 SDK（本目录内，node_modules 不入库）
 *     npm --prefix scripts/mcp-host-smoke install
 *     # 3) 驱动（--base-url 默认 http://127.0.0.1:8737）
 *     node scripts/mcp-host-smoke/host-driver.mjs [--base-url http://127.0.0.1:8737]
 *
 * 环境变量：PD_MCP_SERVER_CMD / PD_MCP_SERVER_ARGS 可替换被测 server 命令
 * （默认仓库 .venv python + services/backend/mcp_server.py）。
 */
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = path.resolve(SCRIPT_DIR, '..', '..')

const baseArgs = process.argv.slice(2)
const baseUrlArgIdx = baseArgs.indexOf('--base-url')
const BASE_URL =
  baseUrlArgIdx >= 0 ? baseArgs[baseUrlArgIdx + 1] : (process.env.PD_MCP_BASE_URL ?? 'http://127.0.0.1:8737')

const SERVER_CMD = process.env.PD_MCP_SERVER_CMD ?? path.join(REPO_ROOT, '.venv', 'bin', 'python')
const SERVER_ARGS = (
  process.env.PD_MCP_SERVER_ARGS ??
  `${path.join(REPO_ROOT, 'services', 'backend', 'mcp_server.py')} --base-url ${BASE_URL}`
).split(' ')

function step(name, value) {
  console.log(`\n=== ${name} ===`)
  console.log(JSON.stringify(value, null, 2).slice(0, 1600))
}

function fail(message) {
  console.error(`\n[FAIL] ${message}`)
  process.exit(1)
}

const transport = new StdioClientTransport({ command: SERVER_CMD, args: SERVER_ARGS })
const client = new Client({ name: 'pd-mcp-host-smoke', version: '1.0.0' })

await client.connect(transport)
step('initialize（宿主收到 serverInfo）', client.getServerVersion())

const tools = await client.listTools()
const toolNames = tools.tools.map((t) => t.name)
step(`tools/list（${toolNames.length} 个工具）`, toolNames)

const devices = await client.callTool({ name: 'list_devices', arguments: {} })
if (devices.isError) fail('list_devices isError')
const firstDevice = JSON.parse(devices.content[0].text).devices[0]
step('tools/call list_devices', { deviceId: firstDevice.id, model: firstDevice.model })

const session = await client.callTool({
  name: 'start_session',
  arguments: { deviceId: firstDevice.id, packageName: 'com.example.synthetic.game' },
})
if (session.isError) fail('start_session isError')
const sid = JSON.parse(session.content[0].text).id
step('tools/call start_session', { sessionId: sid })

await client.callTool({ name: 'record_start', arguments: { sessionId: sid } })
console.log('\nrecord_start ok; 等待采样累计（3s）…')
await new Promise((r) => setTimeout(r, 3000))
await client.callTool({ name: 'record_stop', arguments: { sessionId: sid } })
console.log('record_stop ok')

const runsBefore = await client.callTool({ name: 'list_runs', arguments: {} })
console.log(`list_runs ok（保存前 ${JSON.parse(runsBefore.content[0].text).runs.length} 条）`)

const saved = await client.callTool({
  name: 'save_session',
  arguments: { sessionId: sid, caseName: 'mcp-host-smoke' },
})
if (saved.isError) fail('save_session isError')
const runId = JSON.parse(saved.content[0].text).runId
step('tools/call save_session', { runId })

const stats = await client.callTool({ name: 'get_statistics', arguments: { runId } })
if (stats.isError) fail('get_statistics isError')
const statsBody = JSON.parse(stats.content[0].text)
step('tools/call get_statistics', { schemaVersion: statsBody.schemaVersion, fpsAvg: statsBody.fps?.avg })

const runDetail = await client.callTool({ name: 'get_run', arguments: { runId } })
if (runDetail.isError) fail('get_run isError')
step('tools/call get_run', { runId: JSON.parse(runDetail.content[0].text).runId })

await client.close()
console.log(`\n=== 全链通过：initialize + tools/list(${toolNames.length}) + tools/call ×8 经官方 MCP SDK 客户端完成（runId=${runId}） ===`)
process.exit(0)
