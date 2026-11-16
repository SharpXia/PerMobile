"""M7 测试 fixture（契约 §22，DEVELOPMENT_PLAN R38/R50/R51）。

如实标注环境限制：本机无 trace_processor 二进制（``which trace_processor_shell``
已验证为空）也无可选 ``perfetto`` Python 包——SQL 执行路径以**假二进制回放
canned CSV** 驱动（perfeval ``tests/test_perfetto_trace.py`` 同款零真二进制模式：
真实二进制同为 CSV 输出，解析/换算/装配全走生产代码）。真 trace_processor 对
真实 trace 的查询路径未实测。

- golden perfetto trace：perfeval golden 47KB ``frame-timeline.pftrace`` 原样字节
  复制进 run ``artifacts/trace.pftrace``（真机 Android 16 采集产物作 fixture）；
- canned CSV 的帧值与契约 §22.1 golden 同源（App Deadline Missed jank、8.33ms
  vsync、com.example.game 进程），clock 对给 BOOTTIME/REALTIME 供 epoch 对齐；
- 诊断合成序列：已知相邻点下降 + 三类方向明确的伴随指标（反相线性/同相强相关/
  噪声），供相关系数方向与候选排序对拍。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDEN_TRACE_PATH = (
    REPO_ROOT / "perfeval" / "fixtures" / "android" / "golden" / "frame-timeline.pftrace"
)

FIXTURE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + b"\x00" * 16

# ---- canned CSV（帧值与契约 §22.1 golden 同源；clock 对供 epoch 换算）----

FRAMES_CSV = (
    '"ts","dur","track_name","surface_frame_token","upid","pid","process_name",'
    '"jank_type","present_type","on_time_finish","display_frame_token","expected_dur"\n'
    '1000000000,8333333,"Expected Timeline","11",3,12345,"com.example.game",'
    '"[NULL]","[NULL]","[NULL]",11,"[NULL]"\n'
    '1700000000,8333333,"Expected Timeline","12",3,12345,"com.example.game",'
    '"[NULL]","[NULL]","[NULL]",12,"[NULL]"\n'
    '1800000000,8333333,"Expected Timeline","13",3,12345,"com.example.game",'
    '"[NULL]","[NULL]","[NULL]",13,"[NULL]"\n'
    '1000000000,8900000,"Actual Timeline","11",3,12345,"com.example.game",'
    '"None","On-time Present",1,11,8333333\n'
    '1700000000,33300000,"Actual Timeline","12",3,12345,"com.example.game",'
    '"App Deadline Missed","Late Present",0,12,8333333\n'
    '1800000000,16500000,"Actual Timeline","13",3,12345,"com.example.game",'
    '"SurfaceFlinger Stuffing","Late Present",1,13,8333333\n'
)

# BOOTTIME=1e9、REALTIME=1798765430000000000（同 snapshot）→ offset =
# REALTIME - BOOTTIME = 1798765429000000000 ns；帧 epoch ms = (ts+offset)//1e6：
# 帧0 → 1798765430000、帧1 → 1798765430700、帧2 → 1798765430800。
CLOCK_CSV = (
    '"ts","clock_id","clock_name","clock_value","snapshot_id"\n'
    '1000000000,6,"BOOTTIME",1000000000,0\n'
    '1000000000,1,"REALTIME",1798765430000000000,0\n'
)

# thread_state 片段全部落在帧1时间窗 [1700000000, 1733300000) ns 内（契约 §22.1
# golden 同款线程：GameThread Running 12.4ms / RenderThread Runnable 2.1ms；
# 另加一行 Sleeping 验证状态映射）。
SCHED_CSV = (
    '"ts","dur","raw_state","cpu","tid","thread_name","pid","process_name"\n'
    '1702000000,12400000,"Running",4,24271,"GameThread",12345,"com.example.game"\n'
    '1715000000,2100000,"R",5,24272,"RenderThread",12345,"com.example.game"\n'
    '1725000000,1000000,"S",4,24271,"GameThread",12345,"com.example.game"\n'
)

# 帧1窗口边界（服务以帧 ts/dur 构造 SQL WHERE 边界；fake 二进制转存收到的 SQL
# 供断言窗口构造正确——真二进制按 WHERE 过滤，fake 回放全集）。
FRAME1_WINDOW_START_NS = 1_700_000_000
FRAME1_WINDOW_END_NS = 1_700_000_000 + 33_300_000


def build_fake_trace_processor(directory: Path) -> Path:
    """生成假 trace_processor_shell：转存收到的 SQL，按 SQL 内容回放 canned CSV。"""
    directory.mkdir(parents=True, exist_ok=True)
    frames = directory / "frames.csv"
    clock = directory / "clock.csv"
    sched = directory / "sched.csv"
    frames.write_text(FRAMES_CSV, encoding="utf-8")
    clock.write_text(CLOCK_CSV, encoding="utf-8")
    sched.write_text(SCHED_CSV, encoding="utf-8")
    script = (
        "#!/bin/sh\n"
        'prev=""\nsql=""\n'
        'for arg in "$@"; do\n'
        '  if [ "$prev" = "--query-string" ]; then sql="$arg"; fi\n'
        '  prev="$arg"\n'
        "done\n"
        f'printf \'%s\' "$sql" > "{directory / "last_sql.txt"}"\n'
        'case "$sql" in\n'
        f'  *clock_snapshot*) cat "{clock}" ;;\n'
        f'  *thread_state*) cat "{sched}" ;;\n'
        f"  *'from slice'*) cat \"{frames}\" ;;\n"
        '  *) echo "unexpected query" >&2; exit 9 ;;\n'
        "esac\n"
    )
    binary = directory / "trace_processor_shell"
    binary.write_text(script, encoding="utf-8")
    binary.chmod(0o755)
    return binary


def last_sql(fake_dir: Path) -> str:
    """读回假二进制最近一次收到的 SQL（窗口构造断言用）。"""
    return (fake_dir / "last_sql.txt").read_text(encoding="utf-8")


def make_trace_run(client: Any, case_name: str = "PD_M7_TRACE") -> str:
    """golden .pftrace 原样字节复制进 run 工件 → 返回 runId。"""
    run_path = client.runs_dir / case_name
    artifacts = run_path / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(GOLDEN_TRACE_PATH, artifacts / "trace.pftrace")
    return f"run-{case_name}"


# ---- 诊断合成序列（已知下降 + 已知方向指标）----

TS_BASE = 1_798_765_400_000
STEP_MS = 500
# 锚指标 frame.fps.mean：10 点基线 60 → 触发点 45（60→45 = 25% 相邻点跌幅 ≥15%）
# → 区间内低值波动（恢复线 51 之下）→ 收口后回 60。
FPS_DROP_SERIES = [60.0] * 10 + [45.0, 44.0, 46.0, 42.0, 45.0, 43.0, 41.0, 44.0, 42.0, 40.0] + [60.0] * 4
# 期望下降区间（默认 -15%/1000ms）：from=+5000（首个下跌点）、to=+9500（恢复线
# 下最后一点）、duration 4500、dropPct=(60-40)/60*100=33.3。
EXPECTED_INTERVAL = {
    "fromMs": TS_BASE + 5_000,
    "toMs": TS_BASE + 9_500,
    "durationMs": 4_500,
    "dropPct": 33.3,
    "baseline": 60.0,
    "trough": 40.0,
}


def drop_series_rows() -> Iterable[dict[str, Any]]:
    """samples.ndjson 行：锚 + 反相线性 CPU（r=-1）+ 同相强相关内存（r≈0.92）
    + 噪声网络（|r|≈0.33）+ 稀疏功率（反相 r=-1 但仅 5 对 → 可用性降权 0.8）。"""
    rows: list[dict[str, Any]] = []
    for index, fps in enumerate(FPS_DROP_SERIES):
        ts = TS_BASE + index * STEP_MS
        rows.append({"tsMs": ts, "metricId": "frame.fps.mean", "value": fps, "tags": {}})
    in_window = FPS_DROP_SERIES[10:20]
    # CPU：与 FPS 完全反相线性（fps 跌 → cpu 升）→ 皮尔逊 -1（方向对拍）
    for offset, fps in enumerate(in_window):
        rows.append(
            {
                "tsMs": TS_BASE + (10 + offset) * STEP_MS,
                "metricId": "cpu.total.pct",
                "value": 120.0 - fps,
                "tags": {},
            }
        )
    # 基线段 CPU（区间外，不参与配对）
    for index in list(range(10)) + list(range(20, 24)):
        rows.append(
            {"tsMs": TS_BASE + index * STEP_MS, "metricId": "cpu.total.pct", "value": 30.0, "tags": {}}
        )
    # 内存：与 FPS 同相（40+fps±小幅扰动）→ 强正相关（r≈0.92）
    wobble = [0.0, 1.0, -1.0, 0.0, 1.0, -1.0, 0.0, 1.0, -1.0, 0.0]
    for offset, (fps, w) in enumerate(zip(in_window, wobble)):
        rows.append(
            {
                "tsMs": TS_BASE + (10 + offset) * STEP_MS,
                "metricId": "memory.pss.peak_mb",
                "value": 40.0 + fps + w,
                "tags": {},
            }
        )
    for index in list(range(10)) + list(range(20, 24)):
        rows.append(
            {"tsMs": TS_BASE + index * STEP_MS, "metricId": "memory.pss.peak_mb", "value": 100.0, "tags": {}}
        )
    # 网络：区间内 10/90 交替噪声（与 FPS 无构造关系）→ |r| 小
    for offset in range(10):
        rows.append(
            {
                "tsMs": TS_BASE + (10 + offset) * STEP_MS,
                "metricId": "net.rx.kbps",
                "value": 10.0 if offset % 2 == 0 else 90.0,
                "tags": {},
            }
        )
    # 功率：仅 5 个稀疏点（1000ms 节拍）反相线性 → r=-1 但样本对 5 → weight 0.8
    for ts, fps in (
        (5_000, 45.0),
        (6_000, 46.0),
        (7_000, 45.0),
        (8_000, 41.0),
        (9_000, 42.0),
    ):
        rows.append(
            {
                "tsMs": TS_BASE + ts,
                "metricId": "power.current.ma",
                "value": 100.0 - fps,
                "tags": {},
            }
        )
    return rows


def flat_series_rows() -> list[dict[str, Any]]:
    """无下降对照序列：恒 60，无区间（dropIntervals=[] 且 note=null）。"""
    return [
        {"tsMs": TS_BASE + index * STEP_MS, "metricId": "frame.fps.mean", "value": 60.0, "tags": {}}
        for index in range(24)
    ]


def write_run_samples(client: Any, case_name: str, rows: Iterable[dict[str, Any]]) -> str:
    """写 run 目录 samples.ndjson（read_samples 同构行）→ 返回 runId。"""
    run_path = client.runs_dir / case_name
    run_path.mkdir(parents=True, exist_ok=True)
    with (run_path / "samples.ndjson").open("w", encoding="utf-8") as sink:
        for row in rows:
            sink.write(json.dumps(row, sort_keys=True) + "\n")
    return f"run-{case_name}"


def drain_events(bus: Any) -> list[dict[str, Any]]:
    """排空 SseBus 订阅队列（诊断断言用）。"""
    events: list[dict[str, Any]] = []
    while not bus.empty():
        events.append(bus.get())
    return events


def completed(stdout: str = "", returncode: int = 0) -> Any:
    result = type("_R", (), {})()
    result.stdout = stdout
    result.returncode = returncode
    result.stderr = ""
    return result


# 供 timed-screenshot 测试复用（真实 m4 fixture 的 real client + recording 会话）
from m4_fixtures import (  # noqa: E402,F401  （re-export 统一 M7 测试导入面）
    FakeClock,
    fake_screenrecord_runner,
    make_real_client,
    make_recording_session,
)
