"""ADB logcat pipes whose reads can be interrupted by a session stop."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable, Iterator

from perfeval.collectors.android.adb import ADB_CREATION_FLAGS, AdbClient


class AdbLogcatLines(Iterator[str]):
    def __init__(
        self,
        process: subprocess.Popen[str],
        on_failure: Callable[[int, str], None],
    ) -> None:
        self._process = process
        self._lines = iter(process.stdout if process.stdout is not None else ())
        self._on_failure = on_failure
        self._lock = threading.Lock()
        self._terminated = False
        self._closed = False

    def __next__(self) -> str:
        if self._closed:
            raise StopIteration
        try:
            return next(self._lines)
        except StopIteration:
            self.close()
            raise

    def _terminate(self) -> None:
        if not self._terminated and self._process.poll() is None:
            self._process.terminate()
            self._terminated = True
            try:
                self._process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()

    def stop(self) -> None:
        """Interrupt a reader waiting for the next logcat line."""
        with self._lock:
            self._terminate()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._terminate()

        process = self._process
        try:
            if not self._terminated and process.returncode not in (0, None):
                stderr_tail = ""
                if process.stderr is not None:
                    try:
                        stderr_tail = (process.stderr.read() or "").strip()[-200:]
                    except OSError:
                        pass
                self._on_failure(process.returncode, stderr_tail)
        finally:
            for pipe in (process.stdout, process.stderr):
                close = getattr(pipe, "close", None)
                if close is not None:
                    close()


def open_adb_logcat(
    serial: str,
    log_format: str,
    on_failure: Callable[[int, str], None],
    *,
    adb_path: str | None = None,
) -> AdbLogcatLines | None:
    command = AdbClient(adb_path).command("logcat", "-T", "1", "-v", log_format, serial=serial)
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=ADB_CREATION_FLAGS,
        )
    except (OSError, ValueError):
        return None
    return AdbLogcatLines(process, on_failure)
