#!/usr/bin/env bash
# 安装 PerfEval Desktop.app 到 /Applications 并重注册 LaunchServices。
#
# 为什么不能只 rm+cp（2026-11-11 实录）：反复 rm+cp 覆盖 bundle 后 inode 变化，
# LaunchServices 数据库仍指向旧记录 → `open -a` / Dock 点开**静默失败**（进程不起、
# 无报错），用户感知为「重启 app 异常」。`lsregister -f` 强制重注册后恢复；
# 两轮 quit→open 循环验证 2s 内起来。app 运行中需先退出（本脚本自动处理）。
set -euo pipefail

SRC="${1:-$(cd "$(dirname "$0")" && pwd)/dist/mac-arm64/PerfEval Desktop.app}"
DEST="/Applications/PerfEval Desktop.app"
LSR=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister

[ -d "$SRC" ] || { echo "产物不存在: $SRC（先 npm run dist:dir）" >&2; exit 1; }

# 运行中的实例先退出（sidecar 收口由壳的 before-quit 阻塞逻辑负责）
osascript -e 'quit app "PerfEval Desktop"' >/dev/null 2>&1 || true
for _ in $(seq 1 10); do
  pgrep -f "PerfEval Desktop.app/Contents/MacOS" >/dev/null 2>&1 || break
  sleep 1
done
pkill -9 -f "PerfEval Desktop.app/Contents/MacOS" 2>/dev/null || true
sleep 1

rm -rf "$DEST"
cp -R "$SRC" "$DEST"
"$LSR" -f "$DEST" # 重注册 LaunchServices：防 open/Dock 静默失败（见头部注释）

echo "已安装并重注册: $DEST"
