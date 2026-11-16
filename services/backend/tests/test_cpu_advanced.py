"""R16 CPU 高阶采样器（契约 §21.2）：cpufreq 每核 / 规范化口径 / 线程级聚合。

fixture 驱动**生产采样器**（DeviceContext.command_runner 替身，与 perfeval
cpu_sampler 测试同款注入接缝）；真机 /sys、/proc 路径未实测（如实标注）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from perfeval.collectors.android.samplers.base import DeviceContext, SampleRecord  # noqa: E402

from app.services.collector_service import SAMPLER_BY_METRIC, _SyntheticGenerator  # noqa: E402
from app.services.cpu_advanced import (  # noqa: E402
    CpuFreqNormalizedSampler,
    CpuThreadSampler,
    build_cpufreq_command,
    build_task_stat_command,
    freq_ratio_mhz,
    parse_cpufreq_text,
    parse_task_stat_text,
)
from perfeval.collectors.android.samplers import SAMPLER_FACTORIES  # noqa: E402

CLK_TCK = 100


def _completed(stdout: str, returncode: int = 0):
    from types import SimpleNamespace

    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr="")


# ---- 解析纯函数 ----


def test_parse_cpufreq_text_marker_pairs() -> None:
    text = """\
__PDCUR__ cpu0
1400000
__PDCUR__ cpu0 max
1800000
__PDCUR__ cpu1
1800000
__PDCUR__ cpu1 max
1800000
__PDCUR__ cpu2
"""
    parsed = parse_cpufreq_text(text)
    assert parsed["cpu0"] == {"cur_khz": 1400000, "max_khz": 1800000}
    assert parsed["cpu1"] == {"cur_khz": 1800000, "max_khz": 1800000}
    # 缺值行（cat 失败）→ None，不猜
    assert parsed["cpu2"] == {"cur_khz": None, "max_khz": None}


def test_freq_ratio_mhz_sum_over_sum() -> None:
    frequencies = {
        "cpu0": {"cur_khz": 900000, "max_khz": 1800000},
        "cpu1": {"cur_khz": 1800000, "max_khz": 1800000},
    }
    # Σ当前/Σ最大 = (0.9+1.8)/(1.8+1.8) = 0.75
    assert abs(freq_ratio_mhz(frequencies) - 0.75) < 1e-9
    # 无有效读数 → None（不猜）
    assert freq_ratio_mhz({"cpu0": {"cur_khz": None, "max_khz": None}}) is None
    assert freq_ratio_mhz({}) is None


def test_parse_task_stat_text_comm_with_spaces() -> None:
    text = """\
