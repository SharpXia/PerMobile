from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from perfeval.core.artifacts import ArtifactIndex, ArtifactRecord, ArtifactRegistry, sha256_of_path
from perfeval.core.schema import ValidationError


def test_artifact_index_and_registry_round_trip(tmp_path: Path) -> None:
    file_path = tmp_path / "trace.txt"
    file_path.write_text("trace-data", encoding="utf-8")
    digest = hashlib.sha256(b"trace-data").hexdigest()

    record = ArtifactRecord(
        artifact_id="pftrace-1",
        path="artifacts/android/perfetto.pftrace",
        sha256=digest,
        size_bytes=file_path.stat().st_size,
        collector="perfetto",
        metric_data={"startup.cold.ms": 1680},
    )

    index = ArtifactIndex()
    index.add(record)
    payload = index.to_dict()
    assert payload["records"][0]["metricData"]["startup.cold.ms"] == 1680

    registry = ArtifactRegistry.from_dict(payload)
    assert registry.lookup("pftrace-1") == record
    assert registry.lookup_by_path("artifacts/android/perfetto.pftrace") == record
    assert registry.by_collector("perfetto") == [record]

    reloaded = ArtifactIndex.from_dict(payload)
    assert reloaded.records == [record]
    assert sha256_of_path(file_path) == digest


def test_artifact_index_rejects_duplicates() -> None:
    record = ArtifactRecord(
        artifact_id="artifact-1",
        path="artifacts/a.txt",
        sha256="0" * 64,
        size_bytes=1,
        collector="host",
    )
    index = ArtifactIndex()
    index.add(record)

    with pytest.raises(ValidationError):
        index.add(
            ArtifactRecord(
                artifact_id="artifact-1",
                path="artifacts/b.txt",
                sha256="0" * 64,
                size_bytes=1,
                collector="host",
            )
        )

    with pytest.raises(ValidationError):
        index.add(
            ArtifactRecord(
                artifact_id="artifact-2",
                path="artifacts/a.txt",
                sha256="0" * 64,
                size_bytes=1,
                collector="host",
            )
        )
