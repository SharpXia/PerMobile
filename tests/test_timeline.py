from __future__ import annotations

import json
from io import StringIO

import pytest

from perfeval.core.timeline import (
    EventSource,
    TimelineEvent,
    TimelineReader,
    TimelineRowError,
    TimelineWriter,
    read_timeline,
    write_timeline,
)
from perfeval.core.schema import ValidationError


def make_event(seq: int, *, duration_ns: int | None = None) -> TimelineEvent:
    return TimelineEvent.new(
        seq=seq,
        run_id="run-1",
        event_id=f"evt-{seq}",
        type="interval" if duration_ns is not None else "instant",
        name="app.launch",
        ts_monotonic_ns=1_000_000_000 + seq,
        duration_ns=duration_ns,
        source=EventSource(platform="android", collector="host.adb", evidence_level="L1"),
        data={"label": "launch"},
    )


def test_timeline_writer_reader_round_trip() -> None:
    sink = StringIO()
    writer = TimelineWriter(sink, flush_each_line=True)
    events = [make_event(1), make_event(2, duration_ns=1_680_000_000)]

    writer.write_many(events)

    text = sink.getvalue()
    assert text.count("\n") == 2
    loaded = TimelineReader().read_all(StringIO(text))
    assert loaded == events


def test_timeline_reader_tolerates_truncated_last_line() -> None:
    complete = make_event(1).to_dict()
    truncated = '{"schemaVersion":"perfeval.timeline.v1","seq":2,"runId":"run-1"'
    text = StringIO(f"{json.dumps(complete)}\n{truncated}")

    loaded = read_timeline(text)
    assert len(loaded) == 1
    assert loaded[0].seq == 1


def test_timeline_reader_reports_line_and_seq() -> None:
    lines = [
        make_event(1).to_dict(),
        {
            "schemaVersion": "perfeval.timeline.v1",
            "seq": 2,
            "runId": "run-1",
            "eventId": "evt-2",
            "type": "interval",
            "name": "startup.cold",
            "tsMonotonicNs": 2_000_000_000,
            "durationNs": "oops",
            "source": {"platform": "android", "collector": "host.adb", "evidenceLevel": "L1"},
            "data": {},
        },
    ]
    text = StringIO("\n".join(json.dumps(line) for line in lines) + "\n")

    with pytest.raises(TimelineRowError) as excinfo:
        read_timeline(text, tolerate_truncated_last_line=False)

    assert excinfo.value.line == 2
    assert excinfo.value.seq == 2
    assert "durationNs" in str(excinfo.value)


def test_timeline_event_new_enforces_schema_invariants() -> None:
    with pytest.raises(ValidationError, match="seq"):
        TimelineEvent.new(
            seq=0,
            run_id="run-1",
            event_id="evt-0",
            type="instant",
            name="invalid",
            ts_monotonic_ns=0,
            source=EventSource(platform="android", collector="test", evidence_level="L1"),
            data={},
        )


def test_timeline_writer_rejects_non_finite_json_numbers() -> None:
    event = make_event(1).to_dict()
    event["data"] = {"value": float("nan")}

    with pytest.raises(ValueError, match="JSON compliant"):
        TimelineWriter(StringIO()).write(event)