__PDTASK__ /proc/12345/task/1000/stat
1000 (RenderThread) S 900 900 0 0 -1 0 0 0 0 0 12 4 0 0 20 0 1 0
__PDTASK__ /proc/12345/task/1001/stat
1001 (JIT mul-add worker) R 900 900 0 0 -1 0 0 0 0 0 3 1 0 0 20 0 1 0
__PDTASK__ /proc/12345/task/9999/stat
"""
    parsed = parse_task_stat_text(text)
    # utime/stime = 第 14/15 列（')' 后偏移 11/12，同 cpu_sampler 口径）
    assert parsed[1000].name == "RenderThread"
    assert (parsed[1000].utime, parsed[1000].stime) == (12, 4)
    assert parsed[1001].name == "JIT mul-add worker"
    assert (parsed[1001].utime, parsed[1001].stime) == (3, 1)
    assert 9999 not in parsed  # 标记下无值行不产线程


# ---- CpuFreqNormalizedSampler（fixture 端到端）----


_CPUMIX_FIXTURE = {
    # 核0 降频（900k/1800k）、核1 满频 → 比值 0.75
    "cpufreq": "__PDCUR__ cpu0\n900000\n__PDCUR__ cpu0 max\n1800000\n"
               "__PDCUR__ cpu1\n1800000\n__PDCUR__ cpu1 max\n1800000\n",
    "proc_t0": "cpu  100 0 50 1800 100 0 0 0 0 0\n"
               "cpu0 60 0 20 700 100 0 0 0 0 0\ncpu1 40 0 30 1100 0 0 0 0 0 0\n"
               "12345 (main) S 1 1 0 0 -1 0 0 0 0 0 12 12 0 0 20 0 1 0\n",
    "proc_t1": "cpu  100 0 50 1801 100 0 0 0 0 0\n"
               "cpu0 60 0 20 770 100 0 0 0 0 0\ncpu1 40 0 30 1130 0 0 0 0 0 0\n"
               "12345 (main) S 1 1 0 0 -1 0 0 0 0 0 24 24 0 0 20 0 1 0\n",
}


class _Runner:
    """可注入 command_runner：cpufreq/CLK_TCK/pidof//proc 批量读的应答源。"""

    def __init__(self, fixture: dict, clock: list[int]) -> None:
        self.fixture = fixture
        self.clock = clock  # [t0_ms, t1_ms, ...]（每次 /proc 读取递增消费）
        self.tick = 0

    def __call__(self, command: list[str], timeout: float | None):
        joined = " ".join(command)
        if "__PDCUR__" in command[-1]:
            return _completed(self.fixture["cpufreq"])
        if "getconf CLK_TCK" in joined:
            return _completed(f"{CLK_TCK}\n")
        if "pidof" in joined:
            return _completed("12345\n")
        if "/proc/stat" in joined:
            payload = self.fixture["proc_t0"] if self.tick == 0 else self.fixture["proc_t1"]
            self.tick += 1
            return _completed(payload)
        return _completed("")


def test_cpu_freq_normalized_sampler_records() -> None:
    sampler = CpuFreqNormalizedSampler(interval_ms=1000, core_count=2)
    clock = [0, 1000]
    runner = _Runner(_CPUMIX_FIXTURE, clock)

    class _Adb:
        @staticmethod
        def command(*args: str, serial: str | None = None) -> list[str]:
            return ["adb", *(["-s", serial] if serial else []), *args]

    ctx = DeviceContext(
        adb=_Adb(), command_runner=runner, serial="S1",
        package_name="com.example.game", elapsed_ms=lambda: clock[0],
    )
    assert sampler.probe(ctx).available is True
    sampler.start(ctx)  # 基线（tick=0 消费 proc_t0）

    clock[0] = 1000  # Δt = 1s
    runner.tick = 1
    records = sampler.sample(ctx)

    freq_records = [r for r in records if r.metric_id == "cpu.freq.mhz"]
    assert [(r.tags["core"], r.value) for r in freq_records] == [
        ("cpu0", 900.0), ("cpu1", 1800.0),
    ]
    # 规范化 = 未规范化 × (Σ当前/Σ最大)：未规范化 = (Δbusy/CLK_TCK)/Δt/核数×100
    # = ((24+24)-(12+12))/100/1/2×100 = 12%；× 0.75 = 9
    normalized = [r for r in records if r.metric_id == "cpu.proc.normalized.pct"]
    assert len(normalized) == 1
    assert abs(normalized[0].value - 9.0) < 1e-6
    assert normalized[0].tags["basis"] == "normalized"
    assert abs(normalized[0].tags["freqRatio"] - 0.75) < 1e-6


def test_cpu_freq_sampler_probe_unavailable_without_cpufreq() -> None:
    sampler = CpuFreqNormalizedSampler(interval_ms=1000, core_count=2)
    runner = _Runner({"cpufreq": "__PDCUR__ cpu0\n", "proc_t0": "", "proc_t1": ""}, [])

    class _Adb:
        @staticmethod
        def command(*args: str, serial: str | None = None) -> list[str]:
            return ["adb", *(["-s", serial] if serial else []), *args]

    ctx = DeviceContext(adb=_Adb(), command_runner=runner, serial="S", package_name=None)
    result = sampler.probe(ctx)
    assert result.available is False
    assert "cpufreq" in (result.reason or "")


