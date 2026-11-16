from __future__ import annotations

import pytest

from perfeval.collectors.android._events import metric_event
from perfeval.core.summary import aggregate_summary


def test_summary_aggregation_builds_summary_json_shape() -> None:
    summary = aggregate_summary(
        "run-001",
        {
            "startup.cold.ms": [
                {
                    "value": 1000,
                    "unit": "ms",
                    "direction": "lower_better",
                    "sourceEvents": ["evt-1"],
                    "artifactRefs": ["artifacts/android/perfetto.pftrace"],
                    "evidenceLevel": "L1",
                },
                {
                    "value": 1000,
                    "unit": "ms",
                    "direction": "lower_better",
                    "sourceEvents": ["evt-2"],
                    "artifactRefs": ["artifacts/android/perfetto.pftrace"],
                    "evidenceLevel": "L2",
                },
                {
                    "value": 1000,
                    "unit": "ms",
                    "direction": "lower_better",
                    "sourceEvents": ["evt-3"],
                    "artifactRefs": ["artifacts/android/perfetto.pftrace"],
                    "evidenceLevel": "L2",
                },
                {
                    "value": 1000,
                    "unit": "ms",
                    "direction": "lower_better",
                    "sourceEvents": ["evt-4"],
                    "artifactRefs": ["artifacts/android/perfetto.pftrace"],
                    "evidenceLevel": "L2",
                },
                {
                    "value": 1000,
                    "unit": "ms",
                    "direction": "lower_better",
                    "sourceEvents": ["evt-5"],
                    "artifactRefs": ["artifacts/android/perfetto.pftrace"],
                    "evidenceLevel": "L2",
                },
                {
                    "value": 1500,
                    "unit": "ms",
                    "direction": "lower_better",
                    "sourceEvents": ["evt-6"],
                    "artifactRefs": ["artifacts/android/perfetto.pftrace"],
                    "evidenceLevel": "L1",
                },
            ],
            "frame.jank_rate": [
                {"value": 0.03, "unit": "ratio", "direction": "lower_better", "sourceEvents": ["evt-10"], "evidenceLevel": "L3"},
                {"value": 0.03, "unit": "ratio", "direction": "lower_better", "sourceEvents": ["evt-11"], "evidenceLevel": "L3"},
                {"value": 0.04, "unit": "ratio", "direction": "lower_better", "sourceEvents": ["evt-12"], "evidenceLevel": "L3"},
            ],
        },
        status="completed",
        context={
            "scenarioId": "startup_home",
            "device": {"model": "Pixel 6", "serialOrUdid": "R58R"},
            "app": {"versionName": "1.2.3"},
        },
    )

    assert summary["schemaVersion"] == "perfeval.summary.v1"
    assert summary["runId"] == "run-001"
    assert summary["status"] == "completed"
    assert summary["scenarioId"] == "startup_home"
    assert summary["device"]["model"] == "Pixel 6"

    startup = summary["metrics"]["startup.cold.ms"]
    assert startup["value"] == 1000.0
    assert startup["median"] == 1000.0
    assert startup["mad"] == 0.0
    assert startup["outlierCount"] == 1
    assert startup["confidence"] == "high"
    assert startup["sourceEvents"] == ["evt-1", "evt-2", "evt-3", "evt-4", "evt-5", "evt-6"]
    assert startup["artifactRefs"] == ["artifacts/android/perfetto.pftrace"]
    assert startup["evidenceLevel"] == "L1"

    jank = summary["metrics"]["frame.jank_rate"]
    assert jank["value"] == 0.03
    assert jank["confidence"] == "high"

    assert summary["evidenceLevel"] == ["L1", "L2", "L3"]
    assert summary["artifacts"] == [
        {
            "id": "perfetto",
            "path": "artifacts/android/perfetto.pftrace",
        }
    ]


def test_summary_ignores_non_finite_and_boolean_metric_values() -> None:
    summary = aggregate_summary(
        "run-invalid-values",
        {
            "metric.nan": [{"value": float("nan")}],
            "metric.inf": [{"value": float("inf")}],
            "metric.bool": [{"value": True}],
            "metric.valid": [{"value": 3}],
        },
    )

    assert set(summary["metrics"]) == {"metric.valid"}


def test_summary_preserves_focused_window_from_timeline() -> None:
    summary = aggregate_summary(
        "run-window",
        timeline_events=[
            metric_event(
                seq=1,
                run_id="run-window",
                name="frame.jank_rate",
                value=0.05,
                unit="ratio",
                collector="dumpsys.gfxinfo",
                data={"window": "com.example.app/com.example.app.PersonHomeComposeActivity"},
            )
        ],
    )

    assert summary["focusedWindow"] == "com.example.app/com.example.app.PersonHomeComposeActivity"
    assert summary["focusedWindows"] == ["com.example.app/com.example.app.PersonHomeComposeActivity"]


