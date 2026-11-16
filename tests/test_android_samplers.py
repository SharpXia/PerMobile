from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from perfeval.collectors.android import AdbClient, parse_android_artifacts
from perfeval.collectors.android.samples import SamplesWriter, parse_samples_events, read_samples
from perfeval.collectors.android.samplers import (
    SAMPLER_FACTORIES,
    CpuSampler,
    DeviceContext,
    GpuThermalSampler,
    MemSampler,
    NetSampler,
    PowerSampler,
    ProbeResult,
    SampleRecord,
    SamplingScheduler,
    build_samplers,
    samplers_from_config,
)
from perfeval.collectors.android.samplers.cpu_sampler import CpuTimes, ProcTimes, _busy_pct, parse_stat_text
from perfeval.collectors.android.samplers.gpu_thermal_sampler import GPUBUSY_PATH
from perfeval.collectors.android.samplers.net_sampler import (
    parse_netstats_totals,
    parse_qtaguid_stats,
)
from perfeval.core.summary import aggregate_summary
from perfeval.core.timeline import TimelineEvent


@dataclass
class FakeSampler:
    """可控 fake：探测结果、start/sample/stop 各阶段异常与产出均可注入。"""

    name: str
    metric_ids: tuple[str, ...] = ("fake.metric",)
    interval_ms: int = 100
    probe_result: ProbeResult = field(default_factory=ProbeResult.ok)
    probe_error: Exception | None = None
    start_error: Exception | None = None
    sample_error: Exception | None = None
    stop_error: Exception | None = None
    sample_records: tuple[SampleRecord, ...] = ()
    stop_records: tuple[SampleRecord, ...] = ()
    started: bool = False
    stopped: bool = False
    sample_count: int = 0

    def probe(self, ctx: DeviceContext) -> ProbeResult:
        if self.probe_error is not None:
            raise self.probe_error
        return self.probe_result

    def start(self, ctx: DeviceContext) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.started = True

    def sample(self, ctx: DeviceContext) -> list[SampleRecord]:
        self.sample_count += 1
        if self.sample_error is not None:
            raise self.sample_error
        return list(self.sample_records)

    def stop(self) -> list[SampleRecord]:
        self.stopped = True
        if self.stop_error is not None:
            raise self.stop_error
        return list(self.stop_records)


def _context(**overrides: Any) -> DeviceContext:
    values: dict[str, Any] = {
        "adb": AdbClient("/adb"),
        "command_runner": lambda command, timeout: subprocess.CompletedProcess(command, 0, "", ""),
        "serial": "emulator-5554",
        "package_name": "com.example.app",
    }
    values.update(overrides)
    return DeviceContext(**values)


def _record(metric_id: str = "fake.metric", value: float = 1.0) -> SampleRecord:
    return SampleRecord(metric_id=metric_id, value=value, tags={"unit": "%"})


def test_sample_record_to_dict_matches_ndjson_contract() -> None:
    record = SampleRecord(metric_id="memory.pss.peak_mb", value=123.4, ts_ms=2000, tags={"unit": "MB"})

    assert record.to_dict() == {
        "tsMs": 2000,
        "metricId": "memory.pss.peak_mb",
        "value": 123.4,
        "tags": {"unit": "MB"},
    }


def test_scheduler_probe_drops_unavailable_samplers_with_diagnostic() -> None:
    good = FakeSampler(name="good")
    bad = FakeSampler(name="broken_probe", probe_result=ProbeResult.unavailable("no kgsl node"))
    error = FakeSampler(name="error_probe", probe_error=RuntimeError("adb offline"))
    collected: list[SampleRecord] = []
    scheduler = SamplingScheduler(
        [good, bad, error],
        context=_context(),
        on_record=collected.append,
        clock=lambda: 0.0,
    )

    scheduler.probe()

    assert scheduler.active_names == ["good"]
    assert "sampler.broken_probe.unavailable:no kgsl node" in scheduler.diagnostics
    assert "sampler.error_probe.unavailable:adb offline" in scheduler.diagnostics


def test_scheduler_start_isolates_start_failures() -> None:
    good = FakeSampler(name="good")
    bad = FakeSampler(name="broken_start", start_error=RuntimeError("permission denied"))
    scheduler = SamplingScheduler([good, bad], context=_context(), clock=lambda: 0.0)
    scheduler.probe()

    scheduler.start()
    scheduler.stop()

    assert scheduler.active_names == ["good"]
    assert good.started is True
    assert bad.started is False
    assert "sampler.broken_start.failed:permission denied" in scheduler.diagnostics


def test_scheduler_poll_once_respects_interval_and_isolates_sample_errors() -> None:
    good = FakeSampler(name="good", interval_ms=100, sample_records=(_record("good.metric", 5.0),))
    broken = FakeSampler(name="broken", interval_ms=100, sample_error=RuntimeError("dump crashed"))
    now = [0.0]
    collected: list[SampleRecord] = []
    scheduler = SamplingScheduler(
        [good, broken],
        context=_context(),
        on_record=collected.append,
        clock=lambda: now[0],
    )
    scheduler.probe()

    assert scheduler.poll_once() == 2  # 首轮立即采样
    assert collected == [SampleRecord(metric_id="good.metric", value=5.0, ts_ms=0, tags={"unit": "%"})]
    assert "sampler.broken.failed:dump crashed" in scheduler.diagnostics

    now[0] = 0.05
    assert scheduler.poll_once() == 0  # 未到 interval，跳过
    now[0] = 0.15
    assert scheduler.poll_once() == 2  # 到期再采样，broken 继续被隔离不阻塞 good
    assert good.sample_count == 2


def test_scheduler_stamps_elapsed_ms_into_context() -> None:
    captured: dict[str, int] = {}
    now = [1.0]

    class RecordingSampler(FakeSampler):
        def sample(self, ctx: DeviceContext) -> list[SampleRecord]:
            captured["elapsed_ms"] = ctx.elapsed_ms()
            return super().sample(ctx)

    recording = RecordingSampler(
        name="stamped",
        interval_ms=100,
        sample_records=(_record(),),
    )
    scheduler = SamplingScheduler([recording], context=_context(), clock=lambda: now[0])
    scheduler.probe()
    scheduler.start()
    now[0] = 1.25
    scheduler.poll_once()
    scheduler.stop()

    assert captured["elapsed_ms"] == 250


def test_scheduler_stop_flushes_stop_records() -> None:
    sampler = FakeSampler(name="flush", interval_ms=100, stop_records=(_record("flush.metric", 9.0),))
    collected: list[SampleRecord] = []
    scheduler = SamplingScheduler(
        [sampler],
        context=_context(),
        on_record=collected.append,
        clock=lambda: 0.0,
    )
    scheduler.probe()

    scheduler.stop()

    assert sampler.stopped is True
    assert collected == [SampleRecord(metric_id="flush.metric", value=9.0, ts_ms=0, tags={"unit": "%"})]


def test_scheduler_stop_isolates_stop_errors() -> None:
    good = FakeSampler(name="good", interval_ms=100)
    bad = FakeSampler(name="broken_stop", interval_ms=100, stop_error=RuntimeError("boom"))
    scheduler = SamplingScheduler([good, bad], context=_context(), clock=lambda: 0.0)
    scheduler.probe()

    scheduler.stop()

    assert "sampler.broken_stop.failed:boom" in scheduler.diagnostics


def test_scheduler_background_thread_samples_until_stopped() -> None:
    sampler = FakeSampler(name="threaded", interval_ms=5, sample_records=(_record("t.metric", 1.0),))
    collected: list[SampleRecord] = []
    scheduler = SamplingScheduler(
        [sampler],
        context=_context(),
        interval_ms=5,
        on_record=collected.append,
        clock=time.monotonic,
    )
    scheduler.probe()
    scheduler.start()
    time.sleep(0.06)
    scheduler.stop()

    assert sampler.sample_count >= 2
    assert len(collected) >= 2
    assert scheduler._thread is None  # noqa: SLF001 - 确认线程已 join


# --- TASK-005：samples.ndjson 落盘契约与 counter 事件转换 ---

def test_samples_writer_appends_contract_lines(tmp_path: Path) -> None:
    path = tmp_path / "artifacts" / "android" / "samples.ndjson"

    with SamplesWriter(path) as writer:
        writer.append(
            [
                SampleRecord(metric_id="cpu.proc.pct", value=38.2, ts_ms=1000, tags={"unit": "%", "collector": "sampler.cpu"}),
                SampleRecord(metric_id="cpu.proc.pct", value=41.0, ts_ms=2000, tags={"unit": "%", "collector": "sampler.cpu"}),
            ]
        )
        writer.append([SampleRecord(metric_id="cpu.proc.pct", value=39.5, ts_ms=3000, tags={"unit": "%"})])

    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert lines == [
        {"tsMs": 1000, "metricId": "cpu.proc.pct", "value": 38.2, "tags": {"unit": "%", "collector": "sampler.cpu"}},
        {"tsMs": 2000, "metricId": "cpu.proc.pct", "value": 41.0, "tags": {"unit": "%", "collector": "sampler.cpu"}},
        {"tsMs": 3000, "metricId": "cpu.proc.pct", "value": 39.5, "tags": {"unit": "%"}},
    ]