# ---- 实机验证修复（2026-11-03）：命令构造禁止 `sh -c` + 多词脚本 ----
# 背景：adb 将 shell 后的 argv 以空格拼接发往设备，`sh -c "a && b"` 的引号全部
# 丢失 —— 设备侧变成 `sh -c a && b`，`sh -c` 只把首词当脚本（真机复现：频率
# 首核 marker 变空行错位、for 循环报 "syntax error: unexpected 'do'" 整条
# 不可用）。修正为单 argv 元素直传，设备 shell 自行解析。


def _assert_script_passed_verbatim(command: list[str]) -> None:
    """命令 argv 中不得出现 `sh -c`；脚本必须是 shell 之后的单个完整元素。"""
    assert "sh" not in command, f"命令仍经 sh -c（引号会丢失）: {command}"
    shell_idx = command.index("shell")
    assert len(command) - shell_idx == 2, f"shell 后应恰为一个脚本元素: {command}"


def test_build_cpufreq_command_no_sh_c() -> None:
    command = build_cpufreq_command(
        lambda *args, serial=None: ["adb", *(["-s", serial] if serial else []), *args],
        "S",
        2,
    )
    _assert_script_passed_verbatim(command)
    script = command[-1]
    assert "&&" in script and "2>/dev/null" in script  # 设备 shell 解析


def test_build_task_stat_command_no_sh_c() -> None:
    command = build_task_stat_command(
        lambda *args, serial=None: ["adb", *(["-s", serial] if serial else []), *args],
        "S",
        4583,
    )
    _assert_script_passed_verbatim(command)
    script = command[-1]
    # cpu-1 修复：同批先读 /proc/stat（核数自取），再 for 循环遍历线程 stat
    assert script.startswith("echo __PDSTAT__; cat /proc/stat 2>/dev/null; ")
    assert "for f in /proc/4583/task/*/stat; do" in script


# ---- CpuThreadSampler（fixture 端到端）----


def test_cpu_thread_sampler_per_thread_delta() -> None:
    sampler = CpuThreadSampler(interval_ms=2000, core_count=8)
    task_t0 = (
        "__PDTASK__ /proc/12345/task/1000/stat\n"
        "1000 (RenderThread) S 1 1 0 0 -1 0 0 0 0 0 10 6 0 0 20 0 1 0\n"
        "__PDTASK__ /proc/12345/task/1001/stat\n"
        "1001 (GameThread) R 1 1 0 0 -1 0 0 0 0 0 20 4 0 0 20 0 1 0\n"
    )
    task_t1 = (
        "__PDTASK__ /proc/12345/task/1000/stat\n"
        "1000 (RenderThread) S 1 1 0 0 -1 0 0 0 0 0 14 6 0 0 20 0 1 0\n"
        "__PDTASK__ /proc/12345/task/1001/stat\n"
        "1001 (GameThread) R 1 1 0 0 -1 0 0 0 0 0 24 6 0 0 20 0 1 0\n"
        "__PDTASK__ /proc/12345/task/1002/stat\n"
        "1002 (JIT) Z 1 1 0 0 -1 0 0 0 0 0 9 9 0 0 20 0 1 0\n"
    )
    responses = {
        "cpufreq": "", "proc_t0": "", "proc_t1": "",
        "task0": task_t0, "task1": task_t1,
    }
    state = {"tick": 0, "now_ms": 0}

    class _Adb:
        @staticmethod
        def command(*args: str, serial: str | None = None) -> list[str]:
            return ["adb", *(["-s", serial] if serial else []), *args]

    def runner(command: list[str], timeout: float | None):
        joined = " ".join(command)
        if "getconf CLK_TCK" in joined:
            return _completed(f"{CLK_TCK}\n")
        if "pidof" in joined:
            return _completed("12345\n")
        if "/proc/12345/task" in joined:
            payload = responses["task0"] if state["tick"] == 0 else responses["task1"]
            state["tick"] += 1
            return _completed(payload)
        return _completed("")

    ctx = DeviceContext(
        adb=_Adb(), command_runner=runner, serial="S1",
        package_name="com.example.game", elapsed_ms=lambda: state["now_ms"],
    )
    assert sampler.probe(ctx).available is True
    sampler.start(ctx)  # 基线（task0；CLK_TCK 探测）

    state["now_ms"] = 2000  # Δt = 2s
    state["tick"] = 1
    records: list[SampleRecord] = sampler.sample(ctx)
    by_thread = {r.tags["thread"]: r.value for r in records}
    # Δbusy/CLK_TCK/Δt/核数×100：RenderThread Δ=4 jiffies → 4/100/2/8×100 = 0.25%
    assert abs(by_thread["RenderThread"] - 0.25) < 1e-9
    # GameThread Δ=(24+6)-(20+4)=6 → 6/100/2/8×100 = 0.375%
    assert abs(by_thread["GameThread"] - 0.375) < 1e-9
    # 新线程（无基线）本窗口不产出
    assert "JIT" not in by_thread
    # 契约 §21.2 tags 口径：thread（线程名）+ tid
    assert all(r.tags.get("tid") for r in records)
    assert all(r.tags["unit"] == "%" for r in records)