def _counter_event(seq: int, name: str, value: float, ts_ms: int) -> dict:
    return {
        "schemaVersion": "perfeval.timeline.v1",
        "seq": seq,
        "runId": "run-stream",
        "eventId": f"run-stream-sample-{seq:06d}",
        "type": "counter",
        "name": name,
        "tsMonotonicNs": ts_ms * 1_000_000,
        "source": {"platform": "android", "collector": "sampler.test", "evidenceLevel": "L1"},
        "data": {"value": value, "unit": "%", "direction": "none"},
    }


def test_summary_stream_metric_emits_series_and_truncates_samples() -> None:
    # 64 个 counter 观测（>= 32 判定为 stream），含一个明显峰值
    events = [
        _counter_event(seq=seq + 1, name="cpu.proc.pct", value=30.0 + (seq % 4), ts_ms=seq * 1000)
        for seq in range(64)
    ]
    events[40]["data"]["value"] = 96.5

    summary = aggregate_summary("run-stream", timeline_events=events)
    metric = summary["metrics"]["cpu.proc.pct"]

    assert metric["isStream"] is True
    assert metric["sampleCount"] == 64
    assert metric["seriesDownsampled"] is False
    assert len(metric["series"]) == 64
    assert metric["series"][0] == {"ts": 0, "v": 30.0}
    assert {"ts": 40000, "v": 96.5} in metric["series"]
    # 聚合统计仍对全量采样点计算
    assert metric["n"] == 64
    assert metric["max"] == 96.5
    # samples 只保留首尾各 3 条
    assert len(metric["samples"]) == 6
    assert metric["samples"][0]["value"] == 30.0
    assert metric["samples"][-1]["value"] == 30.0 + (63 % 4)


def test_summary_non_stream_metric_keeps_full_samples_and_no_series() -> None:
    events = [
        _counter_event(seq=seq + 1, name="cpu.proc.pct", value=30.0, ts_ms=seq * 1000)
        for seq in range(10)
    ]

    summary = aggregate_summary("run-short-stream", timeline_events=events)
    metric = summary["metrics"]["cpu.proc.pct"]

    assert "isStream" not in metric
    assert "series" not in metric
    assert "sampleCount" not in metric
    assert len(metric["samples"]) == 10


def test_summary_stream_series_downsamples_to_600_points_keeping_peaks() -> None:
    # 1500 个点：其中尖刺 99.0 只出现在少数桶中，必须被保留
    events = []
    for seq in range(1500):
        value = 20.0 + (seq % 5)
        if seq in {700, 1400}:
            value = 99.0
        events.append(_counter_event(seq=seq + 1, name="cpu.proc.pct", value=value, ts_ms=seq * 1000))

    summary = aggregate_summary("run-downsample", timeline_events=events)
    metric = summary["metrics"]["cpu.proc.pct"]

    assert metric["isStream"] is True
    assert metric["seriesDownsampled"] is True
    assert len(metric["series"]) <= 600
    assert metric["n"] == 1500
    values = [point["v"] for point in metric["series"]]
    assert 99.0 in values
    assert max(values) == 99.0
    assert metric["max"] == 99.0
    # 降采样只影响图表序列，聚合统计不变（均值含两个 99.0 尖刺）
    assert metric["mean"] == pytest.approx((1500 * 22.0 - 2 * 20.0 + 2 * 99.0) / 1500)
    assert len(metric["samples"]) == 6


def test_summary_stream_metric_stats_do_not_depends_on_series_order() -> None:
    events = [
        _counter_event(seq=seq + 1, name="cpu.proc.pct", value=float(seq), ts_ms=seq * 1000)
        for seq in range(40)
    ]

    summary = aggregate_summary("run-order", timeline_events=events)
    metric = summary["metrics"]["cpu.proc.pct"]

    ts_list = [point["ts"] for point in metric["series"]]
    assert ts_list == sorted(ts_list)
    assert metric["min"] == 0.0
    assert metric["max"] == 39.0


