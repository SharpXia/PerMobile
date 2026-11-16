from __future__ import annotations

import codecs
import json
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from perfeval.gui.models.result import ResultDescriptor, TaskResult, classify_outcome
from perfeval.gui.models.task import (
    LogChunk,
    LogStream,
    ManualCapability,
    ManualRequest,
    ProcessStartInfo,
    TaskSpec,
    TaskPhaseEvent,
    TaskPhaseState,
    TaskState,
)
from perfeval.gui.services.result_locator import ResultLocator
from perfeval.gui.services.experiment_cache import ExperimentCacheService

try:
    from PySide6.QtCore import (
        QObject,
        QProcess,
        QProcessEnvironment,
        QTimer,
        Signal,
    )
except ImportError:  # Core CLI remains importable without the optional GUI extra.
    QObject = object  # type: ignore[assignment,misc]
    QProcess = None  # type: ignore[assignment,misc]
    QProcessEnvironment = None  # type: ignore[assignment,misc]
    QTimer = None  # type: ignore[assignment,misc]

    class Signal:  # type: ignore[no-redef]
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass


_SCHEMA_VERSION = "perfeval.cli.event.v1"


class IncrementalUtf8Decoder:
    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.replacement_count = 0

    def decode(self, payload: bytes, *, final: bool = False) -> tuple[str, int]:
        text = self._decoder.decode(payload, final=final)
        replacements = text.count("\ufffd")
        self.replacement_count += replacements
        return text, replacements


class CliEventStreamParser:
    """Incrementally split and validate the opt-in CLI JSONL stream."""

    def __init__(self) -> None:
        self.buffer = ""
        self.structured_mode: bool | None = None

    def feed(self, text: str, *, final: bool = False) -> tuple[list[Mapping[str, Any]], list[str]]:
        self.buffer += text
        events: list[Mapping[str, Any]] = []
        legacy: list[str] = []

        if self.structured_mode is None:
            stripped = self.buffer.lstrip()
            if stripped and not stripped.startswith("{"):
                self.structured_mode = False
            elif "\n" not in self.buffer and not final:
                return events, legacy

        if self.structured_mode is False:
            if self.buffer:
                legacy.append(self.buffer)
                self.buffer = ""
            return events, legacy

        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            event = _parse_event_line(line)
            if event is None:
                if self.structured_mode is None:
                    self.structured_mode = False
                    legacy.append(line + "\n" + self.buffer)
                    self.buffer = ""
                    return events, legacy
                legacy.append(line + "\n")
            else:
                self.structured_mode = True
                events.append(event)

        if final and self.buffer:
            event = _parse_event_line(self.buffer)
            if event is not None:
                self.structured_mode = True
                events.append(event)
            else:
                legacy.append(self.buffer)
            self.buffer = ""
        return events, legacy