def test_cpu_thread_sampler_process_restart_rebaseline() -> None:
    sampler = CpuThreadSampler(interval_ms=2000, core_count=8)
    task_before = (
        "__PDTASK__ /proc/12345/task/1000/stat\n"
        "1000 (main) S 1 1 0 0 -1 0 0 0 0 0 100 100 0 0 20 0 1 0\n"
    )
    task_after = (
        "__PDTASK__ /proc/99999/task/1000/stat\n"
        "1000 (main) S 1 1 0 0 -1 0 0 0 0 0 1 1 0 0 20 0 1 0\n"
    )
    state = {"tick": 0, "now_ms": 0}

    class _Adb:
        @staticmethod
        def command(*args: str, serial: str | None = None) -> list[str]:
            return ["adb", *(["-s", serial] if serial else []), *args]

    def runner(command: list[str], timeout: float | None):
        joined = " ".join(command)
        if "getconf CLK_TCK" in joined:
            return _completed(f"{CLK_TCK}\n")
        if "pidof" in joined:
            # start 消费 tick=0（旧 pid 12345）；此后进程重启为 99999
            return _completed("12345\n" if state["tick"] < 1 else "99999\n")
        if "/proc/" in joined and "/task/" in joined:
            payload = task_before if state["tick"] < 1 else task_after
            state["tick"] += 1
            return _completed(payload)
        return _completed("")

    ctx = DeviceContext(
        adb=_Adb(), command_runner=runner, serial="S1",
        package_name="com.example.game", elapsed_ms=lambda: state["now_ms"],
    )
    sampler.start(ctx)
    state["now_ms"] = 2000
    assert sampler.sample(ctx) == []  # pid 变化 → re-baseline，本窗口不产出
    # re-baseline 后新窗口恢复产出
    state["now_ms"] = 4000
    records = sampler.sample(ctx)
    assert records == [] or all(r.value >= 0 for r in records)


# ---- 注册表接线（backend 扩展 perfeval SAMPLER_FACTORIES）----


def test_sampler_registry_and_metric_mapping() -> None:
    for name in ("cpu_adv", "cpu_thread", "mem_detail"):
        assert name in SAMPLER_FACTORIES, name
    for metric_id, sampler_name in (
        ("cpu.freq.mhz", "cpu_adv"),
        ("cpu.proc.normalized.pct", "cpu_adv"),
        ("cpu.proc.thread.pct", "cpu_thread"),
        ("memory.detail.native_mb", "mem_detail"),
        ("memory.detail.dalvik_mb", "mem_detail"),
        ("memory.detail.gfx_mb", "mem_detail"),
        ("memory.detail.gl_mb", "mem_detail"),
    ):
        assert SAMPLER_BY_METRIC[metric_id] == sampler_name