def test_summary_stream_series_points_carry_zone_tags() -> None:
    # CM-TASK-019 thermal 方案 (a)：带 zone tag 的 counter 事件在 series 点上保留 tags，
    # 供报告按温区拆分 thermal.max.c 序列
    events = []
    seq = 0
    for tick in range(20):
        for zone, value in (("cpu0", 40.0 + tick), ("gpu", 55.0 + tick)):
            seq += 1
            event = _counter_event(seq=seq, name="thermal.max.c", value=value, ts_ms=tick * 1000)
            event["tags"] = {"zone": zone}
            events.append(event)

    summary = aggregate_summary("run-zone", timeline_events=events)
    metric = summary["metrics"]["thermal.max.c"]

    assert metric["isStream"] is True
    assert len(metric["series"]) == 40
    assert {point["tags"]["zone"] for point in metric["series"]} == {"cpu0", "gpu"}
    assert metric["series"][0] == {"ts": 0, "v": 40.0, "tags": {"zone": "cpu0"}}


def test_summary_stream_series_points_without_tags_keep_point_shape() -> None:
    # 无 tags 的指标 series 点保持 {ts, v} 原形态（既有断言/消费方兼容）
    events = [
        _counter_event(seq=seq + 1, name="cpu.proc.pct", value=30.0, ts_ms=seq * 1000)
        for seq in range(40)
    ]

    summary = aggregate_summary("run-no-tags", timeline_events=events)

    assert summary["metrics"]["cpu.proc.pct"]["series"][0] == {"ts": 0, "v": 30.0}


def _merged_stream_events(*, run_ids: list[str], samples_per_run: int, window_ms: int | None = None) -> list[dict]:
    events: list[dict] = []
    seq = 1
    for run_id in run_ids:
        for index in range(samples_per_run):
            event = _counter_event(
                seq=seq,
                name="cpu.proc.pct",
                value=30.0 + (index % 4),
                ts_ms=index * 1000,
            )
            event["runId"] = run_id
            event["eventId"] = f"{run_id}-sample-{seq:06d}"
            if window_ms is not None:
                event["data"]["samplingWindowMs"] = window_ms
            events.append(event)
            seq += 1
    return events


def test_summary_stream_confidence_not_inflated_by_sample_count() -> None:
    # 单 run 64 点：观测数多不等于跨 run 重复，置信度不得高于 medium（实际 low）
    events = _merged_stream_events(run_ids=["run-a"], samples_per_run=64)

    summary = aggregate_summary("run-a", timeline_events=events)

    metric = summary["metrics"]["cpu.proc.pct"]
    assert metric["n"] == 64
    assert metric["confidence"] == "low"


def test_summary_stream_confidence_upgrades_only_with_repeat_runs() -> None:
    # 5 个 run x 40 点：repeatCount=5 达到最高档基数，序列完整、覆盖完整 -> high
    events = _merged_stream_events(
        run_ids=[f"run-{index}" for index in range(5)],
        samples_per_run=40,
    )

    summary = aggregate_summary("run-merged", timeline_events=events)

    assert summary["metrics"]["cpu.proc.pct"]["confidence"] == "high"


def test_summary_stream_confidence_downgrades_short_sequences() -> None:
    # 5 个 run x 每 run 仅 8 点（< 10）：降一档 -> medium
    events = _merged_stream_events(
        run_ids=[f"run-{index}" for index in range(5)],
        samples_per_run=8,
    )

    summary = aggregate_summary("run-merged-short", timeline_events=events)

    assert summary["metrics"]["cpu.proc.pct"]["confidence"] == "medium"


def test_summary_stream_confidence_downgrades_low_coverage() -> None:
    # 声明 60s 采样窗口但仅覆盖 30s（coverage < 0.6）：降一档 -> medium
    events = _merged_stream_events(
        run_ids=[f"run-{index}" for index in range(5)],
        samples_per_run=40,
        window_ms=80_000,
    )

    summary = aggregate_summary("run-merged-partial", timeline_events=events)

    assert summary["metrics"]["cpu.proc.pct"]["confidence"] == "medium"


def test_summary_stream_truncation_keeps_samples_per_run() -> None:
    # 跨 run 合并时按 run 各自保留首尾 3 条：趋势报告按 runId 取中位数，不能丢中间 repeat
    events: list[dict] = []
    seq = 1
    run_values: dict[str, list[float]] = {}
    for run_index in range(3):
        run_id = f"run-{run_index}"
        run_values[run_id] = []
        for index in range(40):
            value = 10.0 * run_index + index
            run_values[run_id].append(value)
            events.append(_counter_event(seq=seq, name="cpu.proc.pct", value=value, ts_ms=index * 1000))
            events[-1]["runId"] = run_id
            events[-1]["eventId"] = f"{run_id}-sample-{seq:06d}"
            seq += 1

    summary = aggregate_summary("run-merged", timeline_events=events)
    metric = summary["metrics"]["cpu.proc.pct"]

    samples = metric["samples"]
    assert metric["sampleCount"] == 120
    assert len(samples) == 18  # 3 run x 6 条
    by_run: dict[str, list[dict]] = {}
    for sample in samples:
        by_run.setdefault(sample["runId"], []).append(sample)
    assert set(by_run) == set(run_values)
    for run_id, run_samples in by_run.items():
        assert len(run_samples) == 6
        assert run_samples[0]["value"] == run_values[run_id][0]
        assert run_samples[-1]["value"] == run_values[run_id][-1]


