"""标记与标注（M5 承载于 M4 后端，契约 §15，`perfdog.markers.v1`）。

硬性裁决（契约 §15）：标记**不进** `perfeval.timeline.v1`——UI 标记写 run 目录
sidecar ``markers.ndjson``，save 时与 manifest/summary 同批落盘；落盘元素无
``id``（id 是会话期 DELETE 操作句柄，不落盘）。无标记不写文件（与 summary.json
「有数据才算」同规则）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("pd-backend.markers")

MARKERS_SCHEMA_VERSION = "perfdog.markers.v1"
MARKER_KINDS = ("label", "note", "mark")

# 会话内存侧元素（含 id）；落盘侧（无 id）只保留契约 15.1 字段
_SESSION_FIELDS = {"label": ("name", "color"), "note": ("text",), "mark": ()}
_VALIDATION_FIELD = {"label": "name", "note": "text", "mark": None}


def validate_marker_body(kind: str, body: dict[str, Any], default_ts_ms: int) -> dict[str, Any]:
    """契约 15.2 请求体校验 → 会话内存元素字段（不含 id）。

    - ``tsMs`` 缺省 = 服务端当前 epoch ms；非整数（bool 亦拒）→ 400；
    - label 缺/空 ``name``、note 缺/空 ``text`` → 400（契约 15.2 错误清单；
      ``color`` 仅在为字符串时保留，非契约校验项）；
    - ``mark`` 无附加字段（多余字段忽略）。
    """
    if kind not in MARKER_KINDS:
        raise ValueError(f"未知标记类型 {kind!r}")
    from ..errors import validation_error

    ts_ms = body.get("tsMs", default_ts_ms)
    if isinstance(ts_ms, bool) or not isinstance(ts_ms, int):
        raise validation_error("tsMs 必须是整数（epoch ms）", field="tsMs")
    element: dict[str, Any] = {"tsMs": ts_ms, "kind": kind}
    field = _VALIDATION_FIELD[kind]
    if field:
        value = body.get(field)
        if not isinstance(value, str) or not value.strip():
            raise validation_error(f"{kind} 必须提供非空 {field}", field=field)
        element[field] = value
    if kind == "label" and isinstance(body.get("color"), str):
        element["color"] = body["color"]
    return element


def _sort_key(marker: dict[str, Any]) -> tuple[int, int]:
    """按 tsMs 升序、同 tsMs 按创建序（契约 15.1）；``_order`` 为创建序占位。"""
    return (int(marker["tsMs"]), int(marker.get("_order", 0)))


def sort_markers(markers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(markers, key=_sort_key)


def strip_session_fields(marker: dict[str, Any]) -> dict[str, Any]:
    """会话元素 → 落盘元素：去 id/创建序占位，保留 15.1 字段。"""
    allowed = {"tsMs", "kind", *_SESSION_FIELDS[marker["kind"]]}
    return {key: value for key, value in marker.items() if key in allowed}


def write_markers_sidecar(run_path: Path, markers: list[dict[str, Any]]) -> bool:
    """save 时统一落盘 ``markers.ndjson``（契约 15.1）；无标记不写文件。

    返回是否写盘。写入失败不阻断 save（sidecar 是派生产物，可由会话内存
    重试——但 save 后会话收口内存不可再改，故失败只记日志并如实返回 False）。
    """
    lines = [strip_session_fields(marker) for marker in sort_markers(markers)]
    if not lines:
        return False
    try:
        with (run_path / "markers.ndjson").open("w", encoding="utf-8") as sink:
            for line in lines:
                sink.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as exc:
        logger.warning("markers.ndjson 写盘失败（%s）：%s", run_path, exc)
        return False
    return True


# run 侧增量写的进程内串行锁：FastAPI sync 路由跑线程池，同 run 并发 POST 会竞态
# （读-改-写窗口）。单机单写者假设的进程内一半；多实例部署不在该假设内（见
# append_markers_sidecar docstring，契约 §15 delta 划界由 S1 落笔）。
_RUN_APPEND_LOCK = threading.Lock()


def append_markers_sidecar(run_path: Path, elements: list[dict[str, Any]]) -> None:
    """run 侧标注增量写（§15.2 会话端点的 run 侧扩展；与 save 期整体写
    ``write_markers_sidecar`` 共用同一 sidecar 文件与 15.1 排序契约）。

    读 sidecar（无文件 = 空列表；坏行按 15.3 读回容错口径跳过）→ 追加 → 按契约
    15.1 排序（tsMs 升序、同 tsMs 按创建序：既有行按行序补创建序占位，新元素接续
    编号）→ 临时文件 + ``os.replace`` 原子替换（崩溃不留半行文件）。

    单机单写者假设（如实注明）：save 期整体写与 run 期增量写不会并发作用于同一
    run（save 收口后该 run 才成为 run 侧标注对象，会话侧同刻已被 §15.2 状态门
    409 拦截）；并发增量写由 ``_RUN_APPEND_LOCK`` 在进程内串行化，多实例部署不在
    该假设内。增量重写按读回容错口径剔除既有坏行（与 15.3 读回同口径，非静默
    丢数据）。
    """
    with _RUN_APPEND_LOCK:
        existing = read_markers_sidecar(run_path)
        markers = existing["markers"] if existing is not None else []
        for i, marker in enumerate(markers):
            marker["_order"] = i
        for j, element in enumerate(elements):
            marker = dict(element)
            marker["_order"] = len(markers) + j
            markers.append(marker)
        lines = [strip_session_fields(m) for m in sort_markers(markers)]
        target = run_path / "markers.ndjson"
        tmp_path = run_path / "markers.ndjson.tmp"
        try:
            with tmp_path.open("w", encoding="utf-8") as sink:
                for line in lines:
                    sink.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
            os.replace(tmp_path, target)
        except OSError as exc:
            logger.warning("markers.ndjson 增量写失败（%s）：%s", run_path, exc)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise


def read_markers_sidecar(run_path: Path) -> dict[str, Any] | None:
    """run 目录读回 sidecar（契约 15.3）；无文件返回 None（无标记是常态）。

    坏行跳过（与 read_samples 同容错口径）；返回体含 ``schemaVersion``/``runId``
    （runId 由调用方以 run 目录名补齐——此处不带，见 read_markers_body）。
    """
    path = run_path / "markers.ndjson"
    if not path.is_file():
        return None
    markers: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            not isinstance(payload, dict)
            or payload.get("kind") not in MARKER_KINDS
            or isinstance(payload.get("tsMs"), bool)
            or not isinstance(payload.get("tsMs"), int)
        ):
            continue
        markers.append(payload)
    markers.sort(key=_sort_key)
    return {"markers": markers}


def read_markers_body(run_path: Path, run_id: str) -> dict[str, Any]:
    """契约 15.3 响应体：sidecar 读回 + schemaVersion/runId；无文件 → 空列表。"""
    sidecar = read_markers_sidecar(run_path)
    return {
        "schemaVersion": MARKERS_SCHEMA_VERSION,
        "runId": run_id,
        "markers": sidecar["markers"] if sidecar is not None else [],
    }
