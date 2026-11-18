"""Lifecycle of the bundled ADB server in the Windows Electron application."""

from __future__ import annotations

import ctypes
import os
import socket
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from ctypes import wintypes

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_time", ctypes.c_int64),
        ("per_job_time", ctypes.c_int64),
        ("flags", wintypes.DWORD),
        ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t),
        ("active_process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority_class", wintypes.DWORD),
        ("scheduling_class", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimitInformation),
        ("io_counters", ctypes.c_uint64 * 6),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


@contextmanager
def _kill_on_close_job() -> Iterator[tuple[Callable[[int, int], int], int]]:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = _ExtendedLimitInformation()
        info.basic.flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(
            job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        yield kernel.AssignProcessToJobObject, job
    finally:
        kernel.CloseHandle(job)


def _pick_port() -> int:
    configured = os.environ.get("PERFEVAL_ADB_SERVER_PORT")
    port = int(configured) if configured else 0
    if configured and (not 1024 <= port <= 65535 or port == 5037):
        raise ValueError("PERFEVAL_ADB_SERVER_PORT must be an unoccupied port above 1023, excluding 5037")
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", port))
        return listener.getsockname()[1]


def _wait_until_listening(proc: subprocess.Popen[bytes], port: int) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Bundled ADB server exited during startup (code {proc.returncode})")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError("Bundled ADB server failed to listen on its private loopback port")


@contextmanager
def bundled_adb_server() -> Iterator[None]:
    port = _pick_port()
    adb = os.environ["PERFEVAL_ADB_PATH"]
    proc = None
    assigned = False
    try:
        with _kill_on_close_job() as (assign, job):
            proc = subprocess.Popen(
                [adb, "-L", f"tcp:localhost:{port}", "nodaemon", "server"],
                stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
                env={**os.environ, "ADB_REJECT_KILL_SERVER": "1"},
            )
            # CPython's child handle preserves process identity even if its PID is reused.
            if not assign(job, int(proc._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
            assigned = True
            _wait_until_listening(proc, port)
            # ADB treats the numeric host as remote and therefore never auto-starts a
            # detached server if this managed child stops unexpectedly.
            os.environ["ADB_SERVER_SOCKET"] = f"tcp:127.0.0.1:{port}"
            yield
    finally:
        if proc is not None:
            if not assigned and proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=5)