def test_build_samplers_from_backend_factories() -> None:
    from perfeval.collectors.android.samplers import build_samplers

    samplers = build_samplers({
        "cpu_adv": {"intervalMs": 1000},
        "cpu_thread": {"intervalMs": 2000},
        "mem_detail": {"intervalMs": 2000},
    })
    assert {s.name for s in samplers} == {"cpu_adv", "cpu_thread", "mem_detail"}


# ---- synthetic 生成器（确定性）----


def test_synthetic_generator_deterministic_and_modeled() -> None:
    g1 = _SyntheticGenerator("sess-77", "cpu.freq.mhz")
    g2 = _SyntheticGenerator("sess-77", "cpu.freq.mhz")
    # 确定性：同种子同序列
    assert g1.next_values() == g2.next_values()
    freq_points = g1.next_values()
    assert len(freq_points) == 8  # 8 核各一条
    assert {tags["core"] for _v, tags in freq_points} == {f"cpu{n}" for n in range(8)}
    assert all(0 < value < 2500 for value, _tags in freq_points)

    norm = _SyntheticGenerator("sess-77", "cpu.proc.normalized.pct")
    value, tags = norm.next_values()[0]
    assert tags["basis"] == "normalized"
    assert tags["freqRatio"] in (1.0, 0.6)  # 降频演示窗比值
    assert 0 <= value <= 100

    thread_gen = _SyntheticGenerator("sess-77", "cpu.proc.thread.pct")
    thread_points = thread_gen.next_values()
    assert len(thread_points) == 6
    thread_names = [tags["thread"] for _v, tags in thread_points]
    assert "RenderThread" in thread_names and "GameThread" in thread_names
    # Σ线程份额 ≈ 进程占用（share 合一）
    assert abs(sum(share for _n, share in thread_gen._THREAD_SHARES) - 1.0) < 1e-9

    detail_gen = _SyntheticGenerator("sess-77", "memory.detail.native_mb")
    value, tags = detail_gen.next_values()[0]
    assert value > 0 and tags == {}


def test_synthetic_freq_downclock_window_demo() -> None:
    """降频观察演示：进入演示窗的采样点频率比值 = 0.6（契约 §21.2 口径）。"""
    gen = _SyntheticGenerator("sess-88", "cpu.proc.normalized.pct")
    ratios = []
    for _ in range(23 * 2):
        _value, tags = gen.next_values()[0]
        ratios.append(tags["freqRatio"])
    assert 0.6 in ratios and 1.0 in ratios
    # 降频窗长度 = 5（每 23 点周期内连续 5 点为 0.6）
    first = ratios.index(0.6)
    assert ratios[first : first + 5] == [0.6] * 5


# ---- 2026-11-04 体检修复回归（cpu-1/2/3/4/6）----


def test_freq_ratio_mhz_only_counts_cores_readable_on_both_sides() -> None:
    """cpu-2 修复：Σ 仅计入 cur/max 皆可读的核（模块 docstring 声明的交集口径）。

    旧实现 cur/max 各自独立求和：单核单侧缺读（热插拔竞态/SELinux 瞬断）比值被
    静默污染且可 >1（实测 1.0 → 1.1795 抬高 / 0.8478 压低双向偏移）。"""
    symmetric = {
        **{f"cpu{n}": {"cur_khz": 1800000, "max_khz": 1800000} for n in range(4)},
        **{f"cpu{n}": {"cur_khz": 2800000, "max_khz": 2800000} for n in range(4, 8)},
    }
    assert abs(freq_ratio_mhz(symmetric) - 1.0) < 1e-9

    # cpu4 max 缺读：交集口径下 Σcur=Σmax=15600000 → 1.0（旧独立求和 = 1.1795）
    cur_only = {**symmetric, "cpu4": {"cur_khz": 2800000, "max_khz": None}}
    assert abs(freq_ratio_mhz(cur_only) - 1.0) < 1e-9

    # cpu4 cur 缺读：同样交集剔除（旧独立求和 = 0.8478 压低）
    max_only = {**symmetric, "cpu4": {"cur_khz": None, "max_khz": 2800000}}
    assert abs(freq_ratio_mhz(max_only) - 1.0) < 1e-9

    # 全部单侧缺读 → None（不产出该点，不猜）
    assert freq_ratio_mhz({"cpu0": {"cur_khz": 1800000, "max_khz": None}}) is None
    assert freq_ratio_mhz({"cpu0": {"cur_khz": None, "max_khz": 1800000}}) is None


