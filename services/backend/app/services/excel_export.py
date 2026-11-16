"""Excel 导出 writer（契约 §13.1/§13.2，ARCH §9.6；DEVELOPMENT_PLAN R40，openpyxl E10）。

工作簿对标 [I§2.5] S2-img7 官方截图可见的 **4 个**工作表名（all / FrameInfo /
FrameallCpuLoggetdata / Latency）。DEVELOPMENT_PLAN R40 写「5 工作表」，但调研材料
仅可证实 4 个表名，第 5 表不臆测（契约 §13.1 如实声明；ARCH §9.6 同口径）。

sheet `all` 布局（1-based 行；契约 §13.1「设备信息块 + Sum 汇总行 + 逐秒数据行」）：

  1..D    设备信息块（A=字段名，B=值）。仅写 manifest.json 实际承载的字段，
          不编造——PerfDog 块中的 OS/CPU/GPU/Resolution/Ram 行需 manifest 增设
          deviceInfo 后自动出现；ARCH §9.6 明确 save 扩展不改落盘结构
          （session_manager.py manifest 形状 [R§2.9] 不变），故 M2 不写这些行。
  D+1     空行
  D+2     Sum 标签行（A="Sum"，B..=统计项名）
  D+3     Sum 数值行（A="Sum"，B..=统计值；不可计算 → 空单元格，不填 0，[R§8.6]）
  D+4     空行
  D+5     逐秒网格表头（time / absTime / Label / Notes + 全部指标列）
  D+6..   逐秒数据行（Label/Notes 自 markers.ndjson 接管，契约 §15.4；
          无适用标记为空串）

单一事实源约束（契约 §13.1 / ARCH §9.6）：Sum 行统计量一律调 statistics_service
（build_run_statistics，与 `GET /api/runs/{runId}/statistics` 同值同参数），本模块
**不另算**任何统计量。[I§2.5] 列出的 Recv/Send/BTemp 族官方汇总列在
perfdog.statistics.v1 无对应统计组（net/temp 只有采样点、无聚合口径），按单源约束
不写（不臆测，与「第 5 工作表」同一原则）。

逐秒行口径（契约 §13.1 如实声明）：PerfDog 固定 1s 采样；本实现按指标 intervalMs
（500–2000ms）出点——每行一个「该秒内有采样」的秒桶，指标列取该秒内该指标**最后
一个**采样点，该秒无采样的指标留空（None，不填 0）；整秒无任何采样的行不造
（缺口由行缺失表达，与「时间轴空洞」同哲学）。time = 相对秒（相对最早采样点），
absTime = 该秒桶起点的 epoch ms。

FrameInfo（契约 §13.1）：frameIndex / tsEpochMs / durationMs / jank 四列；
jank 列为 perfdog.statistics.v1 内核双条件 Jank 判定（kernel.jank_classification，
与 Sum 行 Jank 同口径同源），**不复用**事件里的 perfeval 1.5×刷新周期标记
（口径差异见 tests/test_statistics_parity.py）。无逐帧数据时仅写表头。

FrameallCpuLoggetdata / Latency：M2 按名建表、内容留空——数据列随真实数据面补齐
（契约 §13.1）；写占位内容会污染未来 R45 导入链路，故留空。

Label/Notes（契约 §15.4）：自 run 目录 sidecar ``markers.ndjson``（§15.1 落盘）
取——``Label`` = 该秒所属 label 段的 ``name``（段 = 该 label ``tsMs`` 所在秒起、至
下一个 label 或数据结束，[F§6]「标记进入新场景，持续到下次 setLabel」段内逐行
填充）；``Notes`` = 落在该秒内的 note ``text``（多个以 ``"; "`` 连接）；``mark``
不导出（[I§2.5] S2-img7 官方截图逐秒行仅见 Label/Notes 两列，不臆测 Mark 列）。
无 ``markers.ndjson``（旧 run/无标记）→ 两列空串，与 §15.3「无标记是常态」同口径。
"""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote

from openpyxl import Workbook

from perfeval.collectors.android.samples import read_samples

from ..registry import METRICS
from ..stats import kernel
from .marker_service import read_markers_sidecar
from .statistics_service import (
    DEFAULT_FPS_GE,
    build_run_statistics,
    read_frame_detail,
    resolve_run_dir,
)

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

SHEET_ALL = "all"
SHEET_FRAME_INFO = "FrameInfo"
SHEET_CPU_LOG = "FrameallCpuLoggetdata"
SHEET_LATENCY = "Latency"
#: 工作表名与顺序（[I§2.5] S2-img7 可证实的 4 表；契约 §13.1——第 5 表不臆测）
SHEET_NAMES: tuple[str, ...] = (SHEET_ALL, SHEET_FRAME_INFO, SHEET_CPU_LOG, SHEET_LATENCY)