def test_summary_non_stream_confidence_unchanged() -> None:
    summary = aggregate_summary(
        "run-non-stream",
        timeline_events=[
            metric_event(
                seq=seq,
                run_id="run-non-stream",
                name="startup.cold.ms",
                value=1000.0,
                unit="ms",
                collector="am_start",
            )
            for seq in range(1, 7)
        ],
    )

    assert summary["metrics"]["startup.cold.ms"]["confidence"] == "high"


def test_summary_peak_metric_value_is_series_max() -> None:
    # memory.pss.peak_mb 语义修正（CM-001 §5）：聚合值取序列真实最大值而非中位数
    events = [
        _counter_event(seq=seq + 1, name="memory.pss.peak_mb", value=300.0 + (seq % 5), ts_ms=seq * 2000)
        for seq in range(40)
    ]
    events[20]["data"]["value"] = 412.5

    summary = aggregate_summary("run-peak", timeline_events=events)
    metric = summary["metrics"]["memory.pss.peak_mb"]

    assert metric["isStream"] is True
    assert metric["value"] == 412.5
    assert metric["value"] == metric["max"]
    assert metric["median"] == 302.0  # 聚合统计保持全量口径，仅 value 换成峰值


def test_summary_non_peak_metric_value_stays_median() -> None:
    events = [
        _counter_event(seq=seq + 1, name="cpu.proc.pct", value=30.0 + (seq % 5), ts_ms=seq * 1000)
        for seq in range(40)
    ]
    events[20]["data"]["value"] = 96.5

    summary = aggregate_summary("run-median", timeline_events=events)
    metric = summary["metrics"]["cpu.proc.pct"]

    assert metric["value"] == metric["median"]
    assert metric["value"] != metric["max"]


# --- CM-TASK-009：FPS 派生（frame.fps.mean/median + jankSegments） ---


def _frame_detail_event(seq: int, value_ms: float, *, jank: bool, frame_index: int | None = None, fps_source: str = "framestats") -> dict:
    data = {
        "value": value_ms,
        "unit": "ms",
        "direction": "lower_better",
        "jank": jank,
        "flagNames": [],
        "fpsSource": fps_source,
    }
    if frame_index is not None:
        data["frameIndex"] = frame_index
    return {
        "schemaVersion": "perfeval.timeline.v1",
        "seq": seq,
        "runId": "run-fps",
        "eventId": f"run-fps-frame-{seq:03d}",
        "type": "counter",
        "name": "frame.duration.detail.ms",
        "tsMonotonicNs": seq * 16_000_000,
        "source": {
            "platform": "android",
            "collector": "dumpsys.gfxinfo.framestats",
            "evidenceLevel": "L1",
        },
        "data": data,
    }


def test_summary_derives_fps_metrics_from_frame_detail_events() -> None:
    # 帧耗时 10, 20, 30, 40 -> mean 25ms (40fps)，median 25ms (40fps)
    events = [
        _frame_detail_event(1, 10.0, jank=False),
        _frame_detail_event(2, 20.0, jank=False),
        _frame_detail_event(3, 30.0, jank=True),
        _frame_detail_event(4, 40.0, jank=True),
    ]

    summary = aggregate_summary("run-fps", timeline_events=events)

    fps_mean = summary["metrics"]["frame.fps.mean"]
    fps_median = summary["metrics"]["frame.fps.median"]
    assert fps_mean["value"] == pytest.approx(1000 / 25.0)
    assert fps_median["value"] == pytest.approx(1000 / 25.0)
    for entry in (fps_mean, fps_median):
        assert entry["unit"] == "fps"
        assert entry["direction"] == "higher_better"
        assert entry["n"] == 4
        assert entry["evidenceLevel"] == "L1"
        assert entry["derivedFrom"] == "frame.duration.detail.ms"
        assert entry["sourceEvents"] == [event["eventId"] for event in events]
    # fpsSource 提升为 summary 字段（METRIC_SOURCES §4 fps.source 标注）
    assert summary["fpsSource"] == "framestats"
    # 逐帧均值与逐帧 fps 均值口径不同：1000/mean != mean(1000/x)
    assert fps_mean["value"] != pytest.approx(sum(1000 / v for v in (10.0, 20.0, 30.0, 40.0)) / 4)