def test_read_samples_skips_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "samples.ndjson"
    path.write_text(
        "\n".join(
            [
                json.dumps({"tsMs": 1000, "metricId": "cpu.proc.pct", "value": 10.0, "tags": {}}),
                "not-json-line",
                json.dumps({"tsMs": "bad", "metricId": "cpu.proc.pct", "value": True}),
                json.dumps({"metricId": "", "value": 1}),
                json.dumps({"tsMs": 2000, "metricId": "mem.metric", "value": 3.5}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    records = read_samples(path)

    assert [(record.metric_id, record.value, record.ts_ms) for record in records] == [
        ("cpu.proc.pct", 10.0, 1000),
        ("mem.metric", 3.5, 2000),
    ]


def test_parse_samples_events_produces_counter_events_per_design() -> None:
    records = [
        SampleRecord(
            metric_id="cpu.proc.pct",
            value=38.2,
            ts_ms=12345,
            tags={"unit": "%", "collector": "sampler.cpu", "extra": "keep-me"},
        ),
        SampleRecord(
            metric_id="memory.pss.peak_mb",
            value=321.5,
            ts_ms=4000,
            tags={"unit": "MB", "collector": "sampler.mem", "semantic": "final_snapshot"},
        ),
    ]

    events = parse_samples_events(records, run_id="run-a", start_seq=7)

    assert len(events) == 2
    first = events[0]
    assert first["schemaVersion"] == "perfeval.timeline.v1"
    assert first["seq"] == 7
    assert first["runId"] == "run-a"
    assert first["eventId"] == "run-a-sample-000007"
    assert first["type"] == "counter"
    assert first["name"] == "cpu.proc.pct"
    assert first["tsMonotonicNs"] == 12_345_000_000
    assert first["source"] == {"platform": "android", "collector": "sampler.cpu", "evidenceLevel": "L1"}
    assert first["data"] == {"value": 38.2, "unit": "%", "direction": "none"}
    assert first["tags"] == {"extra": "keep-me"}

    second = events[1]
    assert second["data"] == {"value": 321.5, "unit": "MB", "direction": "none", "semantic": "final_snapshot"}
    assert "tags" not in second

    for event in events:
        TimelineEvent.from_dict(event)


def test_parse_samples_events_from_raw_mapping_rows() -> None:
    events = parse_samples_events(
        [
            {"tsMs": 1000, "metricId": "cpu.proc.pct", "value": 12.5, "tags": {"unit": "%"}},
            {"bad": "row"},
        ],
        run_id="run-b",
    )

    assert len(events) == 1
    assert events[0]["eventId"] == "run-b-sample-000001"
    assert events[0]["data"]["value"] == 12.5


def test_parse_android_artifacts_recomputes_from_samples_ndjson(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts" / "android"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "gfxinfo.txt").write_text(
        "Total frames rendered: 100\nJanky frames: 12 (12.00%)\n95th percentile: 24ms\n",
        encoding="utf-8",
    )
    with SamplesWriter(artifact_dir / "samples.ndjson") as writer:
        writer.append(
            [
                SampleRecord(metric_id="cpu.proc.pct", value=30.0 + index, ts_ms=(index + 1) * 1000, tags={"unit": "%", "collector": "sampler.mem"})
                for index in range(40)
            ]
        )

    events = parse_android_artifacts(artifact_dir, run_id="run-offline")
    by_type = [event for event in events if event["type"] == "counter"]

    assert len(by_type) == 40
    assert by_type[0]["name"] == "cpu.proc.pct"
    assert by_type[0]["tsMonotonicNs"] == 1_000_000_000
    assert by_type[-1]["seq"] == len(events)
    for event in events:
        TimelineEvent.from_dict(event)


# --- TASK-005：采样器注册表 ---

class DummySampler:
    name = "dummy"

    def __init__(self, *, interval_ms: int = 1000) -> None:
        self.interval_ms = interval_ms
        self.metric_ids = ("dummy.metric",)

    def probe(self, ctx: DeviceContext) -> ProbeResult:
        return ProbeResult.ok()

    def start(self, ctx: DeviceContext) -> None:
        return None

    def sample(self, ctx: DeviceContext) -> list[SampleRecord]:
        return []

    def stop(self) -> list[SampleRecord]:
        return []


def test_build_samplers_resolves_enabled_and_interval() -> None:
    factory = lambda *, interval_ms=1000: DummySampler(interval_ms=interval_ms)  # noqa: E731
    SAMPLER_FACTORIES["dummy"] = factory
    SAMPLER_FACTORIES["dummy_auto"] = factory
    SAMPLER_FACTORIES["dummy_off"] = factory
    SAMPLER_FACTORIES["dummy_zero"] = factory
    try:
        samplers = build_samplers(
            {
                "dummy": {"enabled": True, "intervalMs": 500},
                "dummy_auto": {"enabled": "auto"},  # auto 交由 probe() 判定，间隔用采样器自身默认
                "dummy_zero": {"intervalMs": 0},  # 非法间隔回退采样器自身默认
                "dummy_off": {"enabled": False, "intervalMs": 250},  # 显式关闭
                "unknown_sampler": {"enabled": True},  # 未注册 → 忽略
            }
        )
    finally:
        for name in ("dummy", "dummy_auto", "dummy_off", "dummy_zero"):
            SAMPLER_FACTORIES.pop(name)

    assert [sampler.interval_ms for sampler in samplers] == [500, 1000, 1000]
    assert all(sampler.name == "dummy" for sampler in samplers)


def test_samplers_from_config_short_circuits_on_sampling_off() -> None:
    SAMPLER_FACTORIES["dummy"] = lambda *, interval_ms=1000: DummySampler(interval_ms=interval_ms)
    try:
        assert samplers_from_config({"dummy": True}, sampling_off=True) == []
        assert len(samplers_from_config({"dummy": True}, sampling_off=False)) == 1
    finally:
        SAMPLER_FACTORIES.pop("dummy")


# --- TASK-007：meminfo 周期采样与 peak 语义修正 ---

GOLDEN_ROOT = Path("perfeval/fixtures/android/golden")


def _meminfo_runner(outputs: dict[str, tuple[int, str]]):
    def fake_runner(command, timeout):
        joined = " ".join(command)
        if "dumpsys meminfo" in joined:
            returncode, stdout = outputs.get("meminfo", (1, ""))
            return subprocess.CompletedProcess(command, returncode, stdout, "")
        return subprocess.CompletedProcess(command, 0, "", "")

    return fake_runner


def test_mem_sampler_registered_and_resolves_interval() -> None:
    samplers = build_samplers({"mem": {"enabled": "auto", "intervalMs": 2000}})

    assert len(samplers) == 1
    assert samplers[0].name == "mem"
    assert samplers[0].interval_ms == 2000
    assert samplers[0].metric_ids == ("memory.pss.peak_mb",)

    # 未指定 intervalMs 时使用 meminfo 采样自身默认 2s（设计 §5），不被全局默认覆盖
    default_samplers = build_samplers({"mem": True})
    assert [sampler.interval_ms for sampler in default_samplers] == [2000]


def test_mem_sampler_records_carry_window_and_utc_through_parse_chain() -> None:
    stdout = (GOLDEN_ROOT / "meminfo-light.txt").read_text(encoding="utf-8")
    sampler = MemSampler(utc_now=lambda: "2026-09-08T01:02:03.456Z")
    ctx = DeviceContext(
        adb=AdbClient("/adb"),
        command_runner=_meminfo_runner({"meminfo": (0, stdout)}),
        serial="emulator-5554",
        package_name="com.perfeval.fixture",
        elapsed_ms=lambda: 2000,
    )

    records = sampler.sample(ctx)
    events = parse_samples_events(records, run_id="run-window")

    assert events[0]["data"]["samplingWindowMs"] == 2000
    assert events[0]["tsUtc"] == "2026-09-08T01:02:03.456Z"
    assert "samplingWindowMs" not in events[0].get("tags", {})
    assert "tsUtc" not in events[0].get("tags", {})
    TimelineEvent.from_dict(events[0])

    # 真实链路进入 summary 引擎：mem counter 序列被识别为 stream，窗口随事件透传
    stream_events = [
        {**event, "seq": index + 1, "eventId": f"run-window-sample-{index + 1:06d}"}
        for index, event in enumerate(events * 40)
    ]
    summary = aggregate_summary("run-window", timeline_events=stream_events)
    metric = summary["metrics"]["memory.pss.peak_mb"]
    assert metric["isStream"] is True
    assert metric["sampleCount"] == 40


def test_parse_chain_low_coverage_downgrades_confidence() -> None:
    # 采样器卡死（ts 不再前进）时 coverage≈0：经真实 parse 链进入引擎后，多 run 合并置信度降档
    records = [
        SampleRecord(
            metric_id="memory.pss.peak_mb",
            value=300.0 + (index % 4),
            ts_ms=0,
            tags={"unit": "MB", "collector": "sampler.mem", "samplingWindowMs": 2000},
        )
        for index in range(200)
    ]
    events = parse_samples_events(records, run_id="run-merged")
    # CLI --repeat 合并时各 repeat 事件携带自身 runId，此处等价模拟
    for index, event in enumerate(events):
        event["runId"] = f"run-{index // 40}"

    summary = aggregate_summary("run-merged", timeline_events=events)

    metric = summary["metrics"]["memory.pss.peak_mb"]
    assert metric["isStream"] is True
    assert metric["confidence"] == "medium"  # repeatCount=5 基数 0.9，coverage<0.6 降一档


def test_scheduler_stop_with_join_timeout_isolates_concurrent_sample() -> None:
    block = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    state = {"samples_started": 0, "stop_calls": 0}
    collected: list[SampleRecord] = []

    class BlockingSampler:
        name = "blocking"
        metric_ids = ("blocking.metric",)
        interval_ms = 1

        def probe(self, ctx: DeviceContext) -> ProbeResult:
            return ProbeResult.ok()

        def start(self, ctx: DeviceContext) -> None:
            return None

        def sample(self, ctx: DeviceContext) -> list[SampleRecord]:
            state["samples_started"] += 1
            block.set()
            release.wait(timeout=5)
            finished.set()
            return []

        def stop(self) -> list[SampleRecord]:
            # 与仍在执行的 sample() 并发调用，必须无锁安全
            state["stop_calls"] += 1
            return [SampleRecord(metric_id="blocking.metric", value=1.0)]

    sampler = BlockingSampler()
    scheduler = SamplingScheduler(
        [sampler],
        context=_context(),
        interval_ms=1,
        on_record=collected.append,
        clock=time.monotonic,
        join_timeout_s=0.05,
    )
    scheduler.probe()
    scheduler.start()
    assert block.wait(timeout=5)  # 线程已进入 sample() 并阻塞

    scheduler.stop()  # join 0.05s 超时，与阻塞中的 sample() 并发刷入 stop 读数后返回
    release.set()
    assert finished.wait(timeout=5)

    assert state["stop_calls"] == 1
    assert state["samples_started"] == 1  # stop 后不再产生新一轮采样
    assert collected == [SampleRecord(metric_id="blocking.metric", value=1.0, ts_ms=0, tags={})]


def test_mem_sampler_samples_pss_total(tmp_path: Path) -> None:
    stdout = (GOLDEN_ROOT / "meminfo-light.txt").read_text(encoding="utf-8")
    sampler = MemSampler(interval_ms=2000, utc_now=lambda: "2026-09-08T00:00:00.000Z")
    ctx = DeviceContext(
        adb=AdbClient("/adb"),
        command_runner=_meminfo_runner({"meminfo": (0, stdout)}),
        serial="emulator-5554",
        package_name="com.perfeval.fixture",
        elapsed_ms=lambda: 6000,
    )

    records = sampler.sample(ctx)

    assert records == [
        SampleRecord(
            metric_id="memory.pss.peak_mb",
            value=89.0,
            ts_ms=6000,
            tags={
                "unit": "MB",
                "collector": "sampler.mem",
                "samplingWindowMs": 2000,
                "tsUtc": "2026-09-08T00:00:00.000Z",
            },
        )
    ]


def test_mem_sampler_probe_checks_capability() -> None:
    stdout = (GOLDEN_ROOT / "meminfo-light.txt").read_text(encoding="utf-8")
    ok_ctx = DeviceContext(
        adb=AdbClient("/adb"),
        command_runner=_meminfo_runner({"meminfo": (0, stdout)}),
        package_name="com.perfeval.fixture",
    )
    failing_ctx = DeviceContext(
        adb=AdbClient("/adb"),
        command_runner=_meminfo_runner({"meminfo": (1, "")}),
        package_name="com.perfeval.fixture",
    )
    no_package_ctx = DeviceContext(
        adb=AdbClient("/adb"),
        command_runner=_meminfo_runner({"meminfo": (0, stdout)}),
    )
    sampler = MemSampler()

    assert sampler.probe(ok_ctx) == ProbeResult.ok()
    # smoke-fix2：命令失败给出含返回码的 reason（原 "dumpsys meminfo unavailable"）
    assert sampler.probe(failing_ctx) == ProbeResult.unavailable("dumpsys meminfo failed rc=1")
    assert sampler.probe(no_package_ctx) == ProbeResult.unavailable("package_name missing")
    assert sampler.sample(failing_ctx) == []


# --- smoke-fix2：probe 语义 = meminfo 通道可用性，而非目标进程当前存活 ---
# runner 生命周期 probe 先于 launch（force_stop 已杀目标进程），输出 "No process
# found"（rc=0）属预期——launch 后进程必然存在；仅 adb 命令失败才 unavailable。


def test_mem_sampler_probe_available_when_process_absent() -> None:
    """probe 时进程不存在（No process found，rc=0）→ available。"""
    no_process_ctx = DeviceContext(
        adb=AdbClient("/adb"),
        command_runner=_meminfo_runner(
            {"meminfo": (0, "No process found for: com.perfeval.fixture\n")}
        ),
        package_name="com.perfeval.fixture",
    )

    assert MemSampler().probe(no_process_ctx) == ProbeResult.ok()


def test_mem_sampler_probe_available_on_empty_process_section() -> None:
    """空 process 段（rc=0，无 PSS 数值）→ 同样 available（通道正常）。"""
    empty_ctx = DeviceContext(
        adb=AdbClient("/adb"),
        command_runner=_meminfo_runner({"meminfo": (0, "")}),
        package_name="com.perfeval.fixture",
    )

    assert MemSampler().probe(empty_ctx) == ProbeResult.ok()


def test_mem_sampler_probe_unavailable_on_command_failure_with_detail() -> None:
    """命令失败（rc!=0，带 stderr）→ unavailable 且 reason 含返回码与 stderr 摘要。"""
    failing_ctx = DeviceContext(
        adb=AdbClient("/adb"),
        command_runner=_meminfo_runner({"meminfo": (7, "java.lang.SecurityException\n")}),
        package_name="com.perfeval.fixture",
    )
    raising_ctx = DeviceContext(
        adb=AdbClient("/adb"),
        command_runner=_raising_meminfo_runner,
        package_name="com.perfeval.fixture",
    )

    assert MemSampler().probe(failing_ctx) == ProbeResult.unavailable(
        "dumpsys meminfo failed rc=7: java.lang.SecurityException"
    )
    assert MemSampler().probe(raising_ctx) == ProbeResult.unavailable(
        "dumpsys meminfo command failed: device offline"
    )


def _raising_meminfo_runner(command, timeout):
    joined = " ".join(command)
    if "dumpsys meminfo" in joined:
        raise RuntimeError("device offline")
    return subprocess.CompletedProcess(command, 0, "", "")


# --- TASK-014：CPU 采样器（进程 + 整机 + 每核可选，METRIC_SOURCES §2） ---

CPU_PID = 4321
UTC_STUB = "2026-09-08T00:00:00.000Z"


def _stat_text(cycles: int) -> str:
    """在 golden proc_stat.txt（第 0 周期，真机 8 核格式脱敏）上叠加 cycles 个窗口的 jiffies。

    guest/guest_nice（第 9/10 列）为正值合成样本（在网真机 RFCX701D8KR 实测 guest=0，
    见 2026-11-10 体检），按 proc(5) 语义取 guest ⊆ user 的量级，且跨窗口恒定（Δguest=0）——
    busy% 手算值不受其影响；Δguest>0 的双写修正演示见专项用例
    （test_cpu_busy_pct_excludes_guest_double_count / test_cpu_sampler_guest_delta_skipped_in_total_pct）。

    每窗口增量（手算口径）：cpu0 +150user/+50idle→75.0；cpu1 +50user/+50sys/+300idle→25.0；
    cpu2 +300user/+50sys/+50idle→87.5；cpu3 +100user/+50sys/+50idle→75.0；cpu4 +100idle→0.0；
    cpu5 +50user/+50idle→50.0；cpu6 +75user/+25idle→75.0；cpu7 +25user/+75idle→25.0；
    聚合行与各核严格一致：Δtotal=1600（busy 口径，不含 guest 两列）、Δidle=700 → 整机 1-700/1600 = 56.25。
    """
    c = cycles
    return "\n".join(
        [
            f"cpu  {9430 + 750 * c} 140 {4620 + 150 * c} {100600 + 700 * c} 900 180 84 0 120 40",
            f"cpu0 {1400 + 150 * c} 25 625 {12550 + 50 * c} 125 25 12 0 12 2",
            f"cpu1 {1400 + 50 * c} 25 {675 + 50 * c} {12800 + 300 * c} 125 25 13 0 14 3",
            f"cpu2 {1500 + 300 * c} 25 {725 + 50 * c} {12550 + 50 * c} 125 25 12 0 15 3",
            f"cpu3 {1300 + 100 * c} 25 {675 + 50 * c} {12550 + 50 * c} 125 25 13 0 13 3",
            f"cpu4 900 10 450 {12400 + 100 * c} 100 20 8 0 9 2",
            f"cpu5 {1000 + 50 * c} 10 500 {12550 + 50 * c} 100 20 9 0 10 2",
            f"cpu6 {950 + 75 * c} 10 480 {12600 + 25 * c} 100 20 9 0 9 2",
            f"cpu7 {980 + 25 * c} 10 490 {12600 + 75 * c} 100 20 8 0 9 2",
            f"intr {8014345 + 2000 * c} 0 1280604 153253 0 0 0 429338 0 0",
            f"ctxt {12347678 + 2000 * c}",
            "btime 1717171717",
            f"processes {4251 + 9 * c}",
            "procs_running 2",
            "procs_blocked 0",
            "softirq 1234567 0 234567 11 34567 0 0 100 234567 0 0",
        ]
    ) + "\n"


def _pid_stat_text(cycles: int, pid: int = CPU_PID) -> str:
    """golden proc_pid_stat.txt（第 0 周期，comm 按 15 字符截断的真实格式）；每窗口 utime+50/stime+50。"""
    c = cycles
    return (
        f"{pid} (com.perfeval.fi) S 1621 1621 0 0 -1 4194624 752288 0 34157 0 {550 + 50 * c} {350 + 50 * c} "
        "0 0 20 0 136 0 1277 23345713152 112117 18446744073709551615 1 1 0 0 0 0 4612 1 "
        "1073775864 0 0 0 17 1 0 0 0 0 0 0 0 0 0 0 0 0\n"
    )


class _ScriptedCpuRunner:
    """按命令类型返回脚本化输出；/proc/stat 每成功读一次 cycle+1，模拟 jiffies 单调累加。

    批量 cat 的拼接 stdout 与真实设备一致：/proc/stat 全文 + 可选的 /proc/<pid>/stat 单行。
    """

    def __init__(
        self,
        *,
        pid: int = CPU_PID,
        pidof_fail: bool = False,
        clk_tck: str = "100",
        clk_fail: bool = False,
        fail_stat: bool = False,
        include_pid_stat: bool = True,
    ) -> None:
        self.pid = pid
        self.pidof_fail = pidof_fail
        self.clk_tck = clk_tck
        self.clk_fail = clk_fail
        self.fail_stat = fail_stat
        self.include_pid_stat = include_pid_stat
        self.custom_stat: str | None = None  # 注入自由构造的 /proc/stat 文本（怪数据/热插拔场景）
        self.cycle = 0
        self.commands: list[str] = []

    def __call__(self, command, timeout):
        joined = " ".join(command)
        self.commands.append(joined)
        if "getconf" in joined:
            if self.clk_fail:
                return subprocess.CompletedProcess(command, 1, "", "")
            return subprocess.CompletedProcess(command, 0, f"{self.clk_tck}\n", "")
        if "pidof" in joined:
            if self.pidof_fail:
                return subprocess.CompletedProcess(command, 1, "", "")
            return subprocess.CompletedProcess(command, 0, f"{self.pid}\n", "")
        if "/proc/stat" in joined:
            if self.fail_stat:
                return subprocess.CompletedProcess(command, 1, "", "")
            stdout = self.custom_stat if self.custom_stat is not None else _stat_text(self.cycle)
            if self.include_pid_stat and f"/proc/{self.pid}/stat" in joined:
                stdout += _pid_stat_text(self.cycle, self.pid)
            self.cycle += 1
            return subprocess.CompletedProcess(command, 0, stdout, "")
        return subprocess.CompletedProcess(command, 0, "", "")


def _cpu_ctx(
    runner,
    elapsed_ms: int = 0,
    package_name: str | None = "com.perfeval.fixture",
) -> DeviceContext:
    return DeviceContext(
        adb=AdbClient("/adb"),
        command_runner=runner,
        serial="emulator-5554",
        package_name=package_name,
        elapsed_ms=lambda: elapsed_ms,
    )


def test_cpu_sampler_registered_and_resolves_interval() -> None:
    samplers = build_samplers({"cpu": {"enabled": "auto", "intervalMs": 500}})

    assert len(samplers) == 1
    assert samplers[0].name == "cpu"
    assert samplers[0].interval_ms == 500
    assert samplers[0].metric_ids == ("cpu.proc.pct", "cpu.total.pct")

    # 未指定 intervalMs 时使用 CPU 采样自身默认 1s（ROADMAP CM-TASK-014 / 设计 §6），不被全局默认覆盖
    default_samplers = build_samplers({"cpu": True})
    assert [sampler.interval_ms for sampler in default_samplers] == [1000]


def test_golden_stat_files_match_parametric_cycle_and_parse() -> None:
    # golden 即参数化生成器的第 0 周期：真实 /proc/stat（cpu 聚合行 + cpu0..N）与 /proc/<pid>/stat 格式
    assert (GOLDEN_ROOT / "proc_stat.txt").read_text(encoding="utf-8") == _stat_text(0)
    assert (GOLDEN_ROOT / "proc_pid_stat.txt").read_text(encoding="utf-8") == _pid_stat_text(0)

    total, cores, proc = parse_stat_text(_stat_text(0) + _pid_stat_text(0), pid=CPU_PID)

    assert total is not None
    # busy 口径总数只含 user..steal（前 8 列）：golden guest/guest_nice 为正值合成样本，
    # 不得计入（cpu-guest-double-count 修复）
    assert total.fields[8] == 120 and total.fields[9] == 40
    assert all(times.fields[8] > 0 and times.fields[9] > 0 for times in cores.values())
    assert total.total == 9430 + 140 + 4620 + 100600 + 900 + 180 + 84
    assert set(cores) == set(range(8))
    assert proc == ProcTimes(utime=550, stime=350)


def test_cpu_busy_pct_excludes_guest_double_count() -> None:
    """guest>0 样本：Δtotal 不得计入 guest/guest_nice 两列（proc(5) 双写，guest 已并入 user/nice）。

    快照对按 golden 周期增量构造（Δuser=750/Δsys=150/Δidle=700 → busy 口径 Δtotal=1600、
    Δidle=700 → 56.25），另叠加 Δguest=150/Δguest_nice=50。修前口径对全列求和会把 guest
    列计入 Δtotal（1600+200=1800）→ busy% = 1-700/1800 = 61.11，被高估 4.86pt——修前修后
    差值可断言。
    """
    base = CpuTimes((9430, 140, 4620, 100600, 900, 180, 84, 0, 120, 40))
    current = CpuTimes((9430 + 750, 140, 4620 + 150, 100600 + 700, 900, 180, 84, 0, 120 + 150, 40 + 50))

    pct = _busy_pct(base, current)

    # 修正后：Δtotal 只含 user..steal（1600），guest 两列被剔除
    assert pct == (1.0 - 700 / 1600) * 100.0  # = 56.25
    # 修前（buggy）口径的算术结果，显式断言差值存在
    buggy_pct = (1.0 - 700 / (1600 + 150 + 50)) * 100.0
    assert buggy_pct > pct
    assert buggy_pct - pct > 4.0


def test_cpu_busy_pct_guest_zero_regression() -> None:
    """guest=0 样本（在网真机 SM_F9560 实测形态）：修正前后口径一致，busy% 不变。"""
    base = CpuTimes((9430, 140, 4620, 100600, 900, 180, 84, 0, 0, 0))
    current = CpuTimes((10180, 140, 4770, 101300, 900, 180, 84, 0, 0, 0))

    assert _busy_pct(base, current) == 56.25


def test_cpu_sampler_guest_delta_skipped_in_total_pct() -> None:
    """端到端：t1 快照 guest/guest_nice 再 +150/+50（Δguest>0），整机 busy% 仍为手算 56.25——
    双写列不进 Δtotal，修前口径（全列求和）会得 1-700/1800=61.11。"""
    runner = _ScriptedCpuRunner()
    sampler = CpuSampler(utc_now=lambda: UTC_STUB)
    sampler.start(_cpu_ctx(runner, elapsed_ms=0))  # 基线 = golden 第 0 周期（guest=120/40）

    lines = _stat_text(1).splitlines()
    # 聚合行 user/sys/idle 按 golden 周期增量（+750/+150/+700），guest 列在基线上再 +150/+50
    lines[0] = "cpu  10180 140 4770 101300 900 180 84 0 270 90"
    runner.custom_stat = "\n".join(lines) + "\n"

    records = sampler.sample(_cpu_ctx(runner, elapsed_ms=1000))

    total = next(record for record in records if record.metric_id == "cpu.total.pct" and "core" not in record.tags)
    assert total.value == 56.25
    # 进程维度不受 guest 列影响（/proc/<pid>/stat 无 guest 概念）
    proc = next(record for record in records if record.metric_id == "cpu.proc.pct")
    assert proc.value == 12.5


def test_cpu_sampler_probe_checks_proc_stat() -> None:
    sampler = CpuSampler()

    assert sampler.probe(_cpu_ctx(_ScriptedCpuRunner())) == ProbeResult.ok()
    assert sampler.probe(_cpu_ctx(_ScriptedCpuRunner(fail_stat=True))) == ProbeResult.unavailable(
        "/proc/stat unavailable"
    )

    def broken_runner(command, timeout):
        raise RuntimeError("adb offline")

    assert sampler.probe(_cpu_ctx(broken_runner)) == ProbeResult.unavailable("/proc/stat unavailable")


def test_cpu_sampler_two_sample_diff_matches_hand_computed_values() -> None:
    runner = _ScriptedCpuRunner()
    sampler = CpuSampler(utc_now=lambda: UTC_STUB)
    sampler.start(_cpu_ctx(runner, elapsed_ms=0))  # 首帧基线由 start() 记录（golden 第 0 周期）

    records = sampler.sample(_cpu_ctx(runner, elapsed_ms=1000))

    tags = {"unit": "%", "collector": "sampler.cpu", "samplingWindowMs": 1000, "tsUtc": UTC_STUB}
    # 进程：(Δ100 jiffies / CLK_TCK 100) / Δt 1s / 8 核 × 100 = 12.5；整机：1 - 700/1600 × 100 = 56.25
    assert records == [
        SampleRecord(metric_id="cpu.proc.pct", value=12.5, ts_ms=1000, tags=tags),
        SampleRecord(metric_id="cpu.total.pct", value=56.25, ts_ms=1000, tags=tags),
    ]


def test_cpu_sampler_per_core_switch_emits_core_series() -> None:
    off_runner = _ScriptedCpuRunner()
    off_sampler = CpuSampler(utc_now=lambda: UTC_STUB)
    off_sampler.start(_cpu_ctx(off_runner, 0))
    assert all("core" not in record.tags for record in off_sampler.sample(_cpu_ctx(off_runner, 1000)))

    on_runner = _ScriptedCpuRunner()
    on_sampler = CpuSampler(include_per_core=True, utc_now=lambda: UTC_STUB)
    on_sampler.start(_cpu_ctx(on_runner, 0))
    records = on_sampler.sample(_cpu_ctx(on_runner, 1000))

    core_values = [(record.tags["core"], record.value) for record in records if "core" in record.tags]
    assert core_values == [
        (0, 75.0),
        (1, 25.0),
        (2, 87.5),
        (3, 75.0),
        (4, 0.0),
        (5, 50.0),
        (6, 75.0),
        (7, 25.0),
    ]
    # tags.core 原样保留在事件 tags 中（不进 data），经真实 parse 链路透传
    events = parse_samples_events(records, run_id="run-cores")
    core_two = [event for event in events if event.get("tags", {}).get("core") == 2]
    assert core_two[0]["data"]["value"] == 87.5
    TimelineEvent.from_dict(core_two[0])


def test_cpu_sampler_pidof_failure_skips_proc_only_and_records_diagnostic() -> None:
    runner = _ScriptedCpuRunner(pidof_fail=True)
    sampler = CpuSampler(utc_now=lambda: UTC_STUB)
    sampler.start(_cpu_ctx(runner, elapsed_ms=0))

    first = sampler.sample(_cpu_ctx(runner, elapsed_ms=1000))
    second = sampler.sample(_cpu_ctx(runner, elapsed_ms=2000))
    # 前两次失败：仅跳过该次进程采样并计 skipped，整机照常出数，无 diagnostic
    assert [[record.metric_id for record in records] for records in (first, second)] == [
        ["cpu.total.pct"],
        ["cpu.total.pct"],
    ]
    assert [record.value for record in first + second] == [56.25, 56.25]
    assert sampler.skipped_proc_samples == 2
    assert sampler.diagnostics == []

    third = sampler.sample(_cpu_ctx(runner, elapsed_ms=3000))
    # 连续第 3 次失败记 diagnostic（METRIC_SOURCES §2 降级口径），整机仍不受影响
    assert [record.metric_id for record in third] == ["cpu.total.pct"]
    assert sampler.skipped_proc_samples == 3
    assert sampler.diagnostics == [
        "sampler.cpu.proc.unavailable:pid/process stat failed for 3 consecutive samples"
    ]

    # pidof 恢复后：首个样本仅有整机（上一帧无进程读数可差分，仅重建基线、不计新失败），次一样本进程恢复
    runner.pidof_fail = False
    rebuild = sampler.sample(_cpu_ctx(runner, elapsed_ms=4000))
    assert [record.metric_id for record in rebuild] == ["cpu.total.pct"]
    assert sampler.skipped_proc_samples == 3
    assert len(sampler.diagnostics) == 1

    recovered = sampler.sample(_cpu_ctx(runner, elapsed_ms=5000))
    assert [record.metric_id for record in recovered] == ["cpu.proc.pct", "cpu.total.pct"]
    assert sampler.skipped_proc_samples == 3


def test_cpu_sampler_clk_tck_probe_and_fallback() -> None:
    probed_runner = _ScriptedCpuRunner(clk_tck="200")
    probed_sampler = CpuSampler(utc_now=lambda: UTC_STUB)
    probed_sampler.start(_cpu_ctx(probed_runner, 0))
    assert probed_sampler.clk_tck == 200
    records = probed_sampler.sample(_cpu_ctx(probed_runner, 1000))
    proc = next(record for record in records if record.metric_id == "cpu.proc.pct")
    # (Δ100 jiffies / CLK_TCK 200) / Δt 1s / 8 核 × 100 = 6.25
    assert proc.value == 6.25

    fallback_runner = _ScriptedCpuRunner(clk_fail=True)
    fallback_sampler = CpuSampler(utc_now=lambda: UTC_STUB)
    fallback_sampler.start(_cpu_ctx(fallback_runner, 0))
    assert fallback_sampler.clk_tck == 100  # getconf 失败回退默认 CLK_TCK


def test_cpu_sampler_read_failure_keeps_baseline() -> None:
    runner = _ScriptedCpuRunner()
    sampler = CpuSampler(utc_now=lambda: UTC_STUB)
    sampler.start(_cpu_ctx(runner, elapsed_ms=0))

    runner.fail_stat = True
    assert sampler.sample(_cpu_ctx(runner, elapsed_ms=1000)) == []

    runner.fail_stat = False
    records = sampler.sample(_cpu_ctx(runner, elapsed_ms=2000))

    # 基线未被失败样本覆盖：跨 2s 窗口差分同一周期增量 → 整机比例不变，进程占空比减半
    assert [(record.metric_id, record.value) for record in records] == [
        ("cpu.proc.pct", 6.25),
        ("cpu.total.pct", 56.25),
    ]


def test_cpu_sampler_without_package_emits_total_only() -> None:
    runner = _ScriptedCpuRunner()
    sampler = CpuSampler(utc_now=lambda: UTC_STUB)
    sampler.start(_cpu_ctx(runner, 0, package_name=None))

    records = sampler.sample(_cpu_ctx(runner, 1000, package_name=None))

    assert [record.metric_id for record in records] == ["cpu.total.pct"]
    assert all("pidof" not in command for command in runner.commands)


def test_cpu_sampler_hotplug_core_changes() -> None:
    # CPU 热插拔：cpu4..cpu7 下线（t1 无行；内核聚合行保留下线核历史累计，整机差分不受影响）、
    # cpu8 新上线（t0 无基线）；当前快照核数 = cpu0..3 + cpu8 = 5
    runner = _ScriptedCpuRunner()
    sampler = CpuSampler(include_per_core=True, utc_now=lambda: UTC_STUB)
    sampler.start(_cpu_ctx(runner, elapsed_ms=0))  # 基线为 golden 第 0 周期（cpu0..7 共 8 核）

    lines = _stat_text(1).splitlines()
    online = [line for line in lines if not line.startswith(("cpu4", "cpu5", "cpu6", "cpu7"))]
    online.insert(5, "cpu8 30 0 10 60 0 0 0 0 0 0")  # 聚合行 + cpu0..3 之后
    runner.custom_stat = "\n".join(online) + "\n"

    records = sampler.sample(_cpu_ctx(runner, elapsed_ms=1000))

    core_values = [(record.tags["core"], record.value) for record in records if "core" in record.tags]
    # 下线核不迭代、新上线核无基线被跳过：仅 cpu0..3 出数（手算值同单核口径）
    assert core_values == [(0, 75.0), (1, 25.0), (2, 87.5), (3, 75.0)]
    # 进程归一化用当前快照核数（5 核）：(Δ100 jiffies / 100) / 1s / 5 × 100 = 20.0
    proc = next(record for record in records if record.metric_id == "cpu.proc.pct")
    assert proc.value == 20.0
    # 整机聚合行不受核集变化影响（下线核累计保留在聚合行内）：1 - 700/1600 = 56.25
    total = next(record for record in records if record.metric_id == "cpu.total.pct" and "core" not in record.tags)
    assert total.value == 56.25


def test_cpu_sampler_out_of_range_pct_dropped_with_diagnostic() -> None:
    # 极端场景：计数器异常回退使 Δidle > Δtotal（数学上合法数据不会出现），超界点必须丢弃并
    # 记 diagnostic，不做掩盖性 clamp
    runner = _ScriptedCpuRunner()
    sampler = CpuSampler(utc_now=lambda: UTC_STUB)
    sampler.start(_cpu_ctx(runner, elapsed_ms=0))

    lines = _stat_text(1).splitlines()
    # 聚合行 user 回退 -500、idle 正常 +700 → Δtotal=200 < Δidle=700 → (1 - 3.5) × 100 = -250%
    lines[0] = "cpu  8930 140 4620 101300 900 180 84 0 0 0"
    runner.custom_stat = "\n".join(lines) + "\n"

    records = sampler.sample(_cpu_ctx(runner, elapsed_ms=1000))

    # 整机点被丢弃，进程点不受影响；基线仍随成功读取更新
    assert [record.metric_id for record in records] == ["cpu.proc.pct"]
    assert records[0].value == 12.5
    assert sampler.diagnostics == ["sampler.cpu.out_of_range:total dropped -250.0 (outside [0, 100])"]

    # 每核同样守卫且 diagnostic 带 core 标识，其余核不受影响
    core_runner = _ScriptedCpuRunner()
    core_sampler = CpuSampler(include_per_core=True, utc_now=lambda: UTC_STUB)
    core_sampler.start(_cpu_ctx(core_runner, elapsed_ms=0))
    core_lines = _stat_text(1).splitlines()
    # cpu0：user -75、system +50、idle +50 → Δtotal=25 < Δidle=50 → -100%
    core_lines[1] = "cpu0 1325 25 675 12600 125 25 12 0 0 0"
    core_runner.custom_stat = "\n".join(core_lines) + "\n"

    core_records = core_sampler.sample(_cpu_ctx(core_runner, elapsed_ms=1000))
    core_values = [(record.tags["core"], record.value) for record in core_records if "core" in record.tags]
    assert all(core != 0 for core, _ in core_values)
    assert (1, 25.0) in core_values
    assert core_sampler.diagnostics == ["sampler.cpu.out_of_range:core0 dropped -100.0 (outside [0, 100])"]
    # 整机与进程（聚合行正常）照常出数
    assert [record.metric_id for record in core_records if "core" not in record.tags] == [
        "cpu.proc.pct",
        "cpu.total.pct",
    ]


def test_cpu_sampler_pid_change_rebaselines_without_output() -> None:
    # 进程重启（pidof 解析到新 pid）：新进程 jiffies 从零计数、小于旧基线 → 视同 re-baseline，
    # 该采样点不产数；下一窗口起按新基线正常差分，且不计 proc miss
    runner = _ScriptedCpuRunner()
    sampler = CpuSampler(utc_now=lambda: UTC_STUB)
    sampler.start(_cpu_ctx(runner, elapsed_ms=0))
    assert [record.metric_id for record in sampler.sample(_cpu_ctx(runner, elapsed_ms=1000))] == [
        "cpu.proc.pct",
        "cpu.total.pct",
    ]

    runner.pid = 8888  # 进程重启：pid 变化（fake 同步输出 pid=8888 的 /proc/<pid>/stat 行）
    assert sampler.sample(_cpu_ctx(runner, elapsed_ms=2000)) == []

    recovered = sampler.sample(_cpu_ctx(runner, elapsed_ms=3000))
    assert [record.metric_id for record in recovered] == ["cpu.proc.pct", "cpu.total.pct"]
    assert recovered[0].value == 12.5  # 新基线（8888 第 1 周期）→ 第 2 周期差分恢复正常
    assert sampler.skipped_proc_samples == 0  # pid 重启走 re-baseline，不算 proc miss
    assert sampler.diagnostics == []


def test_cpu_sampler_records_flow_through_parse_chain_to_summary() -> None:
    runner = _ScriptedCpuRunner()
    sampler = CpuSampler(interval_ms=1000, utc_now=lambda: UTC_STUB)
    sampler.start(_cpu_ctx(runner, elapsed_ms=0))
    records: list[SampleRecord] = []
    for index in range(1, 41):
        records.extend(sampler.sample(_cpu_ctx(runner, elapsed_ms=index * 1000)))

    events = parse_samples_events(records, run_id="run-cpu")
    assert len(events) == 80
    first = events[0]
    assert first["type"] == "counter"
    assert first["name"] == "cpu.proc.pct"
    assert first["source"] == {"platform": "android", "collector": "sampler.cpu", "evidenceLevel": "L1"}
    assert first["data"]["unit"] == "%"
    assert first["data"]["samplingWindowMs"] == 1000
    assert first["tsUtc"] == UTC_STUB
    for event in events:
        TimelineEvent.from_dict(event)

    # 真实链路进入 summary 引擎：CPU counter 序列被识别为 stream
    summary = aggregate_summary("run-cpu", timeline_events=events)
    assert summary["metrics"]["cpu.total.pct"]["isStream"] is True
    assert summary["metrics"]["cpu.total.pct"]["sampleCount"] == 40
    assert summary["metrics"]["cpu.proc.pct"]["isStream"] is True
# --- TASK-016/017/018：net / power / gpu_thermal 周期采样器 ---

_UTC = "2026-09-08T01:02:03.456Z"

# 真实设备降级素材（只读采集）：真机/模拟器 /proc、/sys 节点对非 root adb 均限权，
# qtaguid 在新内核已移除——正好覆盖各降级分支。


def _golden(name: str) -> str:
    return (GOLDEN_ROOT / name).read_text(encoding="utf-8")


def _scripted_runner(script: dict[str, list[tuple[int, str]]]):
    """按命令子串路由的 fake command_runner：队列逐条弹出，错配/超量调用直接报错。"""
    queues = {key: list(items) for key, items in script.items()}

    def fake_runner(command, timeout):
        joined = " ".join(command)
        for key, queue in queues.items():
            if key in joined:
                assert queue, f"unexpected extra call matching {key!r}: {joined}"
                returncode, stdout = queue.pop(0)
                return subprocess.CompletedProcess(command, returncode, stdout, "")
        raise AssertionError(f"unexpected command: {joined}")

    return fake_runner


def _sampling_ctx(script: dict[str, list[tuple[int, str]]], ts: list[int]) -> DeviceContext:
    return DeviceContext(
        adb=AdbClient("/adb"),
        command_runner=_scripted_runner(script),
        serial="TEST-SERIAL-001",
        package_name="com.perfeval.fixture",
        elapsed_ms=lambda: ts[0],
    )


def test_net_power_gpu_thermal_registered_and_resolves_interval() -> None:
    samplers = build_samplers(
        {
            "net": {"intervalMs": 1500},
            "power": True,
            "gpu_thermal": {"enabled": "auto"},
        }
    )

    assert [sampler.name for sampler in samplers] == ["net", "power", "gpu_thermal"]
    assert [sampler.interval_ms for sampler in samplers] == [1500, 1000, 1000]
    assert samplers[0].metric_ids == ("net.rx.kbps", "net.tx.kbps")
    assert samplers[1].metric_ids == ("power.current.ma", "power.level.pct", "power.energy.mah")
    # S1 gpu-freq/thermal-component 扩展：gpu_thermal 六指标（busy/freq/max/三分量）
    assert samplers[2].metric_ids == (
        "gpu.busy.pct",
        "gpu.freq.mhz",
        "thermal.max.c",
        "thermal.cpu.c",
        "thermal.gpu.c",
        "thermal.btemp.c",
    )

    # 未指定 intervalMs 时使用各采样器自身默认 1s（CM-TASK-016/017/018）
    defaults = build_samplers({"net": True, "power": True, "gpu_thermal": True})
    assert [sampler.interval_ms for sampler in defaults] == [1000, 1000, 1000]


def test_net_sampler_qtaguid_uid_differential_hand_computed() -> None:
    sampler = NetSampler(interval_ms=2000, utc_now=lambda: _UTC)
    ts = [1000]
    ctx = _sampling_ctx(
        {
            "pm list packages": [(0, _golden("pm_list_packages_uid.txt"))],
            "xt_qtaguid": [(0, _golden("xt_qtaguid_stats.txt")), (0, _golden("xt_qtaguid_stats_t2.txt"))],
        },
        ts,
    )

    sampler.start(ctx)  # uid=10012 基线 rx=200000 tx=100000（跨 iface/tag/cnt_set 求和）
    ts[0] = 3000
    records = sampler.sample(ctx)

    expected_tags = {
        "unit": "kbps",
        "collector": "sampler.net",
        "source": "qtaguid",
        "samplingWindowMs": 2000,
        "tsUtc": _UTC,
    }
    # Δrx=60000B、Δtx=30000B、Δt=2000ms：kbps = Δbytes × 8 / Δt_ms（METRIC_SOURCES §3 差分口径）
    assert records == [
        SampleRecord(metric_id="net.rx.kbps", value=240.0, ts_ms=3000, tags=expected_tags),
        SampleRecord(metric_id="net.tx.kbps", value=120.0, ts_ms=3000, tags=expected_tags),
    ]


def test_qtaguid_and_netstats_parsers_sum_golden_totals() -> None:
    per_uid = parse_qtaguid_stats(_golden("xt_qtaguid_stats.txt"))

    assert per_uid[10012] == (200000.0, 100000.0)
    assert per_uid[1000] == (999999.0, 999999.0)
    # Xt stats 全部 NetworkStatsHistory 桶 rb/tb 累计（真实 dumpsys netstats 截取）
    assert parse_netstats_totals(_golden("netstats_dumpsys.txt")) == (103751, 106659)


def test_net_sampler_netstats_fallback_differential() -> None:
    netstats_t1 = _golden("netstats_dumpsys.txt")
    # Xt stats 首个 ident 追加一个桶：rx+2900B / tx+1600B（模拟窗口内新增流量）
    netstats_t2 = netstats_t1.replace(
        "      st=1788886800 rb=480 rp=5 tb=312 tp=3 op=0",
        "      st=1788886800 rb=480 rp=5 tb=312 tp=3 op=0\n"
        "      st=1788890400 rb=2900 rp=30 tb=1600 tp=20 op=0",
    )
    sampler = NetSampler(interval_ms=2000, utc_now=lambda: _UTC)
    ts = [1000]
    ctx = _sampling_ctx(
        {
            "pm list packages": [(0, _golden("pm_list_packages_uid.txt"))],
            "xt_qtaguid": [(2, "cat: /proc/net/xt_qtaguid/stats: No such file or directory")],
            "dumpsys netstats": [(0, netstats_t1), (0, netstats_t2)],
        },
        ts,
    )

    sampler.start(ctx)  # qtaguid 不可读 → Xt stats 总量基线 rx=103751 tx=106659
    ts[0] = 3000
    records = sampler.sample(ctx)

    # Δrx=2900B、Δtx=1600B、Δt=2000ms
    assert [(record.metric_id, record.value) for record in records] == [
        ("net.rx.kbps", 11.6),
        ("net.tx.kbps", 6.4),
    ]
    assert all(record.tags["source"] == "netstats" for record in records)


def test_net_sampler_source_switch_rebaselines_instead_of_mixed_diff() -> None:
    # qtaguid 瞬断回退 netstats：整机总量与单 uid 基线口径不同，直接差分必产虚高尖峰——
    # 视同 re-baseline（本轮不产数、记 diagnostic），源稳定后恢复差分（R1 修复）
    t2 = _golden("xt_qtaguid_stats_t2.txt")
    t3 = t2.replace("10012 0 168000 280 84000 210", "10012 0 192000 300 96000 225")
    sampler = NetSampler(interval_ms=2000, utc_now=lambda: _UTC)
    ts = [1000]
    ctx = _sampling_ctx(
        {
            "pm list packages": [(0, _golden("pm_list_packages_uid.txt"))],
            "xt_qtaguid": [
                (0, _golden("xt_qtaguid_stats.txt")),
                (2, "cat: /proc/net/xt_qtaguid/stats: No such file or directory"),
                (0, t2),
                (0, t3),
            ],
            "dumpsys netstats": [(0, _golden("netstats_dumpsys.txt"))],
        },
        ts,
    )

    sampler.start(ctx)  # qtaguid 基线 rx=200000 tx=100000
    ts[0] = 3000
    assert sampler.sample(ctx) == []  # qtaguid 瞬断回退 netstats：源切换，重建基线不产数
    assert sampler.drain_diagnostics() == ["source switched qtaguid -> netstats; re-baselined"]

    ts[0] = 5000
    assert sampler.sample(ctx) == []  # qtaguid 恢复：再次源切换，同样重建基线
    assert sampler.drain_diagnostics() == ["source switched netstats -> qtaguid; re-baselined"]

    ts[0] = 7000
    records = sampler.sample(ctx)  # 源稳定：Δrx=24000、Δtx=12000、Δt=2000ms
    assert [(record.metric_id, record.value) for record in records] == [
        ("net.rx.kbps", 96.0),
        ("net.tx.kbps", 48.0),
    ]
    assert all(record.tags["source"] == "qtaguid" for record in records)


def test_net_sampler_counter_reset_rebaselines_without_negative_rates() -> None:
    reset_snapshot = (
        "idx iface acct_tag_hex uid_tag_int cnt_set rx_bytes rx_packets tx_bytes tx_packets\n"
        "2 wlan0 0x0 10012 0 1000 2 500 1 1000 2 0 0 0 0 500 1 0 0 0 0\n"
    )
    sampler = NetSampler(interval_ms=2000, utc_now=lambda: _UTC)
    ts = [1000]
    ctx = _sampling_ctx(
        {
            "pm list packages": [(0, _golden("pm_list_packages_uid.txt"))],
            "xt_qtaguid": [
                (0, _golden("xt_qtaguid_stats.txt")),
                (0, reset_snapshot),
                (0, _golden("xt_qtaguid_stats_t2.txt")),
            ],
        },
        ts,
    )

    sampler.start(ctx)
    ts[0] = 3000
    assert sampler.sample(ctx) == []  # 计数器回绕：本跳点不产出
    assert sampler.drain_diagnostics() == ["byte counter reset detected; re-baselined"]

    ts[0] = 5000
    records = sampler.sample(ctx)  # 以重置后基线（1000/500）重新差分

    # Δrx=260000-1000、Δtx=130000-500、Δt=2000ms
    assert [(record.metric_id, record.value) for record in records] == [
        ("net.rx.kbps", 1036.0),
        ("net.tx.kbps", 518.0),
    ]


def test_net_sampler_probe_branches_report_precise_reasons() -> None:
    uid_ok = _golden("pm_list_packages_uid.txt")
    qtaguid = _golden("xt_qtaguid_stats.txt")
    netstats = _golden("netstats_dumpsys.txt")
    degraded_reason = "degraded: qtaguid unavailable, dumpsys netstats totals only"

    def probe(script: dict[str, list[tuple[int, str]]], **ctx_over: Any) -> ProbeResult:
        sampler = NetSampler()
        values: dict[str, Any] = {
            "adb": AdbClient("/adb"),
            "command_runner": _scripted_runner(script),
            "serial": "TEST-SERIAL-001",
            "package_name": "com.perfeval.fixture",
        }
        values.update(ctx_over)
        return sampler.probe(DeviceContext(**values))

    assert probe({"pm list packages": [(0, uid_ok)], "xt_qtaguid": [(0, qtaguid)]}) == ProbeResult.ok()
    assert probe(
        {
            "pm list packages": [(0, uid_ok)],
            "xt_qtaguid": [(2, "No such file or directory")],
            "dumpsys netstats": [(0, netstats)],
        }
    ) == ProbeResult(available=True, reason=degraded_reason)
    # uid 解析失败但 netstats 可用 → 降级可用（整机口径）
    assert probe({"pm list packages": [(0, "")], "dumpsys netstats": [(0, netstats)]}) == ProbeResult(
        available=True, reason=degraded_reason
    )
    # 双源均不可用 → unavailable，原因逐项精确
    assert probe(
        {
            "pm list packages": [(0, uid_ok)],
            "xt_qtaguid": [(2, "")],
            "dumpsys netstats": [(1, "")],
        }
    ) == ProbeResult.unavailable("xt_qtaguid stats unreadable; dumpsys netstats unavailable")
    assert probe({}, package_name=None) == ProbeResult.unavailable("package_name missing")


def test_net_sampler_records_flow_through_parse_chain_as_stream() -> None:
    records = [
        SampleRecord(
            metric_id="net.rx.kbps",
            value=100.0 + index,
            ts_ms=(index + 1) * 1000,
            tags={
                "unit": "kbps",
                "collector": "sampler.net",
                "source": "qtaguid",
                "samplingWindowMs": 1000,
                "tsUtc": _UTC,
            },
        )
        for index in range(40)
    ]

    events = parse_samples_events(records, run_id="run-net")
    first = events[0]

    assert first["type"] == "counter"
    assert first["name"] == "net.rx.kbps"
    assert first["tsMonotonicNs"] == 1_000_000_000
    assert first["tsUtc"] == _UTC
    assert first["source"]["collector"] == "sampler.net"
    assert first["data"]["samplingWindowMs"] == 1000
    assert first["data"]["unit"] == "kbps"
    assert first["tags"] == {"source": "qtaguid"}
    TimelineEvent.from_dict(first)

    # 真实链路进 summary 引擎：net counter 序列识别为 stream
    summary = aggregate_summary("run-net", timeline_events=events)
    metric = summary["metrics"]["net.rx.kbps"]
    assert metric["isStream"] is True
    assert metric["sampleCount"] == 40


def test_power_sampler_current_and_power_hand_computed() -> None:
    sampler = PowerSampler(interval_ms=1000, utc_now=lambda: _UTC)
    ctx = _sampling_ctx({"current_now": [(0, _golden("power_supply_battery.txt"))]}, [4000])

    records = sampler.sample(ctx)

    # current_now=-482500µA（放电为负，按 METRIC_SOURCES 取绝对值）→ 482.5mA；
    # voltage_now=3712000µV：powerMw = µA×µV/1e9 = 1791.04mW
    assert records == [
        SampleRecord(
            metric_id="power.current.ma",
            value=482.5,
            ts_ms=4000,
            tags={
                "unit": "mA",
                "collector": "sampler.power",
                "source": "current_now",
                # power-4 修复：原始带符号读数保留（放电为负，厂商符号约定不一，
                # 值层取绝对值不解读方向）
                "currentRawMa": -482.5,
                "powerMw": 1791.04,
                "samplingWindowMs": 1000,
                "tsUtc": _UTC,
            },
        )
    ]


def test_power_sampler_probe_branches_report_precise_reasons() -> None:
    good = _golden("power_supply_battery.txt")
    degraded = "current_now=\nvoltage_now=\ncapacity=76\ncharge_full=4368000\n"
    unavailable_reason = "power_supply battery nodes unreadable: /sys/class/power_supply/battery/*"

    def probe(script: dict[str, list[tuple[int, str]]]) -> ProbeResult:
        sampler = PowerSampler()
        ctx = DeviceContext(
            adb=AdbClient("/adb"),
            command_runner=_scripted_runner(script),
            serial="TEST-SERIAL-001",
            package_name="com.perfeval.fixture",
        )
        return sampler.probe(ctx)

    assert probe({"current_now": [(0, good)]}) == ProbeResult.ok()
    # current 缺失但 capacity 可用 → 降级可用（level 斜率口径）
    assert probe({"current_now": [(0, degraded)]}) == ProbeResult(
        available=True, reason="degraded: current_now unavailable, level slope only"
    )
    # 节点限权（如真机非 root adb）
    assert probe({"current_now": [(1, "cat: Permission denied")]}) == ProbeResult.unavailable(
        unavailable_reason
    )
    # 命令成功但全部节点为空
    assert probe(
        {"current_now": [(0, "current_now=\nvoltage_now=\ncapacity=\ncharge_full=\n")]}
    ) == ProbeResult.unavailable(unavailable_reason)


def test_power_sampler_level_slope_and_energy_estimate_when_current_missing() -> None:
    t1 = _golden("power_supply_battery.txt")
    t2 = "current_now=\nvoltage_now=\ncapacity=75\ncharge_full=4368000\n"
    sampler = PowerSampler(interval_ms=2000, utc_now=lambda: _UTC)
    ts = [1000]
    ctx = _sampling_ctx({"current_now": [(0, t1), (0, t2)]}, ts)

    sampler.start(ctx)  # capacity 基线 76%
    ts[0] = 3000
    records = sampler.sample(ctx)

    # Δlevel=1%、Δt=2000ms → 斜率 30 %/min；
    # 能耗估算 = charge_full(4368000µAh=4368mAh) × 1% = 43.68mAh（METRIC_SOURCES 估算口径）
    assert [(record.metric_id, record.value) for record in records] == [
        ("power.level.pct", 30.0),
        ("power.energy.mah", 43.68),
    ]

    events = parse_samples_events(records, run_id="run-power")
    assert events[0]["data"] == {"value": 30.0, "direction": "none", "unit": "%/min", "samplingWindowMs": 2000}
    assert events[0]["tags"] == {"source": "capacity_diff"}
    assert events[0]["tsUtc"] == _UTC
    assert events[1]["data"]["unit"] == "mAh"
    assert events[1]["data"]["semantic"] == "estimated"
    # S2 power-energy-crosscheck：能量记录带互核输入留痕（capacityPct/chargeFullMah，
    # charge_full µAh→mAh /1000），会话级互核输入在会话内齐备
    assert events[1]["tags"] == {"source": "level_slope", "capacityPct": 75.0, "chargeFullMah": 4368.0}
    for event in events:
        TimelineEvent.from_dict(event)


def test_power_sampler_unavailable_when_all_sources_missing() -> None:
    sampler = PowerSampler()
    ctx = _sampling_ctx({"current_now": [(1, "cat: Permission denied")]}, [0])

    assert sampler.probe(ctx) == ProbeResult.unavailable(
        "power_supply battery nodes unreadable: /sys/class/power_supply/battery/*"
    )
    assert sampler.sample(ctx) == []
    assert sampler.drain_diagnostics() == ["power_supply read failed; sample skipped"]


def test_gpu_thermal_busy_differential_and_multi_zone_hand_computed() -> None:
    sampler = GpuThermalSampler(interval_ms=2000, utc_now=lambda: _UTC)
    ts = [1000]
    ctx = _sampling_ctx(
        {
            "gpubusy": [(0, _golden("kgsl_gpubusy.txt")), (0, _golden("kgsl_gpubusy_t2.txt"))],
            "dumpsys thermalservice": [(0, _golden("thermalservice.txt"))],
            "clock_mhz": [(0, _golden("kgsl_clock_mhz.txt"))],
        },
        ts,
    )

    sampler.start(ctx)  # busy 基线 12800/40000
    ts[0] = 3000
    records = sampler.sample(ctx)

    # Δbusy=8960、Δtotal=20000 → 44.8%；温度取 "Current temperatures from HAL" 段
    # （Cached 段同名 zone 被更新值覆盖：AP 36.3→31.9）；SKIN/USB/CP/PA 不在选型内。
    # S1 gpu-freq/thermal-component 扩展：freq 瞬时 231MHz（SM_F9560 实测 golden）；
    # thermalservice.txt 增 GPU zone → per-zone 4 条 + 分量 cpu/gpu/btemp 各取该类 max
    assert [(record.metric_id, record.value) for record in records] == [
        ("gpu.busy.pct", 44.8),
        ("gpu.freq.mhz", 231.0),
        ("thermal.max.c", 31.9),
        ("thermal.max.c", 30.1),
        ("thermal.max.c", 36.1),
        ("thermal.max.c", 27.9),
        ("thermal.cpu.c", 31.9),
        ("thermal.gpu.c", 36.1),
        ("thermal.btemp.c", 30.1),
    ]
    assert [record.tags["zone"] for record in records[2:]] == [
        "ap", "bat", "gpu", "subbat", "ap", "gpu", "bat",
    ]
    assert records[0].tags["unit"] == "%"
    assert records[1].tags["unit"] == "MHz"
    assert all(record.tags["unit"] == "°C" for record in records[2:])
    assert all(record.tags["samplingWindowMs"] == 2000 for record in records)


def test_gpu_thermal_probe_branches_report_precise_reasons() -> None:
    busy = _golden("kgsl_gpubusy.txt")
    thermal = _golden("thermalservice_emulator.txt")
    gpu_reason = f"gpu busy unavailable: {GPUBUSY_PATH} unreadable"
    thermal_reason = (
        "thermal unavailable: no readings from dumpsys thermalservice "
        "or /sys/class/thermal/thermal_zone*"
    )
    # S1 gpu-freq 扩展：probe 额外探测 clock_mhz（降源 cur_freq）；双源失败 → freq
    # 子源关停（_freq_enabled=False），后续 sample 静默不出 freq 点不刷 diagnostic
    freq_broken = [
        (2, "cat: /sys/class/kgsl/kgsl-3d0/clock_mhz: No such file or directory"),
        (2, "cat: /sys/class/kgsl/kgsl-3d0/devfreq/cur_freq: No such file or directory"),
    ]

    # 三子源可用
    both = GpuThermalSampler()
    both_ctx = _sampling_ctx(
        {
            "gpubusy": [(0, busy)],
            "dumpsys thermalservice": [(0, thermal)],
            "clock_mhz": [(0, _golden("kgsl_clock_mhz.txt"))],
        },
        [0],
    )
    assert both.probe(both_ctx) == ProbeResult.ok()

    # 模拟器无 kgsl：GPU/频率子项 unavailable，温度可用 → 整体 available（子项降级）
    emulator = GpuThermalSampler(utc_now=lambda: _UTC)
    ts = [0]
    emulator_ctx = _sampling_ctx(
        {
            "gpubusy": [(2, "cat: /sys/class/kgsl/kgsl-3d0/gpubusy: No such file or directory")],
            "clock_mhz": [freq_broken[0]],
            "cur_freq": [freq_broken[1]],
            "dumpsys thermalservice": [(0, thermal), (0, thermal)],
        },
        ts,
    )
    assert emulator.probe(emulator_ctx) == ProbeResult(available=True, reason=f"degraded: {gpu_reason}")
    emulator.start(emulator_ctx)
    records = emulator.sample(emulator_ctx)
    # 模拟器路径：无 freq/busy；温度出 battery per-zone + btemp 分量（同一 zone）
    assert [(record.metric_id, record.value, record.tags["zone"]) for record in records] == [
        ("thermal.max.c", 30.2, "battery"),
        ("thermal.btemp.c", 30.2, "battery"),
    ]
    assert all(record.tags["unit"] == "°C" for record in records)

    # kgsl 可用但温度双源均不可用 → 整体 available + 温度子项降级
    no_thermal = GpuThermalSampler()
    no_thermal_ctx = _sampling_ctx(
        {
            "gpubusy": [(0, busy)],
            "dumpsys thermalservice": [(1, "")],
            "thermal_zone": [(1, "")],
            "clock_mhz": [(0, _golden("kgsl_clock_mhz.txt"))],
        },
        [0],
    )
    assert no_thermal.probe(no_thermal_ctx) == ProbeResult(available=True, reason=f"degraded: {thermal_reason}")

    # 三子源均不可用 → unavailable，原因逐项精确（freq 关停不进 unavailable 原因）
    none_sampler = GpuThermalSampler()
    none_ctx = _sampling_ctx(
        {
            "gpubusy": [(2, "")],
            "dumpsys thermalservice": [(1, "")],
            "thermal_zone": [(1, "")],
            "clock_mhz": [freq_broken[0]],
            "cur_freq": [freq_broken[1]],
        },
        [0],
    )
    assert none_sampler.probe(none_ctx) == ProbeResult.unavailable(f"{gpu_reason}; {thermal_reason}")


def test_gpu_thermal_subsource_failures_are_isolated_with_diagnostics() -> None:
    # GPU 子源中途读失败：本跳点跳过 GPU，freq/温度照常出数
    gpu_broken = GpuThermalSampler(utc_now=lambda: _UTC)
    ts = [1000]
    gpu_broken_ctx = _sampling_ctx(
        {
            "gpubusy": [(0, _golden("kgsl_gpubusy.txt")), (2, "cat: Permission denied")],
            "dumpsys thermalservice": [(0, _golden("thermalservice.txt"))],
            "clock_mhz": [(0, _golden("kgsl_clock_mhz.txt"))],
        },
        ts,
    )
    gpu_broken.start(gpu_broken_ctx)
    ts[0] = 2000
    records = gpu_broken.sample(gpu_broken_ctx)

    assert all(record.metric_id in {"gpu.freq.mhz", "thermal.max.c", "thermal.cpu.c", "thermal.gpu.c", "thermal.btemp.c"} for record in records)
    assert (
        "sampler.gpu_thermal.gpu.read_failed:gpubusy read failed; "
        "gpu.busy.pct skipped this round"
    ) in gpu_broken.drain_diagnostics()

    # 温度子源失败（dumpsys + sysfs 双双不可用）：GPU/freq 照常出数
    thermal_broken = GpuThermalSampler(utc_now=lambda: _UTC)
    ts2 = [1000]
    thermal_broken_ctx = _sampling_ctx(
        {
            "gpubusy": [(0, _golden("kgsl_gpubusy.txt")), (0, _golden("kgsl_gpubusy_t2.txt"))],
            "dumpsys thermalservice": [(1, "")],
            "thermal_zone": [(1, "")],
            "clock_mhz": [(0, _golden("kgsl_clock_mhz.txt"))],
        },
        ts2,
    )
    thermal_broken.start(thermal_broken_ctx)
    ts2[0] = 3000
    records2 = thermal_broken.sample(thermal_broken_ctx)

    assert [(record.metric_id, record.value) for record in records2] == [
        ("gpu.busy.pct", 44.8),
        ("gpu.freq.mhz", 231.0),
    ]
    assert (
        "sampler.gpu_thermal.thermal.read_failed:thermal sources read failed; "
        "thermal.max.c skipped this round"
    ) in thermal_broken.drain_diagnostics()


def test_gpu_thermal_counter_wraparound_and_zero_increment_rebaseline() -> None:
    # busy 计数器回绕（Δbusy<0）与统计窗口整体清零（Δtotal<=0）均重建基线/跳点并记
    # diagnostic，不产虚高值（与 net 侧计数器覆盖对齐，R1 修复）
    sampler = GpuThermalSampler(utc_now=lambda: _UTC)
    ts = [1000]
    ctx = _sampling_ctx(
        {
            "gpubusy": [
                (0, _golden("kgsl_gpubusy.txt")),  # start 基线 12800/40000
                (0, "  800  2000\n"),  # 计数器清零重计：Δbusy<0 → 回绕重建
                (0, "  800  2000\n"),  # 无增量：Δtotal=0 → 跳点
                (0, "  1280  8000\n"),  # 恢复累计：Δbusy=480、Δtotal=6000 → 8.0%
            ],
            "dumpsys thermalservice": [(0, _golden("thermalservice.txt"))] * 3,
            "clock_mhz": [(0, _golden("kgsl_clock_mhz.txt"))] * 3,
        },
        ts,
    )

    sampler.start(ctx)
    ts[0] = 3000
    assert [record for record in sampler.sample(ctx) if record.metric_id == "gpu.busy.pct"] == []
    assert sampler.drain_diagnostics() == [
        "sampler.gpu_thermal.gpu.counter_reset:gpu busy counter reset detected; re-baselined"
    ]

    ts[0] = 5000
    assert [record for record in sampler.sample(ctx) if record.metric_id == "gpu.busy.pct"] == []
    assert sampler.drain_diagnostics() == [
        "sampler.gpu_thermal.gpu.no_increment:gpu total counter no increment; skipped this round"
    ]

    ts[0] = 7000
    records = sampler.sample(ctx)
    assert [(record.metric_id, record.value) for record in records if record.metric_id == "gpu.busy.pct"] == [
        ("gpu.busy.pct", 8.0)
    ]


def test_gpu_thermal_sysfs_fallback_zones_with_millicelsius_conversion() -> None:
    sampler = GpuThermalSampler(utc_now=lambda: _UTC)
    ts = [1000]
    ctx = _sampling_ctx(
        {
            "gpubusy": [
                (0, _golden("kgsl_gpubusy.txt")),
                (0, _golden("kgsl_gpubusy.txt")),
                (0, _golden("kgsl_gpubusy_t2.txt")),
            ],
            "dumpsys thermalservice": [(1, ""), (1, "")],
            "thermal_zone": [(0, _golden("thermal_zone_sysfs.txt")), (0, _golden("thermal_zone_sysfs.txt"))],
            "clock_mhz": [(0, _golden("kgsl_clock_mhz.txt")), (0, _golden("kgsl_clock_mhz.txt"))],
        },
        ts,
    )

    assert sampler.probe(ctx) == ProbeResult.ok()  # 温度经 sysfs fallback 仍可用
    sampler.start(ctx)
    ts[0] = 3000
    records = sampler.sample(ctx)

    # 毫度 /1000（>=1000 判定）；skin-weather 不在 CPU/GPU/battery 选型内。
    # S1 分量扩展：sysfs 降源（mType=None）按名字归类 → cpu（cpu0/cpu-0-1 取 max）、
    # gpu（gpu0）、btemp（battery）三分量各出一条（tags.zone 为贡献 zone）
    assert [(record.value, record.tags["zone"]) for record in records if record.metric_id == "thermal.max.c"] == [
        (36.5, "battery"),
        (39.5, "cpu-0-1"),
        (41.0, "cpu0"),
        (38.5, "gpu0"),
    ]
    assert [
        (record.metric_id, record.value, record.tags.get("zone"))
        for record in records
        if record.metric_id != "thermal.max.c"
    ] == [
        ("gpu.busy.pct", 44.8, None),
        ("gpu.freq.mhz", 231.0, None),
        ("thermal.cpu.c", 41.0, "cpu0"),
        ("thermal.gpu.c", 38.5, "gpu0"),
        ("thermal.btemp.c", 36.5, "battery"),
    ]


def test_gpu_thermal_records_flow_through_parse_chain() -> None:
    records = [
        SampleRecord(
            metric_id="gpu.busy.pct",
            value=44.8,
            ts_ms=3000,
            tags={"unit": "%", "collector": "sampler.gpu_thermal", "samplingWindowMs": 2000, "tsUtc": _UTC},
        ),
        SampleRecord(
            metric_id="thermal.max.c",
            value=31.9,
            ts_ms=3000,
            tags={
                "unit": "°C",
                "collector": "sampler.gpu_thermal",
                "zone": "ap",
                "samplingWindowMs": 2000,
                "tsUtc": _UTC,
            },
        ),
    ]

    events = parse_samples_events(records, run_id="run-gpu")

    assert events[0]["data"] == {"value": 44.8, "direction": "none", "unit": "%", "samplingWindowMs": 2000}
    assert events[1]["data"]["unit"] == "°C"
    assert events[1]["tags"] == {"zone": "ap"}
    assert events[1]["tsUtc"] == _UTC
    for event in events:
        TimelineEvent.from_dict(event)


# --- F0：entry 配置透传 / evidenceLevel 映射 / mem 采样器诊断机制对齐 ---


def test_build_samplers_forwards_entry_config_keys_to_factories() -> None:
    samplers = build_samplers({"cpu": {"enabled": "auto", "intervalMs": 1500, "perCore": True}})

    assert len(samplers) == 1
    assert samplers[0].name == "cpu"
    assert samplers[0].interval_ms == 1500
    assert samplers[0].include_per_core is True


def test_build_samplers_ignores_unknown_entry_keys_and_keeps_default_behavior() -> None:
    samplers = build_samplers({"cpu": {"unknownKey": 1}, "mem": True, "cpu_off": False})

    assert [sampler.name for sampler in samplers] == ["cpu", "mem"]
    # 现网 YAML 不带新键时行为不变（include_per_core 默认关闭）
    assert samplers[0].include_per_core is False
    assert samplers[0].interval_ms == 1000
    assert samplers[1].interval_ms == 2000


def test_parse_samples_events_maps_evidence_levels_per_metric_sources() -> None:
    records = [
        SampleRecord(
            metric_id="gpu.busy.pct",
            value=41.0,
            ts_ms=1000,
            tags={"unit": "%", "collector": "sampler.gpu_thermal"},
        ),
        SampleRecord(
            metric_id="power.energy.mah",
            value=0.83,
            ts_ms=1000,
            tags={"unit": "mAh", "collector": "sampler.power"},
        ),
        SampleRecord(
            metric_id="cpu.proc.pct",
            value=12.0,
            ts_ms=1000,
            tags={"unit": "%", "collector": "sampler.cpu"},
        ),
        SampleRecord(
            metric_id="net.rx.kbps",
            value=900.0,
            ts_ms=1000,
            tags={"unit": "KB/s", "collector": "sampler.net", "evidenceLevel": "L2"},
        ),
    ]

    events = parse_samples_events(records, run_id="run-evidence")

    # METRIC_SOURCES §4：gpu.busy.pct / power.energy.mah 为 L2，其余默认 L1
    assert events[0]["source"]["evidenceLevel"] == "L2"
    assert events[1]["source"]["evidenceLevel"] == "L2"
    assert events[2]["source"]["evidenceLevel"] == "L1"
    # 事件 tags 中 evidenceLevel 显式覆盖映射，且不残留在 tags 里
    assert events[3]["source"]["evidenceLevel"] == "L2"
    assert "evidenceLevel" not in events[3].get("tags", {})
    for event in events:
        TimelineEvent.from_dict(event)


def test_mem_sampler_diagnostics_drain_contract_aligned_with_other_samplers() -> None:
    sampler = MemSampler()

    assert sampler.diagnostics == []
    assert sampler.drain_diagnostics() == []
    # drain 幂等：取走即清空
    assert sampler.drain_diagnostics() == []


def test_mem_sampler_pss_failure_streak_emits_single_diagnostic_per_streak() -> None:
    ok_stdout = (GOLDEN_ROOT / "meminfo-light.txt").read_text(encoding="utf-8")
    failing_ctx = DeviceContext(
        adb=AdbClient("/adb"),
        command_runner=_meminfo_runner({"meminfo": (1, "")}),
        serial="emulator-5554",
        package_name="com.perfeval.fixture",
    )
    ok_ctx = DeviceContext(
        adb=AdbClient("/adb"),
        command_runner=_meminfo_runner({"meminfo": (0, ok_stdout)}),
        serial="emulator-5554",
        package_name="com.perfeval.fixture",
    )
    sampler = MemSampler()

    expected = "sampler.mem.pss.unavailable:dumpsys meminfo failed for 2 consecutive samples"
    # 第 1 次失败不记（未达阈值）
    assert sampler.sample(failing_ctx) == []
    assert sampler.drain_diagnostics() == []
    # 第 2 次连续失败记一条 diagnostic（mem 采样失败可见）
    assert sampler.sample(failing_ctx) == []
    assert sampler.drain_diagnostics() == [expected]
    # 连续失败期间不重复记
    assert sampler.sample(failing_ctx) == []
    assert sampler.drain_diagnostics() == []
    # 恢复成功清零连击，之后重新计数
    assert sampler.sample(ok_ctx)
    assert sampler.drain_diagnostics() == []
    assert sampler.sample(failing_ctx) == []
    assert sampler.drain_diagnostics() == []
    assert sampler.sample(failing_ctx) == []
    assert sampler.drain_diagnostics() == [expected]


# ---- 2026-11-04 体检修复回归（cpu-4 / net-1 / net-2 / power-1 / gpu-3）----


def test_cpu_sampler_total_read_failure_counts_and_diagnoses() -> None:
    """cpu-4 修复：整机 /proc/stat 读失败不再整窗静默——skipped_total_samples
    计数、连续 3 次记 sampler.cpu.stat.unavailable（恢复清零，与进程缺失路径
    _track_proc_miss 对称；此前该失败对调度器 sampler.failed 机制也不可见）。"""
    runner = _ScriptedCpuRunner()
    sampler = CpuSampler(utc_now=lambda: UTC_STUB)
    sampler.start(_cpu_ctx(runner, elapsed_ms=0))

    runner.fail_stat = True
    for elapsed in (1000, 2000, 3000):
        assert sampler.sample(_cpu_ctx(runner, elapsed_ms=elapsed)) == []
    assert sampler.skipped_total_samples == 3
    assert sampler.drain_diagnostics() == [
        "sampler.cpu.stat.unavailable:/proc/stat read failed for 3 consecutive samples"
    ]
    # 恢复后连击清零，再次失败需重新累计到阈值（不重复刷屏）
    runner.fail_stat = False
    assert sampler.sample(_cpu_ctx(runner, elapsed_ms=4000))
    assert sampler.drain_diagnostics() == []
    runner.fail_stat = True
    assert sampler.sample(_cpu_ctx(runner, elapsed_ms=5000)) == []
    assert sampler.skipped_total_samples == 4
    assert sampler.drain_diagnostics() == []


def test_net_sampler_qtaguid_multi_user_uid_normalized() -> None:
    """net-1 修复：目标应用运行在副用户（Work profile/Secure Folder）时，qtaguid
    记完整 uid（100000×userId+appId）——按 appId 归一命中，不再 (0,0) 静默假零。"""
    pm = "package:com.perfeval.fixture uid:10112\n"
    # 表中目标流量挂 uid=1010112（user 10 × 100000 + 10112）；无 user 0 的 10112 行
    qtaguid_t0 = (
        "idx iface acct_tag_hex uid_tag_int cnt_set rx_bytes rx_packets tx_bytes tx_packets\n"
        "4 wlan0 0x0 1010112 0 200000 200 100000 100\n"
    )
    qtaguid_t1 = (
        "idx iface acct_tag_hex uid_tag_int cnt_set rx_bytes rx_packets tx_bytes tx_packets\n"
        "4 wlan0 0x0 1010112 0 237500 240 115000 120\n"
    )
    sampler = NetSampler(interval_ms=1000, utc_now=lambda: _UTC)
    ts = [1000]
    ctx = _sampling_ctx(
        {
            "pm list packages": [(0, pm)],
            "xt_qtaguid": [(0, qtaguid_t0), (0, qtaguid_t1)],
        },
        ts,
    )
    sampler.start(ctx)
    ts[0] = 4000
    records = sampler.sample(ctx)
    # Δrx=37500B、Δtx=15000B、Δt=3000ms —— 修复前产出恒 0（uid 查不到静默假零）
    by_metric = {r.metric_id: r.value for r in records}
    assert by_metric["net.rx.kbps"] == 100.0
    assert by_metric["net.tx.kbps"] == 40.0
    assert all(r.tags["source"] == "qtaguid" for r in records)


def test_net_sampler_tags_measured_window_not_nominal() -> None:
    """net-2 修复：samplingWindowMs = 实测差分窗口（本例 3000ms），非名义 1000ms
    ——慢源（dumpsys netstats 秒级耗时）/调度串行会拉长真实窗口，按标签二次归一
    的消费方不再算错（kbps 值本身一直按真实 delta_ms 计算）。"""
    pm = "package:com.perfeval.fixture uid:10012\n"
    t0 = "idx iface acct_tag_hex uid_tag_int cnt_set rx_bytes rx_packets tx_bytes tx_packets\n4 wlan0 0x0 10012 0 200000 200 100000 100\n"
    t1 = "idx iface acct_tag_hex uid_tag_int cnt_set rx_bytes rx_packets tx_bytes tx_packets\n4 wlan0 0x0 10012 0 237500 240 115000 120\n"
    sampler = NetSampler(interval_ms=1000, utc_now=lambda: _UTC)
    ts = [1000]
    ctx = _sampling_ctx(
        {"pm list packages": [(0, pm)], "xt_qtaguid": [(0, t0), (0, t1)]},
        ts,
    )
    sampler.start(ctx)
    ts[0] = 4000  # 真实到达间隔 3000ms（≠名义 1000ms）
    records = sampler.sample(ctx)
    assert all(r.tags["samplingWindowMs"] == 3000 for r in records)
    assert records[0].value == 100.0  # 37500B × 8 / 3000ms


def test_power_sampler_primary_source_emits_energy_per_window() -> None:
    """power-1 修复：主源（有 current_now）按 METRIC_SOURCES §3 要点 3 公式派生
    energy——端点电流均值 × 窗口时长 / 3600；每条 = 单窗口增量（power-2 口径
    统一，tags.samplingWindowMs 为实测窗口）。此前主源从不产出 energy，常见设备
    勾选后恒空。"""
    golden = _golden("power_supply_battery.txt")
    t2 = "current_now=-241250\nvoltage_now=3712000\ncapacity=75\ncharge_full=4368000\n"
    sampler = PowerSampler(interval_ms=1000, utc_now=lambda: _UTC)
    ts = [1000]
    ctx = _sampling_ctx(
        {"current_now": [(0, golden), (0, t2)]}, ts
    )
    sampler.start(ctx)  # 电流基线 482.5mA
    ts[0] = 3000
    records = sampler.sample(ctx)
    by_metric = {r.metric_id: r for r in records}
    # energy = mean(482.5, 241.25)mA × 2s / 3600 = 0.2010 mAh（估算）
    assert abs(by_metric["power.energy.mah"].value - 0.201) < 1e-6
    assert by_metric["power.energy.mah"].tags["source"] == "current_integral"
    assert by_metric["power.energy.mah"].tags["semantic"] == "estimated"
    assert by_metric["power.energy.mah"].tags["samplingWindowMs"] == 2000
    # 电流记录保留原始带符号读数（power-4）
    assert by_metric["power.current.ma"].tags["currentRawMa"] == -241.25


def test_scheduler_probe_records_degraded_reason() -> None:
    """gpu-3 修复：probe available=True + reason（子项降级）落盘
    ``sampler.<name>.degraded:<reason>``——此前该 reason 无任何消费点，gpu_thermal
    在无 kgsl 设备上 GPU 子源静默关停、报告只能兜底「无采样数据」。"""
    degraded = FakeSampler(
        name="gpu_thermal",
        probe_result=ProbeResult(
            available=True, reason="degraded: gpu busy unavailable: /sys/class/kgsl/kgsl-3d0/gpubusy unreadable"
        ),
    )
    scheduler = SamplingScheduler([degraded], context=_context(), clock=lambda: 0.0)
    scheduler.probe()
    # 采样器保留（温度继续采），降级原因进诊断（报告 chip 按 sampler.gpu_thermal.
    # 前缀匹配即可命中原文）
    assert scheduler.active_names == ["gpu_thermal"]
    # 采样器 reason 自带的 "degraded: " 前缀不叠加（复核意见③）：语义进 code
    # token，落盘只保留原因正文（probe 响应原样不动）
    assert scheduler.drain_diagnostics() == [
        "sampler.gpu_thermal.degraded:gpu busy unavailable: "
        "/sys/class/kgsl/kgsl-3d0/gpubusy unreadable"
    ]


def test_parse_samples_events_platform_and_ts_origin_parameters() -> None:
    """sim-2/sim-3 修复：``platform`` 参数（simulator 会话标 ios，与注册表
    platform="ios" 一致）；``ts_origin_ms`` 给出时 tsMonotonicNs 为 run 内相对
    纳秒（会话创建时刻为原点，单调、不受 NTP 回拨影响——此前后端 epoch ms ×1e6
    直填单调钟字段与 02-log-schema.md:146 契约不符）。缺省行为不变（CLI 相对
    时戳路径）。"""
    records = [
        SampleRecord(
            metric_id="memory.rss.mb",
            value=432.8,
            ts_ms=1_793_724_464_935,
            tags={"basis": "host"},
        ),
        SampleRecord(metric_id="memory.rss.mb", value=433.0, ts_ms=1_793_724_465_435, tags={}),
    ]
    created_at = 1_793_724_445_000

    events = parse_samples_events(
        records, run_id="run-ios", platform="ios", ts_origin_ms=created_at
    )
    assert events[0]["source"]["platform"] == "ios"
    # run 内相对纳秒：19_935ms → 19_935_000_000；单调递增
    assert events[0]["tsMonotonicNs"] == 19_935_000_000
    assert events[1]["tsMonotonicNs"] == 20_435_000_000
    assert events[1]["tsMonotonicNs"] > events[0]["tsMonotonicNs"]

    legacy = parse_samples_events(records, run_id="run-cli")
    assert legacy[0]["source"]["platform"] == "android"
    assert legacy[0]["tsMonotonicNs"] == 1_793_724_464_935_000_000
