from __future__ import annotations

import builtins
import json
import sys
from argparse import ArgumentParser, Namespace
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, TextIO


SCHEMA_VERSION = "perfeval.cli.event.v1"
EVENT_TYPES = frozenset({"devices", "log", "manual", "phase", "result"})


class CliEventError(ValueError):
    """Raised when a machine-readable CLI event violates the v1 contract."""


@dataclass(frozen=True)
class CliEvent:
    type: str
    command: str
    data: dict[str, Any]
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "type": self.type,
            "command": self.command,
            "data": self.data,
        }


@dataclass(frozen=True)
class _EventContext:
    command: str
    output: TextIO


_ACTIVE_CONTEXT: ContextVar[_EventContext | None] = ContextVar(
    "perfeval_cli_event_context",
    default=None,
)


def add_gui_events_argument(parser: ArgumentParser) -> None:
    parser.add_argument(
        "--gui-events",
        action="store_true",
        help="Write the opt-in PerfEval GUI JSONL event contract to stdout.",
    )


def gui_events_enabled(args: Namespace) -> bool:
    return bool(getattr(args, "gui_events", False))


def emit_event(event_type: str, data: Mapping[str, Any]) -> bool:
    context = _ACTIVE_CONTEXT.get()
    if context is None:
        return False
    event = CliEvent(type=event_type, command=context.command, data=dict(data))
    context.output.write(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
    context.output.flush()
    return True


def emit_result(*, status: str, exit_code: int, **data: Any) -> bool:
    if isinstance(sys.stdout, _JsonlStdout):
        sys.stdout.finish()
    return emit_event(
        "result",
        {
            "status": status,
            "exitCode": exit_code,
            "outputRoot": None,
            "runDir": None,
            "report": None,
            "summary": None,
            "artifacts": [],
            **data,
        },
    )


def emit_phase(
    phase_id: str,
    state: str,
    label: str,
    *,
    repeat_index: int | None = None,
    repeat_count: int | None = None,
    variant_id: str | None = None,
    detail: str | None = None,
) -> bool:
    data: dict[str, Any] = {"id": phase_id, "state": state, "label": label}
    for key, value in (
        ("repeatIndex", repeat_index),
        ("repeatCount", repeat_count),
        ("variantId", variant_id),
        ("detail", detail),
    ):
        if value is not None:
            data[key] = value
    return emit_event("phase", data)


def parse_event_line(line: str) -> CliEvent:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise CliEventError(f"invalid JSONL event: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise CliEventError("CLI event must be a JSON object")
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise CliEventError(f"unsupported CLI event schema: {payload.get('schemaVersion')!r}")
    event_type = payload.get("type")
    if event_type not in EVENT_TYPES:
        raise CliEventError(f"unsupported CLI event type: {event_type!r}")
    command = payload.get("command")
    if not isinstance(command, str) or not command:
        raise CliEventError("CLI event command must be a non-empty string")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise CliEventError("CLI event data must be an object")
    _validate_event_data(event_type, data)
    return CliEvent(type=event_type, command=command, data=data)


def _validate_event_data(event_type: str, data: dict[str, Any]) -> None:
    if event_type == "log":
        if data.get("stream") not in {"stdout", "stderr"} or not isinstance(data.get("text"), str):
            raise CliEventError("log data requires stream=stdout|stderr and string text")
        return
    if event_type == "manual":
        if not isinstance(data.get("prompt"), str) or not data["prompt"]:
            raise CliEventError("manual data requires a non-empty prompt")
        if data.get("input") != "enter":
            raise CliEventError("manual data input must be 'enter'")
        timeout = data.get("timeoutSeconds")
        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0
        ):
            raise CliEventError("manual timeoutSeconds must be null or a positive number")
        return
    if event_type == "phase":
        if not isinstance(data.get("id"), str) or not data["id"]:
            raise CliEventError("phase data requires a non-empty id")
        if data.get("state") not in {"started", "completed", "skipped", "failed"}:
            raise CliEventError("phase state must be started, completed, skipped, or failed")
        if not isinstance(data.get("label"), str) or not data["label"]:
            raise CliEventError("phase data requires a non-empty label")
        for key in ("repeatIndex", "repeatCount"):
            value = data.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                raise CliEventError(f"phase {key} must be a positive integer")
        if data.get("repeatIndex") is not None and data.get("repeatCount") is not None:
            if data["repeatIndex"] > data["repeatCount"]:
                raise CliEventError("phase repeatIndex must not exceed repeatCount")
        for key in ("variantId", "detail"):
            if data.get(key) is not None and not isinstance(data[key], str):
                raise CliEventError(f"phase {key} must be null or a string")
        return
    if event_type == "result":
        required = {"status", "exitCode", "outputRoot", "runDir", "report", "summary", "artifacts"}
        missing = sorted(required.difference(data))
        if missing:
            raise CliEventError(f"result data missing required fields: {', '.join(missing)}")
        if not isinstance(data["status"], str) or not data["status"]:
            raise CliEventError("result status must be a non-empty string")
        if isinstance(data["exitCode"], bool) or not isinstance(data["exitCode"], int):
            raise CliEventError("result exitCode must be an integer")
        for key in ("outputRoot", "runDir", "report", "summary"):
            if data[key] is not None and not isinstance(data[key], str):
                raise CliEventError(f"result {key} must be null or a string")
        if not isinstance(data["artifacts"], list) or not all(
            isinstance(path, str) for path in data["artifacts"]
        ):
            raise CliEventError("result artifacts must be an array of strings")
        return

    android = data.get("android")
    tools = data.get("tools")
    if not isinstance(android, dict) or not isinstance(tools, dict):
        raise CliEventError("devices data requires tools and android objects")
    if not isinstance(android.get("status"), str) or not isinstance(android.get("devices"), list):
        raise CliEventError("devices android data requires status and devices")
    for device in android["devices"]:
        if (
            not isinstance(device, dict)
            or not isinstance(device.get("serial"), str)
            or not isinstance(device.get("state"), str)
        ):
            raise CliEventError("each Android device requires string serial and state")


class _JsonlStdout:
    """Turn unexpected legacy stdout writes into JSONL log/manual events."""

    def __init__(self, wrapped: TextIO) -> None:
        self._wrapped = wrapped
        self._buffer = ""
        self._last_line = ""
        self._last_line_was_manual = False

    @property
    def encoding(self) -> str | None:
        return getattr(self._wrapped, "encoding", None)

    @property
    def errors(self) -> str | None:
        return getattr(self._wrapped, "errors", None)

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            text = str(text)
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._last_line = line
            self._last_line_was_manual = False
            emit_event("log", {"stream": "stdout", "text": line + "\n"})
        return len(text)

    def flush(self) -> None:
        # Existing manual prompts deliberately flush without a newline before
        # blocking on stdin. Preserve that timing as an explicit manual event.
        if self._buffer:
            prompt = self._buffer
            self._buffer = ""
            self._last_line = prompt
            self._last_line_was_manual = True
            emit_event(
                "manual",
                {
                    "prompt": prompt,
                    "timeoutSeconds": None,
                    "input": "enter",
                },
            )
        self._wrapped.flush()

    def finish(self) -> None:
        if self._buffer:
            emit_event("log", {"stream": "stdout", "text": self._buffer})
            self._buffer = ""
        self._wrapped.flush()

    def take_input_prompt(self, explicit_prompt: str) -> tuple[str, bool]:
        if explicit_prompt:
            return explicit_prompt, False
        prompt = self._last_line
        already_emitted = self._last_line_was_manual
        self._last_line = ""
        self._last_line_was_manual = False
        return prompt, already_emitted

    def isatty(self) -> bool:
        return bool(self._wrapped.isatty())

    def fileno(self) -> int:
        return self._wrapped.fileno()

    def writable(self) -> bool:
        return True


@contextmanager
def gui_event_stream(command: str, *, enabled: bool) -> Iterator[None]:
    if not enabled:
        yield
        return

    raw_stdout = sys.stdout
    proxy = _JsonlStdout(raw_stdout)
    original_input = builtins.input
    token = _ACTIVE_CONTEXT.set(_EventContext(command=command, output=raw_stdout))

    def event_input(prompt: str = "") -> str:
        resolved_prompt, already_emitted = proxy.take_input_prompt(prompt)
        if resolved_prompt and not already_emitted:
            emit_event(
                "manual",
                {
                    "prompt": resolved_prompt,
                    "timeoutSeconds": None,
                    "input": "enter",
                },
            )
        return original_input("")

    sys.stdout = proxy  # type: ignore[assignment]
    builtins.input = event_input
    try:
        yield
    finally:
        proxy.finish()
        builtins.input = original_input
        sys.stdout = raw_stdout
        _ACTIVE_CONTEXT.reset(token)