def test_cpu_thread_sampler_core_count_from_same_batch_stat() -> None:
    """cpu-1 修复：核数 = 同批 /proc/stat cpuN 行数（4 核模拟器形态），不再硬
    编码 8——PD_EMU_FPS_1103 实测 Σthreads 对 proc.pct 比值 ≈0.5（分母应 4）。"""
    stat_section = (
        "__PDSTAT__\n"
        "cpu  100 0 50 1800 100 0 0 0 0 0\n"
        "cpu0 60 0 20 700 100 0 0 0 0 0\n"
        "cpu1 40 0 30 1100 0 0 0 0 0 0\n"
        "cpu2 30 0 20 1200 0 0 0 0 0 0\n"
        "cpu3 20 0 10 1300 0 0 0 0 0 0\n"
    )
    task_t0 = stat_section + (
        "__PDTASK__ /proc/12345/task/1000/stat\n"
        "1000 (main) S 1 1 0 0 -1 0 0 0 0 0 10 6 0 0 20 0 1 0\n"
    )
    task_t1 = stat_section + (
        "__PDTASK__ /proc/12345/task/1000/stat\n"
        "1000 (main) S 1 1 0 0 -1 0 0 0 0 0 14 6 0 0 20 0 1 0\n"
    )
    state = {"tick": 0, "now_ms": 0}

    class _Adb:
        @staticmethod
        def command(*args: str, serial: str | None = None) -> list[str]:
            return ["adb", *(["-s", serial] if serial else []), *args]

    def runner(command: list[str], timeout: float | None):
        joined = " ".join(command)
        if "getconf CLK_TCK" in joined:
            return _completed(f"{CLK_TCK}\n")
        if "pidof" in joined:
            return _completed("12345\n")
        if "/proc/12345/task" in joined:
            payload = task_t0 if state["tick"] == 0 else task_t1
            state["tick"] += 1
            return _completed(payload)
        return _completed("")

    sampler = CpuThreadSampler(interval_ms=2000)  # 不传 core_count（工厂同形态）
    ctx = DeviceContext(
        adb=_Adb(), command_runner=runner, serial="S1",
        package_name="com.example.game", elapsed_ms=lambda: state["now_ms"],
    )
    assert sampler.probe(ctx).available is True
    sampler.start(ctx)
    state["now_ms"] = 2000
    records = sampler.sample(ctx)
    # Δbusy=4 jiffies / CLK_TCK=100 / Δt=2s / 4 核 ×100 = 0.5%（旧硬编码 8 → 0.25）
    assert len(records) == 1
    assert abs(records[0].value - 0.5) < 1e-9


def test_parse_stat_core_count_from_batch_text() -> None:
    """同批文本的 cpuN 行计数：聚合行/线程段不误计；缺 __PDSTAT__ 标记 → None。"""
    text = (
        "__PDSTAT__\n"
        "cpu  100 0 50 1800 100 0 0 0 0 0\n"
        "cpu0 60 0 20 700 100 0 0 0 0 0\n"
        "cpu1 40 0 30 1100 0 0 0 0 0 0\n"
        "cpu10 1 0 1 100 0 0 0 0 0 0\n"
        "__PDTASK__ /proc/12345/task/1000/stat\n"
        "1000 (cpu-worker) S 1 1 0 0 -1 0 0 0 0 0 1 1 0 0 20 0 1 0\n"
    )
    from app.services.cpu_advanced import parse_stat_core_count

    assert parse_stat_core_count(text) == 3
    assert parse_stat_core_count("cpu0 1 2 3\n__PDTASK__ /x\n") is None  # 无标记不猜


