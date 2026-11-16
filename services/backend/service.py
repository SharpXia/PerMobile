#!/usr/bin/env python3
"""perfdog 后端本地常驻 CLI（R55 最小切片：Service + CLI 自动化）。

`run.py` 本身就是本地常驻进程（uvicorn，ARCH §4.1），本文件只补**进程管理**
（start/stop/status + pidfile + 日志落盘），不重写服务内核、不引入守护框架：

    .venv/bin/python services/backend/service.py start  [--port 8737]
                        [--host 127.0.0.1] [--provider auto] [--runs-dir <path>]
    .venv/bin/python services/backend/service.py stop
    .venv/bin/python services/backend/service.py status

- pidfile：`services/backend/data/backend.pid`（data_dir 仅是 main() 的 Python
  关键字参数，供单测注入，CLI 不暴露 --data-dir）；
  日志：同目录 `backend.log`（追加）。
- start 幂等：已运行（pid 存活）直接返回 0，不重复拉起；
- stop：SIGTERM → 最多等 5s → SIGKILL；未运行退出码 3；
- status：运行退出码 0，未运行退出码 3；
- 如实声明：pid 回绕（pidfile 里的 pid 被其他进程复用）是 pidfile 方案的固有
  局限，status 可能误报「运行中」——本工具不做进程名核验（macOS/Linux 通用性
  优先），发现可疑时用 `kill <pid>` 人工核实。

退出码约定：0=成功/已在运行；1=启动失败；2=参数错误；3=未运行。
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

BACKEND_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = BACKEND_DIR / "data"
PIDFILE_NAME = "backend.pid"
LOGFILE_NAME = "backend.log"

STOP_TIMEOUT_S = 5.0
START_PROBE_S = 2.0  # start 后观察窗口：进程秒退（端口占用等）如实报失败


def pidfile_path(data_dir: Path) -> Path:
    return data_dir / PIDFILE_NAME


def logfile_path(data_dir: Path) -> Path:
    return data_dir / LOGFILE_NAME


def read_pid(pid_path: Path) -> int | None:
    """读 pidfile；缺失/非整数/非正 → None（不猜）。"""
    try:
        text = pid_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        pid = int(text)
    except ValueError:
        return None
    return pid if pid > 0 else None


def write_pid(pid_path: Path, pid: int) -> None:
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(f"{pid}\n", encoding="utf-8")


def is_running(pid: int, *, probe: Callable[[int], bool] | None = None) -> bool:
    """pid 是否存活（os.kill(pid, 0) 探测；EPERM=存在但无权信号 → 视为存活）。"""
    if probe is not None:
        return probe(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _default_popen(cmd: list[str], log_path: Path) -> subprocess.Popen[bytes]:
    log = log_path.open("ab")
    return subprocess.Popen(  # noqa: S603 - cmd 由本模块参数拼装
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,  # 脱离终端会话（终端关闭不带走后端）
        cwd=str(BACKEND_DIR),
    )


def cmd_start(
    args: argparse.Namespace,
    data_dir: Path,
    *,
    popen: Callable[[list[str], Path], subprocess.Popen[Any]] = _default_popen,
    running: Callable[[int], bool] = is_running,
) -> int:
    pid_path = pidfile_path(data_dir)
    existing = read_pid(pid_path)
    if existing is not None and running(existing):
        print(f"[pd-service] 已在运行 pid={existing}（pidfile {pid_path}）")
        return 0
    if existing is not None:
        print(f"[pd-service] 清理失效 pidfile（pid={existing} 已不存在）")

    cmd = [
        sys.executable, str(BACKEND_DIR / "run.py"),
        "--port", str(args.port), "--host", args.host,
        "--provider", args.provider,
    ]
    if args.runs_dir:
        cmd += ["--runs-dir", args.runs_dir]
    log_path = logfile_path(data_dir)
    proc = popen(cmd, log_path)
    write_pid(pid_path, proc.pid)

    # 观察窗口：秒退（端口被占/perfeval 导入失败）如实报失败，不留僵尸 pidfile
    deadline = time.monotonic() + START_PROBE_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pid_path.unlink(missing_ok=True)
            print(
                f"[pd-service] 启动失败（退出码 {proc.returncode}），"
                f"日志见 {log_path}",
                file=sys.stderr,
            )
            return 1
        time.sleep(0.1)
    print(
        f"[pd-service] started pid={proc.pid} port={args.port} "
        f"log={log_path} pidfile={pid_path}"
    )
    return 0


def cmd_stop(
    args: argparse.Namespace,
    data_dir: Path,
    *,
    running: Callable[[int], bool] = is_running,
    killer: Callable[[int, int], None] = os.kill,
) -> int:
    del args  # stop 无专属参数；保留形参统一三命令签名
    pid_path = pidfile_path(data_dir)
    pid = read_pid(pid_path)
    if pid is None or not running(pid):
        print("[pd-service] 未在运行")
        pid_path.unlink(missing_ok=True)
        return 3
    try:
        killer(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass  # 恰好在 kill 前退出，按已停止处理
    deadline = time.monotonic() + STOP_TIMEOUT_S
    while time.monotonic() < deadline:
        if not running(pid):
            break
        time.sleep(0.1)
    else:
        killer(pid, signal.SIGKILL)  # 优雅期超时强杀
    pid_path.unlink(missing_ok=True)
    print(f"[pd-service] stopped pid={pid}")
    return 0


def cmd_status(
    args: argparse.Namespace,
    data_dir: Path,
    *,
    running: Callable[[int], bool] = is_running,
) -> int:
    del args
    pid = read_pid(pidfile_path(data_dir))
    if pid is not None and running(pid):
        print(f"[pd-service] running pid={pid}")
        return 0
    print("[pd-service] stopped")
    return 3


def main(
    argv: list[str] | None = None,
    *,
    data_dir: Path | None = None,
    popen: Callable[[list[str], Path], subprocess.Popen[Any]] = _default_popen,
    running: Callable[[int], bool] = is_running,
    killer: Callable[[int, int], None] = os.kill,
) -> int:
    parser = argparse.ArgumentParser(
        prog="service.py", description="perfdog 后端本地常驻管理（start/stop/status）"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="后台拉起 run.py（幂等）")
    start.add_argument("--port", type=int, default=8737)
    start.add_argument("--host", default="127.0.0.1")
    start.add_argument("--provider", default="auto", choices=("auto", "real", "synthetic", "simulator"))
    start.add_argument("--runs-dir", default=None)
    sub.add_parser("stop", help="SIGTERM 停止（5s 超时 SIGKILL）")
    sub.add_parser("status", help="运行状态（退出码 0=运行 / 3=未运行）")
    args = parser.parse_args(argv)

    root = data_dir or DEFAULT_DATA_DIR
    if args.command == "start":
        return cmd_start(args, root, popen=popen, running=running)
    if args.command == "stop":
        return cmd_stop(args, root, running=running, killer=killer)
    return cmd_status(args, root, running=running)


if __name__ == "__main__":
    raise SystemExit(main())
