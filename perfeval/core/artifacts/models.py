from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from ..schema.errors import ValidationError


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string", field=field)
    return value


def _require_int(value: Any, field: str, *, min_value: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer", field=field)
    if min_value is not None and value < min_value:
        raise ValidationError(f"{field} must be >= {min_value}", field=field)
    return value


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{field} must be a mapping", field=field)
    return value


def _optional_mapping(value: Any, field: str) -> Optional[Mapping[str, Any]]:
    if value is None:
        return None
    return _require_mapping(value, field)


def _normalize_sha256(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("sha256:"):
        value = value.split(":", 1)[1]
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValidationError("sha256 must be a 64-character hex digest", field="sha256")
    return value


def sha256_of_path(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    path: str
    sha256: str
    size_bytes: int
    collector: str
    metric_data: Mapping[str, Any] = field(default_factory=dict)
    tags: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArtifactRecord":
        payload = _require_mapping(data, "artifact")
        return cls(
            artifact_id=_require_str(payload.get("artifactId"), "artifactId"),
            path=_require_str(payload.get("path"), "path"),
            sha256=_normalize_sha256(_require_str(payload.get("sha256"), "sha256")),
            size_bytes=_require_int(payload.get("sizeBytes"), "sizeBytes", min_value=0),
            collector=_require_str(payload.get("collector"), "collector"),
            metric_data=_optional_mapping(payload.get("metricData"), "metricData") or {},
            tags=_optional_mapping(payload.get("tags"), "tags") or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifactId": self.artifact_id,
            "path": self.path,
            "sha256": self.sha256,
            "sizeBytes": self.size_bytes,
            "collector": self.collector,
            "metricData": dict(self.metric_data),
            "tags": dict(self.tags),
        }


@dataclass
class ArtifactIndex:
    records: list[ArtifactRecord] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArtifactIndex":
        payload = _require_mapping(data, "artifact index")
        items = payload.get("records", [])
        if not isinstance(items, list):
            raise ValidationError("records must be a list", field="records")
        index = cls()
        for item in items:
            index.add(ArtifactRecord.from_dict(item))
        return index

    def add(self, record: ArtifactRecord | Mapping[str, Any]) -> ArtifactRecord:
        if not isinstance(record, ArtifactRecord):
            record = ArtifactRecord.from_dict(record)
        if any(existing.artifact_id == record.artifact_id for existing in self.records):
            raise ValidationError(
                f"duplicate artifactId {record.artifact_id!r}",
                field="artifactId",
            )
        if any(existing.path == record.path for existing in self.records):
            raise ValidationError(
                f"duplicate artifact path {record.path!r}",
                field="path",
            )
        self.records.append(record)
        return record

    def by_collector(self, collector: str) -> list[ArtifactRecord]:
        return [record for record in self.records if record.collector == collector]

    def by_artifact_id(self, artifact_id: str) -> Optional[ArtifactRecord]:
        for record in self.records:
            if record.artifact_id == artifact_id:
                return record
        return None

    def to_dict(self) -> dict[str, Any]:
        return {"records": [record.to_dict() for record in self.records]}

    def to_path_map(self) -> dict[str, ArtifactRecord]:
        return {record.path: record for record in self.records}


class ArtifactRegistry:
    def __init__(self, index: Optional[ArtifactIndex] = None) -> None:
        self._index = index or ArtifactIndex()

    @property
    def index(self) -> ArtifactIndex:
        return self._index

    def register(self, record: ArtifactRecord | Mapping[str, Any]) -> ArtifactRecord:
        return self._index.add(record)

    def lookup(self, artifact_id: str) -> Optional[ArtifactRecord]:
        return self._index.by_artifact_id(artifact_id)

    def lookup_by_path(self, path: str) -> Optional[ArtifactRecord]:
        return self._index.to_path_map().get(path)

    def by_collector(self, collector: str) -> list[ArtifactRecord]:
        return self._index.by_collector(collector)

    def to_index(self) -> ArtifactIndex:
        return ArtifactIndex(records=list(self._index.records))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArtifactRegistry":
        return cls(ArtifactIndex.from_dict(data))
