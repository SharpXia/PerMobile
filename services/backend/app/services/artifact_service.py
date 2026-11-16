"""工件索引与读取（M4，契约 §17，DEVELOPMENT_PLAN R35/R37）。

三类端点共用同一份索引事实：run 目录 ``artifacts/`` 目录扫描。口径对齐 perfeval
``index_android_artifacts``（collector.py:297-335）与 ``_collector_for_filename``
（collector.py:335-350）：

- ``<1KB`` 录屏判废件不入索引（复用 ``SCREENRECORD_MIN_VALID_BYTES``，plan.py:29；
  在线路径已由 runner pull 删除，此处兜底离线目录场景）；
- kind：``video*``（含分段 video-segNNN.mp4）→ ``video``、``screenshot-*.png`` →
  ``screenshot``、其余 → ``data``（契约 §17）；
- collector：video → ``screenrecord``（perfeval 采集命名 screen*.mp4 的映射，
  collector.py:348-349）、截图 → ``screencap``（§16.2 新增值）、其余沿用 perfeval
  扩展名口径。

排序（契约 §17 golden 顺序）：video 段按名升序在前，随后 screenshot，最后 data。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from perfeval.collectors.android.collector import _collector_for_filename
from perfeval.core.artifacts import sha256_of_path
from perfeval.collectors.android.plan import SCREENRECORD_MIN_VALID_BYTES

_VIDEO_RE = re.compile(r"^video(-seg\d{3})?\.mp4$")
_SCREENSHOT_RE = re.compile(r"^screenshot-\d{13}\.png$")

# Content-Type 按扩展名（契约 §17 artifacts/{name}）；未列出 → octet-stream
CONTENT_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".mp4": "video/mp4",
    ".json": "application/json",
    ".ndjson": "application/x-ndjson",
    ".txt": "text/plain",
}


def artifact_kind(name: str) -> str:
    if _VIDEO_RE.fullmatch(name):
        return "video"
    if _SCREENSHOT_RE.fullmatch(name):
        return "screenshot"
    return "data"


def backend_collector_for(name: str) -> str:
    """契约 §17 collector 口径：契约命名优先，其余沿用 perfeval 扩展名映射。"""
    if _VIDEO_RE.fullmatch(name):
        return "screenrecord"
    if _SCREENSHOT_RE.fullmatch(name):
        return "screencap"
    return _collector_for_filename(name)


def build_artifact_index(run_path: Path) -> list[dict[str, Any]]:
    """run 目录 ``artifacts/`` 扫描 → §17 索引元素列表（目录缺失/为空 → []）。"""
    artifacts_dir = run_path / "artifacts"
    if not artifacts_dir.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(artifacts_dir.iterdir(), key=lambda p: p.name):
        if not path.is_file():
            continue
        name = path.name
        if artifact_kind(name) == "video" and path.stat().st_size < SCREENRECORD_MIN_VALID_BYTES:
            # <1KB 判废件不入索引（契约 §17，perfeval 同口径兜底）
            continue
        try:
            size = path.stat().st_size
            digest = sha256_of_path(path)
        except OSError:
            continue
        records.append(
            {
                "name": name,
                "kind": artifact_kind(name),
                "bytes": size,
                "sha256": digest,
                "collector": backend_collector_for(name),
            }
        )
    order = {"video": 0, "screenshot": 1, "data": 2}
    # video 组内首段 video.mp4 在前（契约 §17 示例顺序），分段按 segNNN 升序
    records.sort(
        key=lambda record: (
            order[record["kind"]],
            0 if record["name"] == "video.mp4" else 1,
            record["name"],
        )
    )
    return records


def resolve_artifact_path(run_path: Path, name: str) -> Path | None:
    """索引内文件名 → 实路径；目录穿越防护（name 必须是索引内纯文件名）。"""
    if (
        not name
        or "/" in name
        or "\\" in name
        or name in (".", "..")
        or "\x00" in name
    ):
        return None
    for record in build_artifact_index(run_path):
        if record["name"] == name:
            candidate = run_path / "artifacts" / name
            return candidate if candidate.is_file() else None
    return None


def select_video_artifact(run_path: Path, name: str | None) -> tuple[str, Path]:
    """§17 video 端点选件：缺省首个 kind=video；``?name=`` 必须在索引且为 video。

    返回 (文件名, 实路径)；run 无任何 video 工件 / name 不合法 → None 由调用方
    转 404 report_not_found。
    """
    videos = [
        record
        for record in build_artifact_index(run_path)
        if record["kind"] == "video"
    ]
    if name is None:
        chosen = videos[0] if videos else None
    else:
        chosen = next((record for record in videos if record["name"] == name), None)
    if chosen is None:
        return "", Path()
    return chosen["name"], run_path / "artifacts" / chosen["name"]
