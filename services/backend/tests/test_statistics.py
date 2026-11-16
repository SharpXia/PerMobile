"""统计内核 golden 测试（M2 / R08-R11；ARCH §7「frameEvents 逐位可比对拍 [R§5]」）。

固定输入帧序列 → 逐位断言输出（含边界：恰等于阈值、<3 帧、全 jank、空序列）。
期望值均由公式手算复核（pvariance/pstdev/中位数/计数），并经本套件与内核互证；
输入序列来自确定性种子生成器（app/stats/synthetic.py）或手写数组。

口径注记（与契约 §12「字段单位与口径」一致）：
- var/std 为总体口径（statistics.pvariance/pstdev）；
- 输出小数位：var/stutter 2 位、其余 1 位（契约示例精度形态）；
- Jank 族阈值取公式值 1000/24×{1,2,3}（≈41.6667/83.3333/125.0ms），非显示取整。
"""

from __future__ import annotations

import math

import pytest

from app.stats import (
    BIG_JANK_THRESHOLD_MS,
    JANK_THRESHOLD_MS,
    SMALL_JANK_THRESHOLD_MS,
    fps_group,
    frame_statistics,
    frame_time_group,
    generate_frame_events,
    generate_frame_sequence,
    jank_classification,
    jank_group,
    low_fps_p1,
    smooth_index,
)

# ---- 常量阈值形态（公式 1000/24×k，[F§3.3]）----


def test_movie_frame_thresholds() -> None:
    assert math.isclose(SMALL_JANK_THRESHOLD_MS, 1000.0 / 24.0)
    assert math.isclose(JANK_THRESHOLD_MS, 1000.0 / 24.0 * 2.0)
    assert BIG_JANK_THRESHOLD_MS == 125.0


# ---- Jank 族（R08）golden：fixture A ----

FIXTURE_A = [10.0, 10.0, 10.0, 90.0, 10.0, 10.0, 10.0, 200.0, 10.0]
FIXTURE_A_DURATION_MS = 1000.0


def test_jank_group_fixture_a() -> None:
    # 手算：i=3 (90ms)：前三帧均值 10 → 90>20 且 >83.3333 → Jank；>125 否 → Small+Jank；
    # i=7 (200ms)：前三帧 (10,10,10) → 200>20 → Jank+BigJank+Small；
    # i=8 (10ms)：前三帧 (10,10,200) 均值 73.33 → 10>146.67 否。
    assert jank_group(FIXTURE_A, FIXTURE_A_DURATION_MS) == {
        "small": 2,
        "big": 1,
        "jank": 2,
        "stutter": 29.0,  # (90+200)/1000×100
        "per10min": 1200.0,  # 2×600000/1000
    }


def test_jank_classification_marks_only_judged_frames() -> None:
    flags = jank_classification(FIXTURE_A)
    assert flags["jank"] == [False, False, False, True, False, False, False, True, False]
    assert flags["bigJank"][7] is True and sum(flags["bigJank"]) == 1
    assert flags["smallJank"] == [False, False, False, True, False, False, False, True, False]


def test_frame_time_group_fixture_a() -> None:
    # avg = (10×7+90+200)/9 = 40.0；pvariance = (7×900+2500+25600)/9 = 3822.2222；
    # delta：|Δ| ∈ {0,0,80,80,0,0,190,190} → 2 次 >100ms → 2×3600000/1000 = 7200/h。
    assert frame_time_group(FIXTURE_A, FIXTURE_A_DURATION_MS) == {
        "avg": 40.0,
        "std": 61.8,  # √3822.2222 = 61.8241
        "var": 3822.22,
        "delta": 7200.0,
    }


def test_low_fps_p1_fixture_a() -> None:
    # n=9 → k = max(1, ceil(0.09)) = 1 → 最差帧 200ms → 1000/200 = 5.0
    assert low_fps_p1(FIXTURE_A) == 5.0