TIME_HEADER = "time"
ABS_TIME_HEADER = "absTime"
LABEL_HEADER = "Label"
NOTES_HEADER = "Notes"
GRID_HEADERS: tuple[str, ...] = (TIME_HEADER, ABS_TIME_HEADER, LABEL_HEADER, NOTES_HEADER)

FRAME_INFO_HEADERS: tuple[str, ...] = ("frameIndex", "tsEpochMs", "durationMs", "jank")

SECOND_MS = 1000

# 设备信息块字段：PerfDog 块键名 → manifest.json 取值路径（点路径；缺失整行不写）
DEVICE_INFO_FIELDS: tuple[tuple[str, str], ...] = (
    ("Device", "device"),
    ("OS", "deviceInfo.os"),
    ("CPU", "deviceInfo.cpu"),
    ("GPU", "deviceInfo.gpu"),
    ("Resolution", "deviceInfo.resolution"),
    ("Ram", "deviceInfo.ram"),
    ("Package", "packageName"),
    ("CaseName", "caseName"),
    ("RunId", "runId"),
    ("SessionId", "sessionId"),
    ("CreatedAt", "createdAt"),
    ("SavedAt", "savedAt"),
)


# ---- 对外入口 ----


def build_run_export_bytes(runs_dir: Path, run_id: str) -> tuple[bytes, str]:
    """实时生成 run 的 xlsx 字节（无缓存文件，ARCH §9.6）。

    返回 (payload, 文件名)；文件名 = run 目录名 + .xlsx（契约 §13.1）。
    run 不存在 → 404 report_not_found（resolve_run_dir，与 §12 同防护）。
    """
    run_path = resolve_run_dir(runs_dir, run_id)
    workbook = Workbook()
    all_sheet = workbook.active
    assert all_sheet is not None  # Workbook 自带 active 表
    all_sheet.title = SHEET_ALL
    for name in SHEET_NAMES[1:]:
        workbook.create_sheet(name)
    _write_all_sheet(all_sheet, run_path)
    _write_frame_info_sheet(workbook[SHEET_FRAME_INFO], run_path / "timeline.ndjson")
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue(), f"{run_path.name}.xlsx"


def write_run_export_xlsx(runs_dir: Path, run_id: str) -> Path:
    """§13.2 save(exportExcel=true) 落盘：与 §13.1 调**同一个 writer**（ARCH §9.6）。

    目标 = `<runs-root>/<run 目录名>.xlsx`（与 §13.1 文件名规则一致；收口时目录
    重名加序号后，以最终目录名为准——§13.1 重导出与 save 导出产物同名）。
    """
    payload, filename = build_run_export_bytes(runs_dir, run_id)
    target = runs_dir / filename
    target.write_bytes(payload)
    return target


def content_disposition(filename: str) -> str:
    """RFC 6266 Content-Disposition：ASCII 用 filename=；非 ASCII 附加
    filename*=UTF-8''<percent-encoded>，filename= 用 percent-encoded ASCII 兜底
    （契约 §13.1）。"""
    encoded = quote(filename, safe="")
    header = f'attachment; filename="{encoded}"'
    if not filename.isascii():
        header += f"; filename*=UTF-8''{encoded}"
    return header


# ---- sheet `all` ----


def _load_manifest(manifest_path: Path) -> dict[str, Any]:
    """manifest.json → dict；缺失/损坏降级为空 dict（设备信息块整块不写，不编造）。"""
    if not manifest_path.is_file():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


def _manifest_value(manifest: dict[str, Any], dotted: str) -> Any:
    node: Any = manifest
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _plain(value: Any) -> Any:
    """manifest 值 → 单元格可写标量（list/dict 不展开，转 JSON 文本如实呈现）。"""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return json.dumps(value, ensure_ascii=False)


