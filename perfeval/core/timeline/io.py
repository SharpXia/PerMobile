from __future__ import annotations

import json
from dataclasses import dataclass
from typing import IO, Any, Iterable, Iterator, Mapping, Optional, TextIO

from ..schema.errors import ValidationError
from .models import TIMELINE_SCHEMA_VERSION, EventSource, TimelineEvent


@dataclass
class TimelineRowError(ValidationError):
    raw_line: Optional[str] = None

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base} | raw={self.raw_line!r}" if self.raw_line is not None else base


def _event_from_payload(
    payload: Any,
    *,
    line: int,
    raw_line: str,
) -> TimelineEvent:
    seq_value = None
    if isinstance(payload, Mapping):
        raw_seq = payload.get("seq")
        if isinstance(raw_seq, int) and not isinstance(raw_seq, bool):
            seq_value = raw_seq
    try:
        return TimelineEvent.from_dict(payload)
    except ValidationError as exc:
        raise TimelineRowError(
            exc.message,
            line=line,
            seq=getattr(exc, "seq", None) if getattr(exc, "seq", None) is not None else seq_value,
            field=exc.field,
            raw_line=raw_line.rstrip("\r\n"),
        ) from exc


class TimelineWriter:
    def __init__(self, sink: TextIO, *, flush_each_line: bool = False) -> None:
        self._sink = sink
        self._flush_each_line = flush_each_line
        self._line_no = 0

    @property
    def line_no(self) -> int:
        return self._line_no

    def write(self, event: TimelineEvent | Mapping[str, Any]) -> TimelineEvent:
        if not isinstance(event, TimelineEvent):
            event = TimelineEvent.from_dict(event)
        self._line_no += 1
        payload = event.to_dict()
        self._sink.write(json.dumps(payload, separators=(",", ":"), sort_keys=False, allow_nan=False))
        self._sink.write("\n")
        if self._flush_each_line:
            self._sink.flush()
        return event

    def write_many(self, events: Iterable[TimelineEvent | Mapping[str, Any]]) -> list[TimelineEvent]:
        written: list[TimelineEvent] = []
        for event in events:
            written.append(self.write(event))
        return written


class TimelineReader:
    def __init__(self, *, tolerate_truncated_last_line: bool = True) -> None:
        self._tolerate_truncated_last_line = tolerate_truncated_last_line

    def iter_events(self, source: TextIO) -> Iterator[TimelineEvent]:
        for line_no, raw_line in enumerate(source, start=1):
            line = raw_line.rstrip("\r\n")
            if not line.strip():
                raise TimelineRowError("blank timeline row", line=line_no, raw_line=raw_line)
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                if self._tolerate_truncated_last_line and not raw_line.endswith(("\n", "\r")):
                    return
                raise TimelineRowError(
                    f"invalid JSON: {exc.msg}",
                    line=line_no,
                    raw_line=raw_line,
                ) from exc
            yield _event_from_payload(payload, line=line_no, raw_line=raw_line)

    def read_all(self, source: TextIO) -> list[TimelineEvent]:
        return list(self.iter_events(source))


def write_timeline(events: Iterable[TimelineEvent | Mapping[str, Any]], sink: TextIO, *, flush_each_line: bool = False) -> list[TimelineEvent]:
    writer = TimelineWriter(sink, flush_each_line=flush_each_line)
    return writer.write_many(events)


def read_timeline(source: TextIO, *, tolerate_truncated_last_line: bool = True) -> list[TimelineEvent]:
    return TimelineReader(tolerate_truncated_last_line=tolerate_truncated_last_line).read_all(source)