def test_smooth_fixture_a_single_window() -> None:
    # 帧时戳 0..280ms 全落首个 1s 窗 → 单窗 pstdev = 61.8241 → 61.8
    ts = [float(i * 35) for i in range(9)]
    assert smooth_index(FIXTURE_A, FIXTURE_A_DURATION_MS, ts) == 61.8


def test_frame_statistics_fixture_a_combined() -> None:
    combined = frame_statistics(FIXTURE_A, FIXTURE_A_DURATION_MS)
    assert combined["frameTime"] == frame_time_group(FIXTURE_A, FIXTURE_A_DURATION_MS)
    assert combined["jank"] == jank_group(FIXTURE_A, FIXTURE_A_DURATION_MS)
    assert combined["smooth"] == {"value": 61.8}
    assert combined["lowFps"] == {"p1": 5.0}


# ---- Jank 双条件边界（恰等于阈值 / 跨阈值 0.01ms）----


def test_jank_boundary_condition_a_exact() -> None:
    # 前三帧均值 50 → 条件 a 阈值 100：恰等于 100.0 → 严格大于不成立 → 不判
    assert jank_group([50.0, 50.0, 50.0, 100.0], 1000.0) == {
        "small": 0, "big": 0, "jank": 0, "stutter": 0.0, "per10min": 0.0,
    }
    # 100.01 > 100 且 > 83.3333 → Jank（未达 125 → 非 BigJank）
    assert jank_group([50.0, 50.0, 50.0, 100.01], 1000.0)["jank"] == 1
    assert jank_group([50.0, 50.0, 50.0, 100.01], 1000.0)["big"] == 0


def test_jank_boundary_movie_frame_exact() -> None:
    # 恰等于两帧电影帧显示值 83.33ms：< 公式阈值 83.3333 → 非 Jank；但 > 41.6667 → SmallJank
    result = jank_group([10.0, 10.0, 10.0, 83.33], 1000.0)
    assert result["jank"] == 0
    assert result["small"] == 1
    # 83.34 > 83.3333 → Jank
    assert jank_group([10.0, 10.0, 10.0, 83.34], 1000.0)["jank"] == 1


def test_jank_boundary_big_exact() -> None:
    # 125.0 恰等于三帧电影帧阈值 → 严格大于不成立 → BigJank 0（仍为 Jank）
    result = jank_group([10.0, 10.0, 10.0, 125.0], 1000.0)
    assert result["big"] == 0
    assert result["jank"] == 1
    assert jank_group([10.0, 10.0, 10.0, 125.01], 1000.0)["big"] == 1


def test_jank_boundary_small_exact() -> None:
    # 41.66 < 41.6667 → 非 SmallJank；41.67 > 41.6667 → SmallJank
    assert jank_group([10.0, 10.0, 10.0, 41.66], 1000.0)["small"] == 0
    assert jank_group([10.0, 10.0, 10.0, 41.67], 1000.0)["small"] == 1


def test_jank_first_three_frames_not_judged() -> None:
    # 前三帧无论多长都不判（[F§3.3]「前三帧平均」需三个前驱）
    assert jank_group([200.0, 500.0, 900.0], 1000.0)["jank"] == 0
    assert jank_group([200.0, 500.0], 1000.0)["jank"] == 0


def test_jank_uniform_high_frames_not_jank() -> None:
    # [F§3.3] 备注：帧率高/帧耗时长 ≠ 卡顿——均匀高耗时序列无 jank
    # （前三帧均值×2 恒 400 > 200，条件 a 不成立）
    assert jank_group([200.0] * 10, 2000.0)["jank"] == 0