class ProcessRunner(QObject):
    """One-task QProcess runner with incremental decoding and bounded cancel."""

    started = Signal(object)
    stdout_chunk = Signal(object)
    stderr_chunk = Signal(object)
    manual_requested = Signal(object)
    phase_changed = Signal(object)
    state_changed = Signal(object)
    finished = Signal(object)
    launch_failed = Signal(object)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        process_factory: Callable[[QObject], QProcess] | None = None,
        result_locator: ResultLocator | None = None,
        log_directory: str | Path | None = None,
        cancel_grace_ms: int = 3000,
    ) -> None:
        if QProcess is None:
            raise RuntimeError("PySide6 is required to use ProcessRunner")
        super().__init__(parent)
        if cancel_grace_ms < 0:
            raise ValueError("cancel_grace_ms must be non-negative")
        self._process_factory = process_factory or (lambda owner: QProcess(owner))
        self._result_locator = result_locator or ResultLocator()
        self._configured_log_directory = Path(log_directory).expanduser() if log_directory else None
        self._cancel_grace_ms = cancel_grace_ms
        self._process: QProcess | None = None
        self._task: TaskSpec | None = None
        self._state = TaskState.IDLE
        self._started_at: datetime | None = None
        self._stdout_decoder = IncrementalUtf8Decoder()
        self._stderr_decoder = IncrementalUtf8Decoder()
        self._event_parser = CliEventStreamParser()
        self._sequence = 0
        self._stdout_parts: list[str] = []
        self._stderr_parts: list[str] = []
        self._stdout_log: TextIO | None = None
        self._stderr_log: TextIO | None = None
        self._stdout_log_path: Path | None = None
        self._stderr_log_path: Path | None = None
        self._result_envelope: Mapping[str, Any] | None = None
        self._structured_status: str | None = None
        self._structured_exit_code: int | None = None
        self._user_cancelled = False
        self._process_error: str | None = None
        self._finalized = False
        self._active_manual_key: str | None = None

    @property
    def state(self) -> TaskState:
        return self._state

    @property
    def is_active(self) -> bool:
        return self._process is not None and self._state in {
            TaskState.STARTING,
            TaskState.RUNNING,
            TaskState.WAITING_FOR_USER,
            TaskState.CANCELLING,
        }

    @property
    def current_task(self) -> TaskSpec | None:
        return self._task

    @property
    def started_at(self) -> datetime | None:
        return self._started_at

    def start(self, task: TaskSpec) -> None:
        if self.is_active:
            raise RuntimeError("a task is already running")
        self._reset_for_task(task)
        process = self._process_factory(self)
        self._process = process
        process.setProcessChannelMode(QProcess.SeparateChannels)
        process.setProgram(task.program)
        process.setArguments(list(task.arguments))
        process.setWorkingDirectory(str(task.working_directory))
        if task.environment:
            environment = QProcessEnvironment.systemEnvironment()
            for key, value in task.environment.items():
                environment.insert(str(key), str(value))
            process.setProcessEnvironment(environment)
        process.started.connect(self._on_started)
        process.readyReadStandardOutput.connect(self._read_stdout)
        process.readyReadStandardError.connect(self._read_stderr)
        process.errorOccurred.connect(self._on_error)
        process.finished.connect(self._on_finished)
        self._set_state(TaskState.STARTING)
        process.start()

    def send_input(self, text: str = "\n") -> bool:
        process = self._process
        if process is None or process.state() == QProcess.NotRunning:
            return False
        payload = text.encode("utf-8")
        if process.write(payload) < 0:
            return False
        self._active_manual_key = None
        if self._state == TaskState.WAITING_FOR_USER:
            self._set_state(TaskState.RUNNING)
        return True

    def continue_manual(self) -> bool:
        return self.send_input("\n")

    def cancel(self) -> bool:
        process = self._process
        if process is None or process.state() == QProcess.NotRunning or self._finalized:
            return False
        if self._state == TaskState.CANCELLING:
            return False
        self._user_cancelled = True
        self._set_state(TaskState.CANCELLING)
        process.terminate()
        QTimer.singleShot(self._cancel_grace_ms, self._kill_if_running)
        return True

    def kill_now(self) -> bool:
        process = self._process
        if process is None or process.state() == QProcess.NotRunning:
            return False
        self._user_cancelled = True
        if self._state != TaskState.CANCELLING:
            self._set_state(TaskState.CANCELLING)
        process.kill()
        return True

    def _reset_for_task(self, task: TaskSpec) -> None:
        self._close_logs()
        self._task = task
        self._state = TaskState.IDLE
        self._started_at = None
        self._stdout_decoder = IncrementalUtf8Decoder()
        self._stderr_decoder = IncrementalUtf8Decoder()
        self._event_parser = CliEventStreamParser()
        self._sequence = 0
        self._stdout_parts = []
        self._stderr_parts = []
        self._result_envelope = None
        self._structured_status = None
        self._structured_exit_code = None
        self._user_cancelled = False
        self._process_error = None
        self._finalized = False
        self._active_manual_key = None
        self._open_logs(task)

    def _open_logs(self, task: TaskSpec) -> None:
        directory = ExperimentCacheService.log_directory(task)
        if directory is None:
            directory = self._configured_log_directory
        if directory is None:
            directory = task.working_directory / ".perfeval" / "gui-logs"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            self._stdout_log_path = directory / f"{task.task_id}.stdout.log"
            self._stderr_log_path = directory / f"{task.task_id}.stderr.log"
            self._stdout_log = self._stdout_log_path.open("w", encoding="utf-8", errors="replace")
            self._stderr_log = self._stderr_log_path.open("w", encoding="utf-8", errors="replace")
        except OSError as exc:
            self._stdout_log = None
            self._stderr_log = None
            self._stdout_log_path = None
            self._stderr_log_path = None
            self._process_error = f"log_open_failed: {exc}"

    def _on_started(self) -> None:
        process = self._process
        if process is None:
            return
        self._started_at = datetime.now(timezone.utc)
        self._set_state(TaskState.RUNNING)
        self.started.emit(ProcessStartInfo(pid=int(process.processId()), started_at=self._started_at))

    def _read_stdout(self) -> None:
        process = self._process
        if process is None:
            return
        payload = bytes(process.readAllStandardOutput())
        text, replacements = self._stdout_decoder.decode(payload)
        if text:
            self._handle_stdout_text(text, replacements=replacements)

    def _read_stderr(self) -> None:
        process = self._process
        if process is None:
            return
        payload = bytes(process.readAllStandardError())
        text, replacements = self._stderr_decoder.decode(payload)
        if text:
            self._emit_chunk(LogStream.STDERR, text, replacements)

    def _handle_stdout_text(self, text: str, *, replacements: int = 0, final: bool = False) -> None:
        events, legacy_parts = self._event_parser.feed(text, final=final)
        for event in events:
            self._handle_event(event)
        replacement_budget = replacements
        for legacy in legacy_parts:
            part_replacements = min(replacement_budget, legacy.count("\ufffd"))
            replacement_budget -= part_replacements
            self._emit_chunk(LogStream.STDOUT, legacy, part_replacements)
            self._detect_text_manual(legacy)

    def _handle_event(self, event: Mapping[str, Any]) -> None:
        event_type = event.get("type")
        data = event.get("data")
        if not isinstance(data, Mapping):
            return
        if event_type == "log":
            text = data.get("text")
            if isinstance(text, str) and text:
                stream = LogStream.STDERR if data.get("stream") == "stderr" else LogStream.STDOUT
                self._emit_chunk(stream, text, text.count("\ufffd"))
            return
        if event_type == "manual":
            if data.get("input") not in {None, "enter"}:
                return
            prompt = data.get("prompt")
            if isinstance(prompt, str) and prompt:
                timeout = data.get("timeoutSeconds")
                timeout_value = float(timeout) if isinstance(timeout, (int, float)) and timeout > 0 else None
                request = ManualRequest(
                    prompt=prompt,
                    timeout_seconds=timeout_value,
                    source="event",
                    event_id=str(data.get("id")) if data.get("id") is not None else None,
                )
                self._emit_manual(request)
            return
        if event_type == "phase":
            phase_id = data.get("id")
            state = data.get("state")
            label = data.get("label")
            if isinstance(phase_id, str) and isinstance(label, str):
                try:
                    phase_state = TaskPhaseState(str(state))
                except ValueError:
                    return
                self.phase_changed.emit(
                    TaskPhaseEvent(
                        phase_id=phase_id,
                        state=phase_state,
                        label=label,
                        repeat_index=_positive_int_or_none(data.get("repeatIndex")),
                        repeat_count=_positive_int_or_none(data.get("repeatCount")),
                        variant_id=str(data["variantId"]) if isinstance(data.get("variantId"), str) else None,
                        detail=str(data["detail"]) if isinstance(data.get("detail"), str) else None,
                    )
                )
            return
        if event_type == "result":
            self._result_envelope = event
            status = data.get("status")
            self._structured_status = str(status) if status is not None else None
            structured_exit = data.get("exitCode")
            if isinstance(structured_exit, int) and not isinstance(structured_exit, bool):
                self._structured_exit_code = structured_exit

    def _emit_chunk(self, stream: LogStream, text: str, replacements: int) -> None:
        self._sequence += 1
        chunk = LogChunk(
            sequence=self._sequence,
            stream=stream,
            text=text,
            monotonic_ns=time.monotonic_ns(),
            replacement_count=replacements,
        )
        if stream == LogStream.STDOUT:
            self._stdout_parts.append(text)
            self._write_log(self._stdout_log, text)
            self.stdout_chunk.emit(chunk)
        else:
            self._stderr_parts.append(text)
            self._write_log(self._stderr_log, text)
            self.stderr_chunk.emit(chunk)

    @staticmethod
    def _write_log(handle: TextIO | None, text: str) -> None:
        if handle is None:
            return
        try:
            handle.write(text)
            handle.flush()
        except OSError:
            pass

    def _detect_text_manual(self, text: str) -> None:
        task = self._task
        if task is None or task.manual_capability == ManualCapability.NONE:
            return
        combined = ("".join(self._stdout_parts))[-4096:]
        prompt = _manual_prompt_from_text(combined)
        if prompt:
            timeout = _manual_timeout_from_text(prompt)
            self._emit_manual(
                ManualRequest(prompt=prompt, timeout_seconds=timeout, source="text")
            )

    def _emit_manual(self, request: ManualRequest) -> None:
        key = request.event_id or request.prompt
        if key == self._active_manual_key:
            return
        self._active_manual_key = key
        if self._state == TaskState.RUNNING:
            self._set_state(TaskState.WAITING_FOR_USER)
        self.manual_requested.emit(request)

    def _on_error(self, error: Any) -> None:
        process = self._process
        message = process.errorString() if process is not None else str(error)
        self._process_error = message
        if process is not None and error == QProcess.FailedToStart:
            self.launch_failed.emit(message)
            self._finalize(exit_code=None, launch_error=True)

    def _on_finished(self, exit_code: int, _exit_status: Any) -> None:
        self._finalize(exit_code=int(exit_code), launch_error=False)

    def _finalize(self, *, exit_code: int | None, launch_error: bool) -> None:
        if self._finalized:
            return
        self._finalized = True
        self._read_stdout()
        self._read_stderr()
        stdout_tail, stdout_replacements = self._stdout_decoder.decode(b"", final=True)
        if stdout_tail:
            self._handle_stdout_text(stdout_tail, replacements=stdout_replacements)
        else:
            self._handle_stdout_text("", final=True)
        stderr_tail, stderr_replacements = self._stderr_decoder.decode(b"", final=True)
        if stderr_tail:
            self._emit_chunk(LogStream.STDERR, stderr_tail, stderr_replacements)
        ended_at = datetime.now(timezone.utc)
        task = self._task
        if task is None:
            return
        located = self._result_locator.locate(
            task,
            envelope=self._result_envelope,
            stdout_text="".join(self._stdout_parts),
            started_at=self._started_at,
        )
        descriptors = list(located.descriptors)
        descriptors.extend(self._log_descriptors(task))
        effective_exit_code = exit_code
        if effective_exit_code in {None, 0} and self._structured_exit_code not in {None, 0}:
            effective_exit_code = self._structured_exit_code
        outcome = classify_outcome(
            effective_exit_code,
            structured_status=self._structured_status,
            launch_error=launch_error,
            user_cancelled=self._user_cancelled,
        )
        result = TaskResult(
            task_id=task.task_id,
            task_type=task.task_type,
            outcome=outcome,
            exit_code=exit_code,
            process_error=self._process_error,
            stdout_log_path=self._stdout_log_path,
            stderr_log_path=self._stderr_log_path,
            results=tuple(descriptors),
            started_at=self._started_at,
            ended_at=ended_at,
            user_cancelled=self._user_cancelled,
            structured_status=self._structured_status,
            metadata={
                "result_not_found": located.result_not_found,
                "scanned_entries": located.scanned_entries,
                "stdout_replacement_count": self._stdout_decoder.replacement_count,
                "stderr_replacement_count": self._stderr_decoder.replacement_count,
                "result_envelope": dict(self._result_envelope or {}),
                "structured_exit_code": self._structured_exit_code,
            },
        )
        self._active_manual_key = None
        self._set_state(TaskState.COMPLETED)
        self._close_logs()
        self.finished.emit(result)

    def _log_descriptors(self, task: TaskSpec) -> list[ResultDescriptor]:
        from perfeval.gui.models.result import ResultType, ResultViewer

        values: list[ResultDescriptor] = []
        for label, path in (("stdout 日志", self._stdout_log_path), ("stderr 日志", self._stderr_log_path)):
            if path is not None:
                values.append(
                    ResultDescriptor(
                        type=ResultType.LOG,
                        label=label,
                        location=str(path),
                        exists=path.exists(),
                        viewer=ResultViewer.SYSTEM,
                        related_task_id=task.task_id,
                    )
                )
        return values

    def _kill_if_running(self) -> None:
        process = self._process
        if process is not None and process.state() != QProcess.NotRunning:
            process.kill()

    def _set_state(self, state: TaskState) -> None:
        if state == self._state:
            return
        self._state = state
        self.state_changed.emit(state)

    def _close_logs(self) -> None:
        for handle in (self._stdout_log, self._stderr_log):
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
        self._stdout_log = None
        self._stderr_log = None


def _parse_event_line(line: str) -> Mapping[str, Any] | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, Mapping):
        return None
    if payload.get("schemaVersion") != _SCHEMA_VERSION:
        return None
    if payload.get("type") not in {"devices", "manual", "phase", "result", "log"}:
        return None
    if not isinstance(payload.get("command"), str) or not isinstance(payload.get("data"), Mapping):
        return None
    return payload


def _positive_int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _manual_prompt_from_text(buffer: str) -> str | None:
    lines = buffer.replace("\r", "\n").split("\n")
    candidates = [line.strip() for line in lines[-4:] if line.strip()]
    for candidate in reversed(candidates):
        folded = candidate.casefold()
        if "按 enter" in folded or "press enter" in folded:
            return candidate[-1000:]
    return None


def _manual_timeout_from_text(prompt: str) -> float | None:
    import re

    match = re.search(r"(?:最长|max(?:imum)?|within)\s*([0-9]+(?:\.[0-9]+)?)\s*s", prompt, re.IGNORECASE)
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None
