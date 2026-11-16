#!/usr/bin/env node
// H6 api-client 隔离门禁。
// 权威定义：开发仓库内部架构文档 §5.2
//   - 递归扫描 services/frontend/src/ 全部 .ts/.js/.vue（含 main.ts 与未来新增目录），
//     豁免仅 src/api-client/；
//   - 禁止词按标识符级匹配（覆盖引用与实例化两种形态）：
//     fetch( / EventSource / XMLHttpRequest / WebSocket；
//   - 退出码：0=通过；1=发现违禁引用（输出 文件:行号 清单）；
//     2=src/ 或 api-client/ 目录结构性缺失（按失败处理，不误判通过）。
// 仅用 Node 内置模块，无第三方依赖；路径相对脚本自身解析，与调用方 CWD 无关。

import { readdirSync, statSync, readFileSync } from 'node:fs';
import { extname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const FRONTEND_ROOT = resolve(fileURLToPath(new URL('..', import.meta.url)));
const SRC_DIR = join(FRONTEND_ROOT, 'src');
const EXEMPT_DIR = join(SRC_DIR, 'api-client'); // 唯一豁免面：src/api-client/
const SCAN_EXTENSIONS = new Set(['.ts', '.js', '.vue']);

// 标识符级匹配：
//  - \b 左边界拒绝 refetch( / MyWebSocket / XMLHttpRequest2 这类同串前缀；
//  - 右边界（对裸标识符）拒绝 WebSocketClient / EventSourceImpl 这类同串后缀；
//  - `fetch\s*\(` 允许标识符与 "(" 间空白（fetch (x) 是合法调用），仍拒绝 refetch(。
const FORBIDDEN = [
  { word: 'fetch(', pattern: /\bfetch\s*\(/ },
  { word: 'EventSource', pattern: /\bEventSource\b/ },
  { word: 'XMLHttpRequest', pattern: /\bXMLHttpRequest\b/ },
  { word: 'WebSocket', pattern: /\bWebSocket\b/ },
];

function isDirectory(p) {
  try {
    return statSync(p).isDirectory();
  } catch {
    return false;
  }
}

// 结构性缺失：src/ 或 api-client/ 不存在 → 退出码 2（按失败处理）
if (!isDirectory(SRC_DIR)) {
  console.error(`[isolation] 结构性缺失：src/ 目录不存在（期望 ${SRC_DIR}）`);
  process.exit(2);
}
if (!isDirectory(EXEMPT_DIR)) {
  console.error(
    `[isolation] 结构性缺失：src/api-client/ 目录不存在（期望 ${EXEMPT_DIR}）` +
      `——api-client 是 H6 的网络面兼豁免面，必须存在`,
  );
  process.exit(2);
}

// 递归收集待扫描文件；除 EXEMPT_DIR 外不做任何其他豁免（不留暗门）
function collectFiles(dir, out) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (p === EXEMPT_DIR) continue;
      collectFiles(p, out);
    } else if (entry.isFile() && SCAN_EXTENSIONS.has(extname(entry.name))) {
      out.push(p);
    }
  }
}

const files = [];
collectFiles(SRC_DIR, files);
files.sort();

const violations = [];
for (const file of files) {
  const rel = relative(FRONTEND_ROOT, file).split(sep).join('/');
  let content;
  try {
    content = readFileSync(file, 'utf8');
  } catch (err) {
    // 无法读取 = 无法验证 = 按违禁处理
    console.error(`[isolation] 无法读取 ${rel}: ${err.message}`);
    process.exitCode = 1;
    continue;
  }
  content.split(/\r?\n/).forEach((lineText, i) => {
    for (const { word, pattern } of FORBIDDEN) {
      if (pattern.test(lineText)) {
        const snippet =
          lineText.trim().length > 120 ? `${lineText.trim().slice(0, 120)}…` : lineText.trim();
        violations.push(`${rel}:${i + 1}  [${word}]  ${snippet}`);
      }
    }
  });
}

if (violations.length > 0) {
  console.error(`[isolation] 发现 ${violations.length} 处违禁引用：`);
  for (const v of violations) console.error(`  ${v}`);
  console.error(
    `[isolation] 网络访问只允许存在于 src/api-client/（ARCHITECTURE.md §5.2 / H6）`,
  );
  process.exit(1);
}

console.log(
  `[isolation] 通过：扫描 ${files.length} 个 .ts/.js/.vue 文件，豁免 src/api-client/，未发现违禁引用。`,
);
process.exit(0);