def test_jank_all_jank_series() -> None:
    # 逐帧递增（每个 > 前三均值×2）→ 所有可判帧均为 Jank
    durations = [10.0, 10.0, 10.0, 90.0, 190.0, 390.0, 790.0]
    result = jank_group(durations, 10000.0)
    assert result == {
        "small": 4,  # 可判帧 4 帧全部 > 41.6667
        "big": 3,  # 190/390/790 > 125
        "jank": 4,
        "stutter": 14.6,  # (90+190+390+790)/10000×100
        "per10min": 240.0,  # 4×600000/10000
    }
    assert frame_time_group(durations, 10000.0) == {
        "avg": 212.9,  # 1490/7 = 212.857
        "std": 268.2,
        "var": 71934.69,
        "delta": 720.0,  # |Δ|>100ms：200、400 → 2×3600000/10000
    }
    assert low_fps_p1(durations) == 1.3  # k=1 → 1000/790 = 1.266


# ---- 边界：<3 帧 / 空序列 ----


def test_two_frames_no_jank_but_ftime_computes() -> None:
    assert jank_group([50.0, 200.0], 1000.0) == {
        "small": 0, "big": 0, "jank": 0, "stutter": 0.0, "per10min": 0.0,
    }
    assert frame_time_group([50.0, 200.0], 1000.0) == {
        "avg": 125.0, "std": 75.0, "var": 5625.0, "delta": 3600.0,
    }
    assert low_fps_p1([50.0, 200.0]) == 5.0


def test_empty_sequence_all_none() -> None:
    combined = frame_statistics([], 0)
    assert combined["frameTime"] == {"avg": None, "std": None, "var": None, "delta": None}
    assert combined["jank"] == {"small": None, "big": None, "jank": None, "stutter": None, "per10min": None}
    assert combined["smooth"] == {"value": None}
    assert combined["lowFps"] == {"p1": None}


# ---- 1%Low 取整口径 ----


def test_low_fps_p1_sampling_rule() -> None:
    # k = max(1, ceil(n×1%))：n=250 → 3 帧（1000+900+800 均值 900 → 1.11 → 1.1）
    assert low_fps_p1([1000.0, 900.0, 800.0] + [10.0] * 247) == 1.1
    # n=100 → k=1（恰 1%）；n=99 → k=1（不足 1% 取最差 1 帧）
    assert low_fps_p1([1000.0] + [10.0] * 99) == 1.0
    assert low_fps_p1([1000.0] + [10.0] * 98) == 1.0


# ---- Smooth 行为近似（窗口口径；数值不可与 PerfDog 对比，报告恒带 note）----


def test_smooth_windowed_mean_of_per_second_stddev() -> None:
    # 窗 1（0-999ms）帧 [10,30] → pstdev 10；窗 2（1000-1999ms）帧 [20,20] → 0；
    # Smooth = (10+0)/2 = 5.0
    assert smooth_index([10.0, 30.0, 20.0, 20.0], 2000.0, [0.0, 500.0, 1000.0, 1500.0]) == 5.0


def test_smooth_ts_vs_uniform_paths_differ() -> None:
    # ts 路径：帧 (10,30) 落窗 0、(20) 落窗 1 → (10+0)/2 = 5.0；
    # 均匀铺开路径（无时戳）：三点同窗 → pstdev(10,30,20) = 8.165 → 8.2
    assert smooth_index([10.0, 30.0, 20.0], 1000.0, [0.0, 0.0, 1500.0]) == 5.0
    assert smooth_index([10.0, 30.0, 20.0], 1000.0) == 8.2


def test_smooth_stable_series_low_value() -> None:
    # 稳定 120fps：抖动 ±0.5ms → Smooth 远低于官方建议阈值（游戏 <8，[F§3.5]）
    stable = [8.3 + 0.5 * ((-1) ** i) for i in range(120)]
    value = smooth_index(stable, 1000.0, [float(i * 8.3) for i in range(120)])
    assert value is not None and value < 8.0


# ---- FPS 统计矩阵（R11）----


