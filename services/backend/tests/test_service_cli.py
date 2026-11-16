"""Service CLI（R55 最小切片）：start/stop/status 的 pidfile 进程管理逻辑。

进程管理测试用注入替身（popen/running/killer），不真实拉起后端——真实拉起路径
（subprocess.Popen + start_new_session + 日志重定向）属 OS 集成，单测替身覆盖
命令拼装、pidfile 生命周期与退出码；如实声明：真实进程的启停未在单测内验证。
"""

from __future__ import annotations

import signal
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import service as svc  # noqa: E402


def _fake_proc(pid: int, poll_after: int | None = None):
    """popen 替身：pid 固定；poll_after=N 表示第 N 次 poll() 起返回退出码（秒退）。"""
    state = {"polls": 0}

    def popen(cmd: list[str], log_path: Path):
        state["cmd"] = cmd
        state["log_path"] = log_path

        def poll() -> int | None:
            state["polls"] += 1
            if poll_after is not None and state["polls"] >= poll_after:
                return 1
            return None

        return SimpleNamespace(pid=pid, poll=poll, returncode=1)

    return state, popen


def test_status_without_pidfile_exits_3(tmp_path: Path) -> None:
    assert svc.main(["status"], data_dir=tmp_path) == 3


def test_start_writes_pidfile_and_run_command(tmp_path: Path) -> None:
    state, popen = _fake_proc(424242)
    rc = svc.main(["start", "--port", "9001", "--provider", "synthetic"],
                  data_dir=tmp_path, popen=popen, running=lambda pid: False)
    assert rc == 0
    assert svc.read_pid(svc.pidfile_path(tmp_path)) == 424242
    cmd = state["cmd"]
    assert cmd[1:3] == [str(svc.BACKEND_DIR / "run.py"), "--port"]
    assert "--provider" in cmd and "synthetic" in cmd
    assert state["log_path"] == svc.logfile_path(tmp_path)


def test_start_idempotent_when_already_running(tmp_path: Path) -> None:
    svc.write_pid(svc.pidfile_path(tmp_path), 999)
    spawned: list[list[str]] = []

    def popen(cmd: list[str], log_path: Path):
        spawned.append(cmd)
        raise AssertionError("已在运行时不得重复拉起")

    rc = svc.main(["start"], data_dir=tmp_path, popen=popen, running=lambda pid: True)
    assert rc == 0
    assert spawned == []
    assert svc.read_pid(svc.pidfile_path(tmp_path)) == 999


def test_start_recovers_stale_pidfile(tmp_path: Path) -> None:
    """pidfile 里的 pid 已不存在 → 清理并重新拉起（不误判已在运行）。"""
    svc.write_pid(svc.pidfile_path(tmp_path), 777777)
    state, popen = _fake_proc(1001)
    rc = svc.main(["start"], data_dir=tmp_path, popen=popen, running=lambda pid: False)
    assert rc == 0
    assert svc.read_pid(svc.pidfile_path(tmp_path)) == 1001
    assert state["cmd"][1].endswith("run.py")


def test_start_failure_removes_pidfile(tmp_path: Path) -> None:
    """进程秒退（如端口被占）→ 退出码 1 且不留僵尸 pidfile（启动失败如实暴露）。"""
    _state, popen = _fake_proc(424242, poll_after=1)
    rc = svc.main(["start"], data_dir=tmp_path, popen=popen, running=lambda pid: False)
    assert rc == 1
    assert not svc.pidfile_path(tmp_path).exists()


def test_stop_terminates_and_removes_pidfile(tmp_path: Path) -> None:
    """SIGTERM 后进程退出 → 正常收尾（不发 SIGKILL、pidfile 清除）。"""
    svc.write_pid(svc.pidfile_path(tmp_path), 5000)
    kills: list[tuple[int, int]] = []
    state = {"terminated": False}

    def killer(pid: int, sig: int) -> None:
        kills.append((pid, sig))
        state["terminated"] = True

    def running(pid: int) -> bool:
        return not state["terminated"]  # SIGTERM 即退出

    rc = svc.main(["stop"], data_dir=tmp_path, running=running, killer=killer)
    assert rc == 0
    assert kills == [(5000, signal.SIGTERM)]  # 优雅退出，只发一次 SIGTERM
    assert not svc.pidfile_path(tmp_path).exists()


def test_stop_esclates_to_sigkill_after_grace(tmp_path: Path) -> None:
    """SIGTERM 后 5s 内未退出 → SIGKILL（优雅期超时强杀）。"""
    svc.write_pid(svc.pidfile_path(tmp_path), 5001)
    kills: list[tuple[int, int]] = []

    def killer(pid: int, sig: int) -> None:
        kills.append((pid, sig))

    rc = svc.main(
        ["stop"], data_dir=tmp_path,
        running=lambda pid: True,  # 恒「存活」→ 走满优雅期
        killer=killer,
    )
    assert rc == 0
    assert kills == [(5001, signal.SIGTERM), (5001, signal.SIGKILL)]


def test_stop_when_not_running_exits_3(tmp_path: Path) -> None:
    rc = svc.main(["stop"], data_dir=tmp_path, running=lambda pid: False)
    assert rc == 3


def test_read_pid_garbage_is_none(tmp_path: Path) -> None:
    pid_path = svc.pidfile_path(tmp_path)
    for content in ("", "abc", "-3", "0", "12 34"):
        pid_path.write_text(content, encoding="utf-8")
        assert svc.read_pid(pid_path) is None, content
    pid_path.write_text("4242\n", encoding="utf-8")
    assert svc.read_pid(pid_path) == 4242
