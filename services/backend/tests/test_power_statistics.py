"""power 统计组（2026-11-10 契约 §12 delta：Sum(Battery) + FPower 帧分摊）。

合成 power.energy.mah 逐窗增量序列 + 帧事件序列，逐位断言（手算复核）：
- power.sum：Σ energy（power-2 口径：每条记录 = 单窗增量）会话累计；
- power.frame：能耗窗口按时间密度分摊到帧（首条增量无窗口端点不计入），
  mean/p95（最近秩）/total + 守恒（帧完整铺满能耗窗口时 Σ 帧能耗 = Σ_{i≥2} mah）；
- none 基线 + 能耗在场：frameDataBasis=none 但 power.sum 如实计算（帧与能耗
  独立）；无能耗 → null + note（不填假零值）；
- §18 区间：power 组按会话全量计算，不随 fromMs/toMs 过滤（契约 delta 声明）。

live 真值注记：SM_F9560 电池 sysfs 被 SELinux 屏蔽（power.* 无源），live 复验
挂起至具备可读 power_supply 的设备；本文件验收以合成 fixture 为准。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from app.services.statistics_service import (
    POWER_ENERGY_METRIC_ID,
    POWER_FRAME_NOTE,
    POWER_FRAME_NOTE_NONE,
    POWER_SUM_NOTE,
    POWER_SUM_NOTE_NONE,
    _power_section,
    build_run_statistics,
    build_session_statistics,
)
from perfeval.collectors.android.samples import SamplesWriter
from perfeval.collectors.android.samplers.base import SampleRecord
from perfeval.core.timeline import write_timeline


def _write_samples(run_dir: Path, points: list[tuple[int, float]]) -> None:
    with SamplesWriter(run_dir / "samples.ndjson") as writer:
        for ts_ms, mah in points:
            writer.append(
                SampleRecord(
                    metric_id=POWER_ENERGY_METRIC_ID,
                    value=mah,
                    ts_ms=ts_ms,
                    tags={"unit": "mAh", "collector": "sampler.power", "semantic": "estimated"},
                )
            )


def _write_frames(run_dir: Path, frames: list[tuple[int, float]]) -> None:
    payloads = []
    for seq, (ts_ms, duration_ms) in enumerate(frames, start=1):
        payloads.append(
            {
                "schemaVersion": "perfeval.timeline.v1",
                "seq": seq,
                "runId": "run-power-fixture",
                "eventId": f"run-power-fixture-frame-{seq:03d}",
                "type": "counter",
                "name": "frame.duration.detail.ms",
                "tsMonotonicNs": int(ts_ms) * 1_000_000,
                "source": {"platform": "android", "collector": "synthetic.fixture", "evidenceLevel": "L1"},
                "data": {"value": duration_ms, "unit": "ms", "direction": "lower_better"},
            }
        )
    with (run_dir / "timeline.ndjson").open("w", encoding="utf-8") as sink:
        write_timeline(payloads, sink)


# ---- 内核级手算（分摊密度）----


def test_power_section_hand_computed_apportionment() -> None:
    # 能耗：ts=1000 mah=0.5（首条，不计分摊）、ts=3000 mah=1.0、ts=5000 mah=2.0
    # 段 A [1000,3000] 密度 0.0005 mAh/ms；段 B [3000,5000] 密度 0.001 mAh/ms
    # 帧：1000(500)/2500(500) → A 各 500ms → 0.25/0.25；3500(500)/4500(500) → B 各 0.5
    energy = [(1000, 0.5), (3000, 1.0), (5000, 2.0)]
    frames = [(1000.0, 500.0), (2500.0, 500.0), (3500.0, 500.0), (4500.0, 500.0)]
    section = _power_section(energy, frames)
    assert section["sum"] == {
        "totalMah": 3.5,  # 0.5 + 1.0 + 2.0
        "windowCount": 3,
        "note": POWER_SUM_NOTE,
    }
    assert section["frame"] == {
        "meanMah": 0.375,   # 1.5 / 4
        "p95Mah": 0.5,      # 最近秩：ceil(0.95×4)-1 = 3 → sorted[3] = 0.5
        "totalMah": 1.5,    # 0.5×0.5 + 1.0×0.5（帧未铺满窗口，只计交叠部分）
        "frameCount": 4,
        "note": POWER_FRAME_NOTE,
    }


def test_power_frame_conservation_when_frames_tile_windows() -> None:
    """帧完整铺满能耗窗口：Σ 帧能耗 = Σ_{i≥2} mah_i（守恒不变量）。"""
    energy = [(1000, 0.5), (2000, 1.0), (3000, 1.5)]
    frames = [(1000.0, 1000.0), (2000.0, 1000.0)]
    section = _power_section(energy, frames)
    assert section["frame"]["totalMah"] == 2.5  # 1.0 + 1.5（首条 0.5 无窗口端点）
    assert section["frame"]["meanMah"] == 1.25
    assert section["frame"]["p95Mah"] == 1.5    # 最近秩：ceil(0.95×2)-1 = 1 → sorted[1]
    assert section["frame"]["frameCount"] == 2


def test_power_section_partial_frame_outside_energy_timeline() -> None:
    """能耗时线之前的帧不计入（估算不外推）；帧起点在时线内则交叠部分计入。"""
    section = _power_section([(2000, 1.0), (4000, 2.0)], [(1000.0, 500.0), (2000.0, 500.0)])
    frame = section["frame"]
    assert frame["frameCount"] == 1  # ts=1000 < 首条能耗 ts=2000 → 不计入
    assert frame["totalMah"] == 0.5  # [2000,2500]∩[2000,4000] = 500ms × 0.001
    assert frame["meanMah"] == 0.5


def test_power_section_none_cases() -> None:
    """无能耗 / 单条增量（无窗口端点）/ 无帧 → null + note，不填假零值。"""
    none_section = _power_section([], [])
    assert none_section["sum"] == {"totalMah": None, "windowCount": 0, "note": POWER_SUM_NOTE_NONE}
    assert none_section["frame"]["totalMah"] is None
    assert none_section["frame"]["note"] == POWER_FRAME_NOTE_NONE
    # 单条增量：无前趋端点 → 分摊不可计算
    single = _power_section([(1000, 0.5)], [(1000.0, 500.0)])
    assert single["sum"]["totalMah"] == 0.5
    assert single["frame"]["totalMah"] is None
    # 能耗与帧时域无重叠
    disjoint = _power_section([(1000, 0.5), (2000, 1.0)], [(90000.0, 500.0)])
    assert disjoint["frame"]["totalMah"] is None


# ---- run 路径端到端（含 none 基线 + 能耗在场、区间不过滤）----


def test_build_run_statistics_power_group(tmp_path: Path) -> None:
    run_dir = tmp_path / "PD_POWER_FIXTURE"
    run_dir.mkdir()
    _write_samples(run_dir, [(1000, 0.5), (3000, 1.0), (5000, 2.0)])
    _write_frames(run_dir, [(1000, 500.0), (2500, 500.0), (3500, 500.0), (4500, 500.0)])

    body = build_run_statistics(tmp_path, "run-PD_POWER_FIXTURE", [18, 25])
    assert body["frameDataBasis"] == "frameEvents"
    assert body["power"] == {
        "sum": {"totalMah": 3.5, "windowCount": 3, "note": POWER_SUM_NOTE},
        "frame": {
            "meanMah": 0.375, "p95Mah": 0.5, "totalMah": 1.5, "frameCount": 4,
            "note": POWER_FRAME_NOTE,
        },
    }


def test_build_run_statistics_none_baseline_with_energy(tmp_path: Path) -> None:
    """无帧无 fps 但能耗在场：frameDataBasis=none，power.sum 如实计算。"""
    run_dir = tmp_path / "PD_POWER_NONE"
    run_dir.mkdir()
    _write_samples(run_dir, [(1000, 0.5), (3000, 1.0)])

    body = build_run_statistics(tmp_path, "run-PD_POWER_NONE", [18, 25])
    assert body["frameDataBasis"] == "none"
    assert body["fps"]["avg"] is None
    assert body["power"]["sum"]["totalMah"] == 1.5
    assert body["power"]["sum"]["windowCount"] == 2
    assert body["power"]["frame"]["totalMah"] is None  # 无帧 → 分摊不计算


def test_build_run_statistics_power_group_ignores_interval(tmp_path: Path) -> None:
    """§18 区间：power 组按会话全量计算，不随 fromMs/toMs 过滤（契约 delta 声明）。"""
    run_dir = tmp_path / "PD_POWER_INTERVAL"
    run_dir.mkdir()
    _write_samples(run_dir, [(1000, 0.5), (3000, 1.0), (5000, 2.0)])
    _write_frames(run_dir, [(1000, 500.0), (2500, 500.0)])

    whole = build_run_statistics(tmp_path, "run-PD_POWER_INTERVAL", [18, 25])
    brushed = build_run_statistics(
        tmp_path, "run-PD_POWER_INTERVAL", [18, 25], from_ms=4000, to_ms=5000
    )
    # 区间把 fps 点列裁空（none 基线 + 回显），power 组仍为全量
    assert brushed["frameDataBasis"] == "none" and brushed["fromMs"] == 4000
    assert brushed["power"] == whole["power"]
    assert brushed["power"]["sum"]["totalMah"] == 3.5


def test_build_run_statistics_no_energy_null_notes(tmp_path: Path) -> None:
    """无能耗数据：power 组 null + note（不填假零值），既有 fps 组形态不变。"""
    run_dir = tmp_path / "PD_POWER_EMPTY"
    run_dir.mkdir()
    _write_frames(run_dir, [(1000, 500.0)])

    body = build_run_statistics(tmp_path, "run-PD_POWER_EMPTY", [18, 25])
    assert body["power"]["sum"] == {"totalMah": None, "windowCount": 0, "note": POWER_SUM_NOTE_NONE}
    assert body["power"]["frame"]["totalMah"] is None
    assert body["power"]["frame"]["note"] == POWER_FRAME_NOTE_NONE
    assert body["fps"]["avg"] is not None  # 既有组不受 power 组增列影响


# ---- 会话路径端到端 ----


def _fake_session(records: list[dict], metrics: dict[str, int] | None = None):
    return SimpleNamespace(
        id="sess-power", records=lambda: records,
        metrics=metrics or {"frame.fps.mean": 500},
    )


def test_build_session_statistics_power_group() -> None:
    session = _fake_session(
        [
            {"tsMs": 1000, "metricId": "frame.fps.mean", "value": 60.0},
            {"tsMs": 2000, "metricId": "frame.fps.mean", "value": 58.0},
            {"tsMs": 1000, "metricId": POWER_ENERGY_METRIC_ID, "value": 0.5},
            {"tsMs": 3000, "metricId": POWER_ENERGY_METRIC_ID, "value": 1.0},
        ]
    )
    body = build_session_statistics(session, [18, 25])
    assert body["frameDataBasis"] == "fpsSeries"
    # 会话侧无逐帧数据（§12）：FPower 恒 null + note；Sum 如实累计
    assert body["power"]["sum"] == {"totalMah": 1.5, "windowCount": 2, "note": POWER_SUM_NOTE}
    assert body["power"]["frame"]["totalMah"] is None
    assert body["power"]["frame"]["note"] == POWER_FRAME_NOTE_NONE
    assert body["durationMs"] == 1000  # fps 组不受 power 组增列影响


def test_build_session_statistics_no_energy_null() -> None:
    session = _fake_session([{"tsMs": 1000, "metricId": "frame.fps.mean", "value": 60.0}])
    body = build_session_statistics(session, [18, 25])
    assert body["power"]["sum"]["totalMah"] is None
    assert body["power"]["sum"]["note"] == POWER_SUM_NOTE_NONE


# ---- 端点层（§12 payload 顶层 power 组 + §9 报告面引用）----


def test_statistics_endpoint_payload_includes_power_group(tmp_path: Path, client) -> None:
    """§12 run 统计端点透出 power 组；§9 run 详情不内联统计（引用不内联，§19 同款）。"""
    run_dir = client.runs_dir / "PD_POWER_ENDPOINT"
    run_dir.mkdir(parents=True)
    _write_samples(run_dir, [(1000, 0.5), (3000, 1.0)])
    _write_frames(run_dir, [(1000, 500.0), (2500, 500.0)])

    resp = client.get("/api/runs/run-PD_POWER_ENDPOINT/statistics")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["power"]["sum"]["totalMah"] == 1.5
    assert body["power"]["frame"]["totalMah"] == 0.5  # 0.25 + 0.25（段 [1000,3000]）
    assert body["power"]["frame"]["note"] == POWER_FRAME_NOTE
    payload = json.dumps(body)  # 可序列化（契约 §1 JSON）
    assert "power" in payload

    detail = client.get("/api/runs/run-PD_POWER_ENDPOINT").json()
    assert "power" not in json.dumps(detail.get("summary") or {})  # 报告面走 §12 引用
