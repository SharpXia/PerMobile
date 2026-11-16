from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .samplers.base import SampleRecord

TIMELINE_SCHEMA_VERSION = "perfeval.timeline.v1"

# tags 中的保留键：解析为事件 envelope / data 字段，其余原样保留在事件 tags
_TAG_COLLECTOR = "collector"
_TAG_UNIT = "unit"
_TAG_DIRECTION = "direction"
_TAG_SEMANTIC = "semantic"
_TAG_TS_UTC = "tsUtc"
_TAG_SAMPLING_WINDOW_MS = "samplingWindowMs"
_TAG_EVIDENCE_LEVEL = "evidenceLevel"

# metricId -> evidenceLevel 映射（METRIC_SOURCES §4 总表）；未列出的指标保持 L1。
# gpu.busy.pct：kgsl 节点为 OEM 私有实现，口径存在厂商差异 → L2；
# power.energy.mah：电流均值 × 窗口时长的估算值 → L2。
EVIDENCE_LEVEL_BY_METRIC_ID: dict[str, str] = {
    "gpu.busy.pct": "L2",
    "power.energy.mah": "L2",
}
DEFAULT_EVIDENCE_LEVEL = "L1"


class SamplesWriter:
    """samples.ndjson 在线追加写（host 侧 artifacts/android/），崩溃容错、逐行落盘。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._handle: Any = None

    @property
    def path(self) -> Path:
        return self._path

    def append(self, records: Sequence[SampleRecord] | SampleRecord) -> None:
        if not records:
            return
        if isinstance(records, SampleRecord):
            records = [records]
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._handle is None:
            self._handle = self._path.open("a", encoding="utf-8")
        for record in records:
            self._handle.write(json.dumps(record.to_dict(), sort_keys=True) + "\n")
        self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "SamplesWriter":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def read_samples(path: str | Path) -> list[SampleRecord]:
    """读取 samples.ndjson；坏行跳过（与 artifact 文本解码的容错策略一致）。"""
    records: list[SampleRecord] = []
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, Mapping):
            continue
        metric_id = payload.get("metricId")
        value = payload.get("value")
        if not isinstance(metric_id, str) or not metric_id:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        tags = payload.get("tags")
        records.append(
            SampleRecord(
                metric_id=metric_id,
                value=float(value),
                ts_ms=payload.get("tsMs") if isinstance(payload.get("tsMs"), int) else 0,
                tags=dict(tags) if isinstance(tags, Mapping) else {},
            )
        )
    return records


def parse_samples_events(
    samples: Sequence[SampleRecord | Mapping[str, Any]],
    *,
    run_id: str,
    start_seq: int = 1,
    platform: str = "android",
    ts_origin_ms: int | None = None,
) -> list[dict[str, Any]]:
    """把 samples 记录转为 timeline counter 事件（CM-001 设计 §2 JSON 约定）。

    - ``platform``：事件 source.platform（sim-2 修复，2026-11-04 体检：simulator
      会话复用本函数时平台曾被硬编码为 "android"，与注册表 platform="ios" 矛盾）；
      后端按 provider_mode 传 "ios"/"android"，perfeval CLI 相对时戳路径维持缺省。
    - ``ts_origin_ms``：run 统一时钟原点（epoch ms）——sim-3 修复（2026-11-04
      体检）：后端缓冲是 epoch 域，直接 ×1e6 填 tsMonotonicNs 使字段与契约
      「统一单调时钟纳秒」不符且受 NTP 回拨影响；提供原点后事件时戳变为
      run 内相对纳秒（单调、与 fixtures/gfxinfo 逐帧路径同哲学）。epoch 信息
      仍由事件 tsUtc（tags.tsUtc）与 samples.ndjson tsMs 保留。
    """
    events: list[dict[str, Any]] = []
    seq = start_seq
    for sample in samples:
        record = _as_sample_record(sample)
        if record is None:
            continue
        tags = dict(record.tags)
        collector = _pop_str(tags, _TAG_COLLECTOR, "sampler")
        unit = _pop_str(tags, _TAG_UNIT, None)
        direction = _pop_str(tags, _TAG_DIRECTION, "none")
        semantic = _pop_str(tags, _TAG_SEMANTIC, None)
        ts_utc = _pop_str(tags, _TAG_TS_UTC, None)
        window_ms = tags.pop(_TAG_SAMPLING_WINDOW_MS, None)
        # evidenceLevel：事件 tags 显式覆盖 > METRIC_SOURCES §4 映射 > 默认 L1
        evidence_level = (
            _pop_str(tags, _TAG_EVIDENCE_LEVEL, None)
            or EVIDENCE_LEVEL_BY_METRIC_ID.get(record.metric_id, DEFAULT_EVIDENCE_LEVEL)
        )

        data: dict[str, Any] = {"value": record.value, "direction": direction}
        if unit is not None:
            data["unit"] = unit
        if semantic is not None:
            data["semantic"] = semantic
        if isinstance(window_ms, (int, float)) and not isinstance(window_ms, bool) and window_ms > 0:
            # 采样窗口供 summary 引擎计算 coverage（CM-001 §4 置信度语义分离）
            data["samplingWindowMs"] = window_ms

        ts_ms = int(record.ts_ms)
        if ts_origin_ms is not None:
            ts_ms = max(0, ts_ms - int(ts_origin_ms))
        event: dict[str, Any] = {
            "schemaVersion": TIMELINE_SCHEMA_VERSION,
            "seq": seq,
            "runId": run_id,
            "eventId": f"{run_id}-sample-{seq:06d}",
            "type": "counter",
            "name": record.metric_id,
            "tsMonotonicNs": max(0, ts_ms) * 1_000_000,
            "source": {
                "platform": platform,
                "collector": collector,
                "evidenceLevel": evidence_level,
            },
            "data": data,
        }
        if ts_utc is not None:
            event["tsUtc"] = ts_utc
        if tags:
            event["tags"] = tags
        events.append(event)
        seq += 1
    return events


def _as_sample_record(sample: SampleRecord | Mapping[str, Any]) -> SampleRecord | None:
    if isinstance(sample, SampleRecord):
        return sample
    if isinstance(sample, Mapping):
        value = sample.get("value")
        metric_id = sample.get("metricId")
        if not isinstance(metric_id, str) or not metric_id:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        ts_ms = sample.get("tsMs")
        tags = sample.get("tags")
        return SampleRecord(
            metric_id=metric_id,
            value=float(value),
            ts_ms=ts_ms if isinstance(ts_ms, int) else 0,
            tags=dict(tags) if isinstance(tags, Mapping) else {},
        )
    return None


def _pop_str(tags: dict[str, Any], key: str, default: str | None) -> str | None:
    value = tags.pop(key, None)
    return str(value) if isinstance(value, str) and value else default