def test_summary_jank_segments_require_three_consecutive_jank_frames() -> None:
    # jank 分布：[1,2]（2 连，不算）、[4,5,6]（3 连）、[8]（单帧）
    janky = {1, 2, 4, 5, 6, 8}
    events = [
        _frame_detail_event(seq + 1, 40.0 if seq in janky else 10.0, jank=seq in janky, frame_index=seq)
        for seq in range(10)
    ]

    summary = aggregate_summary("run-jank", timeline_events=events)

    assert summary["jankSegments"] == [{"startIndex": 4, "endIndex": 6, "frameCount": 3}]


def test_summary_frame_derivation_absent_without_detail_events() -> None:
    summary = aggregate_summary(
        "run-no-frame",
        {"frame.jank_rate": [{"value": 0.1, "unit": "ratio"}]},
    )

    assert "frame.fps.mean" not in summary["metrics"]
    assert "frame.fps.median" not in summary["metrics"]
    assert "jankSegments" not in summary
    assert "fpsSource" not in summary


def test_summary_frame_distribution_degenerates_to_single_bin_for_equal_durations() -> None:
    events = [
        _frame_detail_event(seq + 1, 16.7, jank=False) for seq in range(5)
    ]

    summary = aggregate_summary("run-flat", timeline_events=events)

    assert summary["frameDurationDistribution"] == [{"upToMs": 16.7, "count": 5}]
    assert summary["jankSegments"] == []


# ---- 2026-11-04 体检修复回归（gpu-1 thermal 峰值口径 / power-2 能耗积分口径）----


def _zone_counter_event(seq: int, value: float, zone: str) -> dict:
    return {
        "schemaVersion": "perfeval.timeline.v1",
        "seq": seq,
        "runId": "run-thermal",
        "eventId": f"run-thermal-sample-{seq:06d}",
        "type": "counter",
        "name": "thermal.max.c",
        "tsMonotonicNs": seq * 1_000_000_000,
        "source": {"platform": "android", "collector": "sampler.gpu_thermal", "evidenceLevel": "L1"},
        "data": {"value": value, "unit": "°C", "direction": "lower_better"},
        "tags": {"zone": zone},
    }


def test_summary_thermal_max_c_headline_is_hottest_zone_reading() -> None:
    """gpu-1 修复：thermal.max.c headline = 最高温区温度（PEAK 白名单），不再取
    多温区混合中位数——真机多温区（ap 42.1 / bat 29.2）曾显示混合中位 30.8°C，
    系统性低估峰值 11.3°C 且进入对比引擎。"""
    events = []
    seq = 1
    for _tick in range(20):  # ap 恒 40.0、bat 恒 30.0（确认员构造样例同形态）
        events.append(_zone_counter_event(seq, 40.0, "ap"))
        seq += 1
        events.append(_zone_counter_event(seq, 30.0, "bat"))
        seq += 1

    summary = aggregate_summary("run-thermal", timeline_events=events)
    entry = summary["metrics"]["thermal.max.c"]
    assert entry["value"] == 40.0  # 旧混合中位数 = 35.0
    assert entry["max"] == 40.0
    assert entry["n"] == 40


def _energy_counter_event(seq: int, value: float) -> dict:
    return {
        "schemaVersion": "perfeval.timeline.v1",
        "seq": seq,
        "runId": "run-energy",
        "eventId": f"run-energy-sample-{seq:06d}",
        "type": "counter",
        "name": "power.energy.mah",
        "tsMonotonicNs": seq * 1_000_000_000,
        "source": {"platform": "android", "collector": "sampler.power", "evidenceLevel": "L2"},
        "data": {"value": value, "unit": "mAh", "direction": "lower_better", "semantic": "estimated"},
    }


def test_summary_energy_mah_headline_is_window_sum() -> None:
    """power-2 修复：power.energy.mah 聚合取 Σ 单窗增量（积分型指标豁免通用
    中位数）——real 降级链 300 窗 ×43.68mAh 曾显示 14.5mAh（真值 4368，量级差
    数百倍），报告「估算能耗 X mAh」caption 与对比引擎同读该 value。"""
    events = [_energy_counter_event(seq + 1, 43.68) for seq in range(300)]

    summary = aggregate_summary("run-energy", timeline_events=events)
    entry = summary["metrics"]["power.energy.mah"]
    assert abs(entry["value"] - 43.68 * 300) < 1e-6  # 会话总量（旧中位数 = 43.68）
    assert abs(entry["max"] - 43.68) < 1e-6