def test_fps_group_fixture_b() -> None:
    values = [60.0, 60.0, 60.0, 50.0, 60.0, 60.0]
    assert fps_group(values, 30000.0, [18, 25, 55]) == {
        "avg": 58.3,  # 350/6 = 58.333
        "var": 13.89,  # (5×(5/3)² + (25/3)²)/6 = 13.8889
        "std": 3.7,  # 3.7268
        "drop": 120.0,  # 60→50 降 10 > 8：1 次 × 3600000/30000
        "min": 50.0,
        "median": 60.0,
        "medRange": 100.0,  # ±20% of 60 = [48,72] 全含
        "ge": {"18": 100.0, "25": 100.0, "55": 83.3},  # 5/6 = 83.33
    }


def test_fps_drop_boundary_exactly_8() -> None:
    # 相邻点下降恰 8 帧：不满足「>8」（[F§3.1]）→ 0
    assert fps_group([60.0, 52.0], 60000.0, [18])["drop"] == 0.0
    # 下降 8.1 → 计 1 次 × 3600000/60000 = 60/h
    assert fps_group([60.0, 51.9], 60000.0, [18])["drop"] == 60.0


def test_fps_drop_multiple_points() -> None:
    # 3 次降帧（60→50、50→40、40→30）× 3600000/70000 = 154.2857 → 154.3
    values = [60.0, 60.0, 60.0, 50.0, 50.0, 40.0, 40.0, 30.0]
    assert fps_group(values, 70000.0, [18])["drop"] == 154.3
    assert fps_group(values, 70000.0, [18])["medRange"] == 87.5  # 7/8
    assert fps_group(values, 70000.0, [18])["median"] == 50.0


def test_fps_medrange_inclusive_bounds() -> None:
    # ±20% 含端点：80 与 120 恰在边界 → 全含
    assert fps_group([80.0, 100.0, 120.0], 60000.0, [18])["medRange"] == 100.0
    assert fps_group([79.9, 100.0, 120.1], 60000.0, [18])["medRange"] == 33.3


def test_fps_ge_inclusive_and_key_order() -> None:
    body = fps_group([20.0, 30.0], 60000.0, [30, 18])
    # 键按阈值数值升序（契约 §12），且 ≥ 阈值含等于
    assert list(body["ge"]) == ["18", "30"]
    assert body["ge"] == {"18": 100.0, "30": 50.0}


def test_fps_single_point_zero_duration() -> None:
    # 单点：durationMs=0（首尾差 0）→ 速率字段防守性取 0，统计量按单点照常
    assert fps_group([57.5], 0.0, [18, 25]) == {
        "avg": 57.5, "var": 0.0, "std": 0.0, "drop": 0.0,
        "min": 57.5, "median": 57.5, "medRange": 100.0,
        "ge": {"18": 100.0, "25": 100.0},
    }


def test_fps_empty_all_none_with_ge_keys() -> None:
    body = fps_group([], 0.0, [18, 25])
    for field in ("avg", "var", "std", "drop", "min", "median", "medRange"):
        assert body[field] is None
    assert body["ge"] == {"18": None, "25": None}


# ---- 确定性种子帧序列（synthetic 输入源）----


def test_synthetic_frame_sequence_deterministic() -> None:
    assert generate_frame_sequence("golden", 500) == generate_frame_sequence("golden", 500)
    assert generate_frame_sequence("golden", 500) != generate_frame_sequence("other", 500)
    events = generate_frame_events("golden", 500)
    assert len(events) == 500
    # 帧起点按显示时间连续推进（上一帧起点 + 上一帧耗时）
    for earlier, later in zip(events, events[1:]):
        assert later.ts_ms == pytest.approx(earlier.ts_ms + earlier.duration_ms)
    assert events[0].ts_ms == 0.0


def test_synthetic_sequence_exercises_kernel_paths() -> None:
    durations = generate_frame_sequence("golden", 2000)
    assert all(d > 0 for d in durations)
    result = jank_group(durations, sum(durations))
    # 序列含中等/严重尖刺 → Jank/BigJank/SmallJank 各至少 1 次（分支覆盖）
    assert result["jank"] >= 1
    assert result["big"] >= 1
    assert result["small"] >= result["jank"]  # Small 阈值最低，恒为 Jank 扩集