def test_cpu_freq_sampler_core_window_expands_to_device_cores() -> None:
    """cpu-6 修复：probe 按 /proc/stat cpuN 行数展开 cpufreq 读窗——10 核设备
    cpu8/cpu9 出点（旧固定 8 窗静默缺核）；显式 core_count 参数优先不覆盖。"""

    class _Adb:
        @staticmethod
        def command(*args: str, serial: str | None = None) -> list[str]:
            return ["adb", *(["-s", serial] if serial else []), *args]

    def _cpufreq_text(cores: int) -> str:
        parts = []
        for core in range(cores):
            parts.append(f"__PDCUR__ cpu{core}\n1800000\n__PDCUR__ cpu{core} max\n2800000\n")
        return "".join(parts)

    stat_text = "cpu  1 2 3 4 5 6 7 8 9 10\n" + "".join(
        f"cpu{n} 1 2 3 4 5 6 7 8 9 10\n" for n in range(10)
    )

    def runner(command: list[str], timeout: float | None):
        joined = " ".join(command)
        if "__PDCUR__" in command[-1]:
            return _completed(_cpufreq_text(10))
        if "/proc/stat" in joined:
            return _completed(stat_text)
        return _completed("")

    sampler = CpuFreqNormalizedSampler(interval_ms=1000)  # 缺省 8 → probe 后展开 10
    ctx = DeviceContext(adb=_Adb(), command_runner=runner, serial="S", package_name=None)
    assert sampler.probe(ctx).available is True
    assert sampler.core_count == 10

    records = sampler.sample(ctx)
    freq_cores = {r.tags["core"] for r in records if r.metric_id == "cpu.freq.mhz"}
    assert freq_cores == {f"cpu{n}" for n in range(10)}  # cpu8/cpu9 不再静默缺失

    # 显式 core_count 优先：不覆盖
    explicit = CpuFreqNormalizedSampler(interval_ms=1000, core_count=2)
    assert explicit.probe(ctx).available is True
    assert explicit.core_count == 2