def _write_all_sheet(sheet: Any, run_path: Path) -> None:
    runs_dir = run_path.parent
    manifest = _load_manifest(run_path / "manifest.json")
    samples = _read_run_samples(run_path / "samples.ndjson")
    markers = _load_run_markers(run_path)

    row = 1
    for label, dotted in DEVICE_INFO_FIELDS:
        value = _manifest_value(manifest, dotted)
        if value is None:
            continue  # manifest 未承载的字段整行不写（不编造，[R§8.6] 同哲学）
        sheet.cell(row=row, column=1, value=label)
        sheet.cell(row=row, column=2, value=_plain(value))
        row += 1

    row += 1  # 设备信息块与 Sum 区之间空行
    sum_pairs = _sum_pairs(
        build_run_statistics(runs_dir, run_path.name, list(DEFAULT_FPS_GE))
    )
    sheet.cell(row=row, column=1, value="Sum")
    for offset, (label, _) in enumerate(sum_pairs, start=2):
        sheet.cell(row=row, column=offset, value=label)
    row += 1
    sheet.cell(row=row, column=1, value="Sum")
    for offset, (_, value) in enumerate(sum_pairs, start=2):
        if value is not None:
            sheet.cell(row=row, column=offset, value=value)  # None → 空单元格，不填 0
    row += 2  # Sum 区与逐秒网格之间空行

    columns = _metric_columns(manifest, samples)
    for col, header in enumerate(GRID_HEADERS + tuple(columns), start=1):
        sheet.cell(row=row, column=col, value=header)
    for seconds_row in _second_rows(samples, columns, markers):
        row += 1
        for col, value in enumerate(seconds_row, start=1):
            if value is not None:
                sheet.cell(row=row, column=col, value=value)


def _read_run_samples(samples_path: Path) -> list[tuple[int, str, float]]:
    """samples.ndjson → [(tsMs, metricId, value)] 升序（read_samples 已丢非数值行）。"""
    if not samples_path.is_file():
        return []
    try:
        records = read_samples(samples_path)
    except OSError:
        return []
    rows = [(int(record.ts_ms), record.metric_id, float(record.value)) for record in records]
    rows.sort(key=lambda item: item[0])
    return rows


def _metric_columns(manifest: dict[str, Any], samples: list[tuple[int, str, float]]) -> list[str]:
    """全部指标列：manifest 声明集 ∪ 实际采样集；按注册表顺序排列、未注册的按名
    追加在尾（契约 §13.1「采集到的全部指标列」）。"""
    declared = [
        entry["metricId"]
        for entry in manifest.get("metrics") or []
        if isinstance(entry, dict) and isinstance(entry.get("metricId"), str)
    ]
    present = [metric_id for _, metric_id, _ in samples]
    ids = list(dict.fromkeys(declared + present))
    order = {spec.metric_id: index for index, spec in enumerate(METRICS)}
    ids.sort(key=lambda metric_id: (order.get(metric_id, len(order)), metric_id))
    return ids


def _load_run_markers(run_path: Path) -> list[dict[str, Any]]:
    """run 目录 sidecar → 排序标记列表（契约 §15.4 Label/Notes 数据源）。

    无 ``markers.ndjson``（旧 run/无标记）或读盘失败 → 空列表（无标记是常态，
    与 §15.3 同口径）；坏行已由 read_markers_sidecar 跳过。
    """
    sidecar = read_markers_sidecar(run_path)
    if sidecar is None:
        return []
    return sidecar["markers"]


def _label_for_bucket(bucket: int, origin: int, labels: list[tuple[int, str]]) -> str:
    """该秒所属 label 段的 ``name``（契约 §15.4）。

    段 = 该 label ``tsMs`` 所在秒（floor 归桶，与逐秒行同网格）起、至下一个 label
    的所在秒（不含）或数据结束——[F§6]「标记进入新场景，持续到下次 setLabel」段内
    逐行填充；同秒多个 label 取 tsMs 升序后最后一个。``tsMs`` 早于数据首秒的 label
    段起点为负秒，同样自首行起覆盖。无适用段 → 空串。
    """
    applicable = ""
    for ts_ms, name in labels:  # read_markers_sidecar 已按 tsMs 升序
        if bucket >= (ts_ms - origin) // SECOND_MS:
            applicable = name
        else:
            break
    return applicable


def _notes_for_bucket(second_start: int, notes: list[tuple[int, str]]) -> str:
    """落在该秒 ``[second_start, second_start+1000)`` 内的 note ``text``（§15.4）。

    多个以 ``"; "`` 连接（read_markers_sidecar 已按 tsMs 升序）；无则空串。
    """
    texts = [text for ts_ms, text in notes if second_start <= ts_ms < second_start + SECOND_MS]
    return "; ".join(texts)