def test_cpu_adv_and_thread_read_failure_streak_diagnostics() -> None:
    """cpu-4 修复（cpu_advanced 侧）：cpufreq / 线程 stat 读失败不再整窗静默——
    连续 3 次记 sampler.cpu_adv.freq.unavailable / sampler.cpu_thread.task.
    unavailable（恢复清零；进程消失不属读失败，由 §6.1 null 机制承载）。"""
    state = {"fail": False}

    class _Adb:
        @staticmethod
        def command(*args: str, serial: str | None = None) -> list[str]:
            return ["adb", *(["-s", serial] if serial else []), *args]

    def _make_runner(task_mode: bool):
        def runner(command: list[str], timeout: float | None):
            joined = " ".join(command)
            # 失败注入仅限数据读取命令（cpufreq/task stat）；pidof 失败属「进程
            # 消失」语义（§6.1 null 机制承载），不算读失败
            if state["fail"] and (task_mode if "/task/" in joined else "__PDCUR__" in command[-1]):
                raise TimeoutExpired("adb", timeout or 10)
            if task_mode:
                if "getconf CLK_TCK" in joined:
                    return _completed(f"{CLK_TCK}\n")
                if "pidof" in joined:
                    return _completed("12345\n")
                if "/proc/" in joined and "/task/" in joined:
                    return _completed("__PDSTAT__\ncpu0 1 2 3 4 5 6 7 8 9\n__PDTASK__ /proc/12345/task/1/stat\n1 (main) S 1 1 0 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0\n")
                return _completed("")
            if "__PDCUR__" in command[-1]:
                return _completed("__PDCUR__ cpu0\n1800000\n__PDCUR__ cpu0 max\n1800000\n")
            return _completed("")
        return runner

    from subprocess import TimeoutExpired

    # cpu_adv：probe 通过 → 连续 3 tick cpufreq 失败 → 一条诊断
    adv = CpuFreqNormalizedSampler(interval_ms=1000, core_count=1)
    ctx = DeviceContext(adb=_Adb(), command_runner=_make_runner(False), serial="S", package_name=None)
    assert adv.probe(ctx).available is True
    state["fail"] = True
    for _ in range(3):
        assert adv.sample(ctx) == []
    assert adv.drain_diagnostics() == [
        "sampler.cpu_adv.freq.unavailable:cpufreq read failed for 3 consecutive samples"
    ]
    state["fail"] = False
    adv.sample(ctx)
    assert adv.drain_diagnostics() == []

    # cpu_thread：start 基线成功 → 连续 3 tick task 读失败（pidof 正常，进程在）
    thread = CpuThreadSampler(interval_ms=2000)
    now = {"ms": 0}
    ctx2 = DeviceContext(
        adb=_Adb(), command_runner=_make_runner(True), serial="S",
        package_name="com.example.game", elapsed_ms=lambda: now["ms"],
    )
    thread.start(ctx2)
    state["fail"] = True
    for elapsed in (2000, 4000, 6000):
        now["ms"] = elapsed
        assert thread.sample(ctx2) == []
    assert thread.drain_diagnostics() == [
        "sampler.cpu_thread.task.unavailable:task stat read failed for 3 consecutive samples"
    ]


def test_retarget_cpu_thread_sampler_follows_process_name() -> None:
    """cpu-3 修复：processName 会话线程通道改道子进程——`_retarget_sampler` 换装
    内嵌 pidof 探测器为 ProcessTargetCpuSampler，与 cpu.proc.pct 同目标（此前
    thread.pct 仍 pidof 主包名，同会话两通道口径静默分裂）。"""
    from app.services.collector_service import _retarget_sampler

    calls: list[str] = []

    class _Adb:
        @staticmethod
        def command(*args: str, serial: str | None = None) -> list[str]:
            return ["adb", *(["-s", serial] if serial else []), *args]

    def runner(command: list[str], timeout: float | None):
        joined = " ".join(command)
        if "pidof" in joined:
            calls.append(joined)
            return _completed("200\n")
        return _completed("")

    sampler = CpuThreadSampler(interval_ms=2000)
    retargeted = _retarget_sampler(sampler, "com.example.game:game")
    assert retargeted is sampler
    assert retargeted._pid_probe.target_name == "com.example.game:game"

    ctx = DeviceContext(
        adb=_Adb(), command_runner=runner, serial="S1", package_name="com.example.game"
    )
    assert retargeted._resolve_pid(ctx) == 200
    assert calls[-1].endswith("pidof com.example.game:game")  # 子进程名（修复前为 com.example.game）


def test_synthetic_power_metrics_window_semantics() -> None:
    """power-2/3 修复（synthetic 侧）：energy 出单窗增量（非会话累计，与 real
    采样器口径一致，summary 聚合求和）；power.level.pct 出 %/min 速率（非电量
    百分比，与 real 降级通道及注册表 unit=%/min 一致）。"""
    energy = _SyntheticGenerator("sess-energy", "power.energy.mah")
    values = [energy.next_values()[0][0] for _ in range(50)]
    assert all(0 < v < 0.05 for v in values), values[:3]
    assert max(values) - min(values) < 0.05  # 无累计爬升

    level = _SyntheticGenerator("sess-level", "power.level.pct")
    rates = [level.next_values()[0][0] for _ in range(50)]
    assert all(0 < v < 10 for v in rates)  # %/min 速率形态（电量百分比会爬到 87）