def _second_rows(
    samples: list[tuple[int, str, float]],
    columns: list[str],
    markers: list[dict[str, Any]] = (),
) -> list[tuple[Any, ...]]:
    """逐秒行：每秒桶取各指标该秒最后一个采样点（契约 §13.1 口径，见模块 docstring）。

    秒桶为半开区间 [origin+k*1000, origin+(k+1)*1000)，时戳按 floor 归桶（恰在秒界
    归后一秒）。行 = (time, absTime, Label, Notes, *各指标值)；Label/Notes 自
    markers.ndjson（契约 §15.4，见 _label_for_bucket/_notes_for_bucket），无适用
    标记为空串；该秒无采样的指标为 None（写空单元格，不填 0）。
    """
    if not samples:
        return []
    origin = samples[0][0]
    labels = [
        (int(marker["tsMs"]), str(marker["name"]))
        for marker in markers
        if marker.get("kind") == "label" and isinstance(marker.get("name"), str)
    ]
    notes = [
        (int(marker["tsMs"]), str(marker["text"]))
        for marker in markers
        if marker.get("kind") == "note" and isinstance(marker.get("text"), str)
    ]
    cells: dict[tuple[int, str], float] = {}
    for ts_ms, metric_id, value in samples:  # ts 升序遍历，后写覆盖先写 = 最后一个采样点
        cells[((ts_ms - origin) // SECOND_MS, metric_id)] = value
    column_index = {metric_id: offset for offset, metric_id in enumerate(columns)}
    rows: list[tuple[Any, ...]] = []
    for bucket in sorted({bucket for bucket, _ in cells}):
        values: list[Any] = [None] * len(columns)
        for (second, metric_id), value in cells.items():
            if second == bucket:
                values[column_index[metric_id]] = value
        second_start = origin + bucket * SECOND_MS
        rows.append(
            (
                bucket,
                second_start,
                _label_for_bucket(bucket, origin, labels),
                _notes_for_bucket(second_start, notes),
                *values,
            )
        )
    return rows


def _sum_pairs(stats: dict[str, Any]) -> list[tuple[str, Any]]:
    """perfdog.statistics.v1 载荷 → Sum 行 (标签, 值) 序列（[I§2.5] 官方汇总族）。

    仅映射统计层已有字段；Recv/Send/BTemp 族无对应统计组，不写（见模块 docstring）。
    """
    fps = stats.get("fps") or {}
    frame_time = stats.get("frameTime") or {}
    jank = stats.get("jank") or {}
    smooth = stats.get("smooth") or {}
    low_fps = stats.get("lowFps") or {}
    ge = fps.get("ge") or {}

    def _ge_key(threshold: str) -> int:
        try:
            return int(threshold)
        except ValueError:  # pragma: no cover — ge 键恒为阈值十进制字符串（契约 §12）
            return 1 << 30

    pairs: list[tuple[str, Any]] = [
        ("Avg(FPS)", fps.get("avg")),
        ("Var(FPS)", fps.get("var")),
        ("Std(FPS)", fps.get("std")),
        ("Drop(FPS)", fps.get("drop")),
        ("Min(FPS)", fps.get("min")),
        ("Median(FPS)", fps.get("median")),
        ("MedRange(FPS)", fps.get("medRange")),
    ]
    for threshold in sorted(ge, key=_ge_key):
        pairs.append((f"FPS>={threshold}(%)", ge[threshold]))
    pairs += [
        ("Avg(FTime)", frame_time.get("avg")),
        ("Std(FTime)", frame_time.get("std")),
        ("Var(FTime)", frame_time.get("var")),
        ("Delta(FTime)", frame_time.get("delta")),
        ("SmallJank", jank.get("small")),
        ("BigJank", jank.get("big")),
        ("Jank", jank.get("jank")),
        ("Jank(/10min)", jank.get("per10min")),
        ("Stutter(%)", jank.get("stutter")),
        ("Smooth", smooth.get("value")),
        ("1%Low(FPS)", low_fps.get("p1")),
        ("Duration(ms)", stats.get("durationMs")),
    ]
    return pairs


# ---- sheet FrameInfo ----


def _write_frame_info_sheet(sheet: Any, timeline_path: Path) -> None:
    """逐帧耗时明细（契约 §13.1）；无逐帧数据时仅写表头。"""
    for col, header in enumerate(FRAME_INFO_HEADERS, start=1):
        sheet.cell(row=1, column=col, value=header)
    frames = read_frame_detail(timeline_path)
    if not frames:
        return
    # jank 列 = 统计内核双条件 Jank 判定（与 Sum 行 Jank 同源同口径，见模块 docstring）
    jank_flags = kernel.jank_classification([frame["duration_ms"] for frame in frames])["jank"]
    row = 2
    for index, frame in enumerate(frames):
        frame_index = frame["frame_index"]
        sheet.cell(row=row, column=1, value=frame_index if frame_index is not None else index)
        if frame["ts_epoch_ms"] is not None:  # frame_relative 事件无 epoch，留空不编造
            sheet.cell(row=row, column=2, value=frame["ts_epoch_ms"])
        sheet.cell(row=row, column=3, value=frame["duration_ms"])
        sheet.cell(row=row, column=4, value=bool(jank_flags[index]))
        row += 1
