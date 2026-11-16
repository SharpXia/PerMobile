"""2026-11-10 指标扩展注册与解析面（gap gpu-freq-missing / thermal-component-split）。

覆盖两缺口（memory-swap-vss-available 的解析面在 services/backend/tests/
test_meminfo_detail.py 扩展，注册面在本文末尾一并断言）：

- gpu.freq.mhz：kgsl 频率解析与出点（clock_mhz 主源 MHz 直读、devfreq/cur_freq
  降源 Hz→MHz 归一）——SM_F9560 真机样本固化（kgsl_clock_mhz.txt=231、
  kgsl_devfreq_cur_freq.txt=231000000，2026-11-10 实测）；
- thermal 分量：thermal.cpu.c/gpu.c/btemp.c 按分量出点（每分量取该类 zone 的
  max，tags.zone 保留），thermal.max.c per-zone 原口径不变；thermalservice.txt
  扩 mType 0/1/2 样本在场（S1 fixture 归属：thermalservice 样本归本流扩展）；
  真机样本 thermalservice_smf9560.txt 固化「无 GPU/NPU zone → 分量不出点非 0」
  路径（SM_F9560 Android 16 实测：dumpsys thermalservice 无 mType=1、无 NTemp）。

registry 行断言（契约 §5/§21 delta 由本流落笔）与 registry_body 门禁扩展见文末。
"""

from __future__ import annotations

import sys
from pathlib import Path
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services" / "backend"))

from perfeval.collectors.android.samplers.base import DeviceContext  # noqa: E402
from perfeval.collectors.android.samplers.gpu_thermal_sampler import (  # noqa: E402
    GpuThermalSampler,
    METRIC_GPU_BUSY_PCT,
    METRIC_GPU_FREQ_MHZ,
    METRIC_THERMAL_BTEMP_C,
    METRIC_THERMAL_CPU_C,
    METRIC_THERMAL_GPU_C,
    METRIC_THERMAL_MAX_C,
    parse_gpu_clock_mhz,
    parse_thermalservice,
    thermal_component_id,
)

from app.registry import get_metric, is_registered, registry_body  # noqa: E402

GOLDEN_ROOT = Path("perfeval/fixtures/android/golden")
_UTC = "2026-11-10T01:02:03.456Z"


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
        adb=type("A", (), {"command": staticmethod(
            lambda *args, serial=None: ["adb", *(["-s", serial] if serial else []), *args]
        )})(),
        command_runner=_scripted_runner(script),
        serial="TEST-SERIAL-001",
        package_name="com.perfeval.fixture",
        elapsed_ms=lambda: ts[0],
    )


# ---- gpu.freq.mhz：解析（真机样本 + Hz 降源归一）----


def test_parse_gpu_clock_mhz_golden_mhz_direct() -> None:
    """SM_F9560 实测：cat clock_mhz -> 231（MHz 直读）。"""
    assert parse_gpu_clock_mhz(_golden("kgsl_clock_mhz.txt")) == 231.0


def test_parse_gpu_clock_mhz_hz_fallback_normalized() -> None:
    """SM_F9560 实测：devfreq/cur_freq -> 231000000（Hz）→ 归一 231 MHz。"""
    assert parse_gpu_clock_mhz(_golden("kgsl_devfreq_cur_freq.txt")) == 231.0
    assert parse_gpu_clock_mhz("  1245000000\n") == 1245.0  # Hz 上限段
    assert parse_gpu_clock_mhz("not-a-number\n") is None
    assert parse_gpu_clock_mhz("") is None


def test_thermal_component_classification() -> None:
    """mType 枚举优先（0/1/2），sysfs 降级（None）按名字归类；SKIN/USB/CP 不归类。"""
    assert thermal_component_id("AP", 0) == METRIC_THERMAL_CPU_C
    assert thermal_component_id("GPU", 1) == METRIC_THERMAL_GPU_C
    assert thermal_component_id("BAT", 2) == METRIC_THERMAL_BTEMP_C
    assert thermal_component_id("SUBBAT", 2) == METRIC_THERMAL_BTEMP_C
    assert thermal_component_id("cpu0", None) == METRIC_THERMAL_CPU_C
    assert thermal_component_id("cpu-0-1", None) == METRIC_THERMAL_CPU_C
    assert thermal_component_id("gpu0", None) == METRIC_THERMAL_GPU_C
    assert thermal_component_id("battery", None) == METRIC_THERMAL_BTEMP_C
    # NPU/NTemp 无对应枚举映射：不出分量（SM_F9560/模拟器实测均无该 zone，
    # 即便 OEM 以任意 mType 上报也不映射——unavailable，不填 0）
    assert thermal_component_id("NTemp", 12) is None
    assert thermal_component_id("SKIN", 3) is None
    assert thermal_component_id("CP", 12) is None


# ---- gpu.freq.mhz：采样器出点（含降源与无 kgsl 路径）----


def test_gpu_thermal_sampler_emits_gpu_freq_record_with_tags() -> None:
    sampler = GpuThermalSampler(interval_ms=2000, utc_now=lambda: _UTC)
    ts = [1000]
    ctx = _sampling_ctx(
        {
            "gpubusy": [(0, _golden("kgsl_gpubusy.txt")), (0, _golden("kgsl_gpubusy_t2.txt"))],
            "clock_mhz": [(0, _golden("kgsl_clock_mhz.txt")), (0, _golden("kgsl_clock_mhz.txt"))],
            "thermalservice": [(0, _golden("thermalservice.txt")), (0, _golden("thermalservice.txt"))],
        },
        ts,
    )
    assert sampler.probe(ctx).available is True
    sampler.start(ctx)
    ts[0] = 3000
    records = sampler.sample(ctx)

    freq = [r for r in records if r.metric_id == METRIC_GPU_FREQ_MHZ]
    assert len(freq) == 1
    assert freq[0].value == 231.0
    assert freq[0].ts_ms == 3000
    assert freq[0].tags == {
        "unit": "MHz",
        "collector": "sampler.gpu_thermal",
        "samplingWindowMs": 2000,
        "tsUtc": _UTC,
    }
    # metric_ids 全集含新序列（CM-001 METRIC_SOURCES 命名面）
    assert METRIC_GPU_FREQ_MHZ in sampler.metric_ids


def test_gpu_thermal_sampler_freq_falls_back_to_devfreq_cur_freq() -> None:
    """clock_mhz 不可读 → devfreq/cur_freq 降源（Hz→MHz 归一）。"""
    sampler = GpuThermalSampler(interval_ms=1000, utc_now=lambda: _UTC)
    ts = [1000]
    ctx = _sampling_ctx(
        {
            "gpubusy": [(0, _golden("kgsl_gpubusy.txt")), (0, _golden("kgsl_gpubusy_t2.txt"))],
            "clock_mhz": [(2, "cat: Permission denied"), (2, "cat: Permission denied")],
            "cur_freq": [(0, _golden("kgsl_devfreq_cur_freq.txt")), (0, _golden("kgsl_devfreq_cur_freq.txt"))],
            "thermalservice": [(0, _golden("thermalservice.txt")), (0, _golden("thermalservice.txt"))],
        },
        ts,
    )
    assert sampler.probe(ctx).available is True
    sampler.start(ctx)
    ts[0] = 2000
    freq = [r for r in sampler.sample(ctx) if r.metric_id == METRIC_GPU_FREQ_MHZ]
    assert [(r.value) for r in freq] == [231.0]


def test_gpu_thermal_sampler_freq_skipped_with_diagnostic_when_unreadable() -> None:
    """kgsl 在场但频率双源均不可读：本跳点跳过并记 diagnostic（与 gpu 子源同哲学）。"""
    sampler = GpuThermalSampler(interval_ms=1000, utc_now=lambda: _UTC)
    ts = [1000]
    ctx = _sampling_ctx(
        {
            "gpubusy": [(0, _golden("kgsl_gpubusy.txt")), (0, _golden("kgsl_gpubusy_t2.txt"))],
            "clock_mhz": [(0, _golden("kgsl_clock_mhz.txt")), (2, "cat: Permission denied")],
            "cur_freq": [(2, "cat: Permission denied")],
            "thermalservice": [(0, _golden("thermalservice.txt")), (0, _golden("thermalservice.txt"))],
        },
        ts,
    )
    sampler.probe(ctx)
    sampler.start(ctx)
    ts[0] = 2000
    assert [r for r in sampler.sample(ctx) if r.metric_id == METRIC_GPU_FREQ_MHZ] == []
    assert any("gpu_freq.read_failed" in d for d in sampler.drain_diagnostics())


def test_gpu_thermal_sampler_freq_absent_without_kgsl() -> None:
    """模拟器无 kgsl：freq 通道不启用（不出点、不逐轮 diagnostic），温度照常出数。"""
    sampler = GpuThermalSampler(interval_ms=1000, utc_now=lambda: _UTC)
    ts = [0]
    ctx = _sampling_ctx(
        {
            "gpubusy": [(2, "cat: /sys/class/kgsl/kgsl-3d0/gpubusy: No such file or directory")],
            "clock_mhz": [(2, "cat: /sys/class/kgsl/kgsl-3d0/clock_mhz: No such file or directory")],
            "cur_freq": [(2, "cat: /sys/class/kgsl/kgsl-3d0/devfreq/cur_freq: No such file or directory")],
            "thermalservice": [(0, _golden("thermalservice_emulator.txt")), (0, _golden("thermalservice_emulator.txt"))],
        },
        ts,
    )
    probe = sampler.probe(ctx)
    assert probe.available is True and "degraded" in (probe.reason or "")
    sampler.start(ctx)
    records = sampler.sample(ctx)
    assert all(r.metric_id != METRIC_GPU_FREQ_MHZ for r in records)
    assert {r.metric_id for r in records} == {METRIC_THERMAL_MAX_C, METRIC_THERMAL_BTEMP_C}
    assert sampler.drain_diagnostics() == []  # 无 kgsl 的关停路径不刷 diagnostic


# ---- thermal 分量：golden 对拍（mType 0/1/2 样本在场）----


def test_thermal_components_from_golden_thermalservice() -> None:
    """thermalservice.txt（mType 0/1/2 样本在场）：三分量出点且每分量 = 该类 zone
    的 max（tags.zone 为贡献 zone）；thermal.max.c per-zone 原口径不变。"""
    sampler = GpuThermalSampler(interval_ms=2000, utc_now=lambda: _UTC)
    ts = [1000]
    ctx = _sampling_ctx(
        {
            "gpubusy": [(0, _golden("kgsl_gpubusy.txt")), (0, _golden("kgsl_gpubusy_t2.txt"))],
            "clock_mhz": [(0, _golden("kgsl_clock_mhz.txt")), (0, _golden("kgsl_clock_mhz.txt"))],
            "thermalservice": [(0, _golden("thermalservice.txt")), (0, _golden("thermalservice.txt"))],
        },
        ts,
    )
    sampler.probe(ctx)
    sampler.start(ctx)
    ts[0] = 3000
    records = sampler.sample(ctx)

    zones = parse_thermalservice(_golden("thermalservice.txt"))
    selected = {n: (t, v) for n, (t, v) in zones.items() if thermal_component_id(n, t)}
    max_records = [r for r in records if r.metric_id == METRIC_THERMAL_MAX_C]
    assert [(r.value, r.tags["zone"]) for r in max_records] == [
        (31.9, "ap"), (30.1, "bat"), (36.1, "gpu"), (27.9, "subbat"),
    ]
    components = {r.metric_id: r for r in records if r.metric_id in (
        METRIC_THERMAL_CPU_C, METRIC_THERMAL_GPU_C, METRIC_THERMAL_BTEMP_C,
    )}
    assert set(components) == {
        METRIC_THERMAL_CPU_C, METRIC_THERMAL_GPU_C, METRIC_THERMAL_BTEMP_C,
    }
    assert (components[METRIC_THERMAL_CPU_C].value, components[METRIC_THERMAL_CPU_C].tags["zone"]) == (31.9, "ap")
    assert (components[METRIC_THERMAL_GPU_C].value, components[METRIC_THERMAL_GPU_C].tags["zone"]) == (36.1, "gpu")
    assert (components[METRIC_THERMAL_BTEMP_C].value, components[METRIC_THERMAL_BTEMP_C].tags["zone"]) == (30.1, "bat")
    # 分量 <= 全 zone 最大值（"max ≥ 各分量"验收：分量是该 max 的子集口径）
    global_max = max(r.value for r in max_records)
    for record in components.values():
        assert record.value <= global_max
    assert all(r.tags["unit"] == "°C" for r in records if r.metric_id != METRIC_GPU_FREQ_MHZ)


def test_thermal_components_absent_types_emit_no_points() -> None:
    """真机样本（SM_F9560，无 mType=1 GPU zone、无 NPU）：thermal.gpu.c 不出点
    非 0（unavailable，[R§8.6]）；cpu/btemp 正常出点。"""
    real_output = _golden("thermalservice_smf9560.txt")
    sampler = GpuThermalSampler(interval_ms=1000, utc_now=lambda: _UTC)
    ts = [0]
    ctx = _sampling_ctx(
        {
            "gpubusy": [(2, "cat: Permission denied")],
            "clock_mhz": [(2, "cat: Permission denied")],
            "cur_freq": [(2, "cat: Permission denied")],
            "thermalservice": [(0, real_output), (0, real_output)],
        },
        ts,
    )
    assert sampler.probe(ctx).available is True  # 温度可用 → 子项降级
    sampler.start(ctx)
    records = sampler.sample(ctx)
    by_id = [r.metric_id for r in records]
    assert METRIC_THERMAL_GPU_C not in by_id
    assert METRIC_THERMAL_CPU_C in by_id and METRIC_THERMAL_BTEMP_C in by_id
    cpu = next(r for r in records if r.metric_id == METRIC_THERMAL_CPU_C)
    btemp = next(r for r in records if r.metric_id == METRIC_THERMAL_BTEMP_C)
    assert (cpu.value, cpu.tags["zone"]) == (26.3, "ap")  # 2026-11-10 实测读数
    assert (btemp.value, btemp.tags["zone"]) == (23.3, "bat")


def test_thermal_components_from_sysfs_fallback_names() -> None:
    """sysfs 降级（无 mType）：名字关键词归类（cpu0/cpu-0-1→cpu、gpu0→gpu、battery→btemp）。"""
    sampler = GpuThermalSampler(interval_ms=2000, utc_now=lambda: _UTC)
    ts = [1000]
    ctx = _sampling_ctx(
        {
            "gpubusy": [(0, _golden("kgsl_gpubusy.txt")), (0, _golden("kgsl_gpubusy_t2.txt"))],
            "clock_mhz": [(0, _golden("kgsl_clock_mhz.txt")), (0, _golden("kgsl_clock_mhz.txt"))],
            "thermalservice": [(1, ""), (1, "")],
            "thermal_zone": [(0, _golden("thermal_zone_sysfs.txt")), (0, _golden("thermal_zone_sysfs.txt"))],
        },
        ts,
    )
    assert sampler.probe(ctx) .available is True
    sampler.start(ctx)
    ts[0] = 3000
    records = sampler.sample(ctx)
    components = {(r.metric_id, r.tags["zone"]): r.value for r in records if r.metric_id in (
        METRIC_THERMAL_CPU_C, METRIC_THERMAL_GPU_C, METRIC_THERMAL_BTEMP_C,
    )}
    assert components == {
        (METRIC_THERMAL_CPU_C, "cpu0"): 41.0,      # max(41.0, 39.5)
        (METRIC_THERMAL_GPU_C, "gpu0"): 38.5,
        (METRIC_THERMAL_BTEMP_C, "battery"): 36.5,
    }


# ---- 注册表行（契约 §5/§21 delta；SAMPLER_BY_METRIC 登记由 S2 执行）----


def test_registry_gpu_freq_metric_spec() -> None:
    assert is_registered("gpu.freq.mhz")
    spec = get_metric("gpu.freq.mhz")
    assert (spec.unit, spec.direction, spec.evidence_level) == ("MHz", "none", "L2")
    assert spec.default_interval_ms == 2000
    assert spec.synthetic_available is False  # 与 gpu.busy.pct 同例：synthetic 无建模
    assert spec.real_available is True
    assert spec.gpu_source_required is True  # §20.3 判定第 3 条：kgsl 硬性依赖


def test_registry_thermal_component_specs() -> None:
    for metric_id in ("thermal.cpu.c", "thermal.gpu.c", "thermal.btemp.c"):
        assert is_registered(metric_id)
        spec = get_metric(metric_id)
        assert (spec.unit, spec.direction, spec.evidence_level) == ("°C", "lower_better", "L1")
        assert spec.default_interval_ms == 2000
        assert spec.synthetic_available is True and spec.real_available is True
        assert spec.gpu_source_required is False


def test_registry_memory_swap_vss_available_specs() -> None:
    for metric_id in ("memory.swap.mb", "memory.vss.mb"):
        assert is_registered(metric_id)
        spec = get_metric(metric_id)
        assert (spec.unit, spec.direction, spec.evidence_level) == ("MB", "lower_better", "L1")
        assert spec.default_interval_ms == 2000
        assert spec.meminfo_detail_required is True  # 整组门禁（§21.3 同款）
        assert spec.synthetic_available is False and spec.real_available is True
    available = get_metric("memory.available.mb")
    assert available.meminfo_detail_required is False  # 设备级指标，不挂明细段门禁
    assert (available.unit, available.direction) == ("MB", "lower_better")
    assert available.synthetic_available is False and available.real_available is True


def test_registry_body_gates_meminfo_detail_required_rows() -> None:
    """registry_body 门禁扩展：meminfoDetail 探测缺失 → swap/vss 禁勾 not_landed；
    available.mb（设备级）不受明细段门禁；platform_unsupported 优先级不覆写。"""
    from app.services.capability_matrix import device_profile

    profile = device_profile(
        usb=True, wifi=False, gpu_source="kgsl",
        cpu_cores=8, refresh_rate_hz=120, meminfo_detail=False,
    )
    body = registry_body("real", profile)
    by_id = {m["metricId"]: m for m in body["metrics"]}
    for metric_id in ("memory.swap.mb", "memory.vss.mb"):
        entry = by_id[metric_id]
        assert entry["available"] is False
        assert entry["capabilities"]["reason"] == "not_landed"
    assert by_id["memory.available.mb"]["available"] is True
    assert "reason" not in by_id["memory.available.mb"]["capabilities"]
    # swap/vss 不入 Memory Detail 四项勾选组（§21.3 组口径不变）
    from app.registry import MEMORY_DETAIL_GROUP
    assert set(MEMORY_DETAIL_GROUP).isdisjoint({"memory.swap.mb", "memory.vss.mb", "memory.available.mb"})
    # platform 优先：ios profile → platform_unsupported（非 not_landed）
    ios_profile = device_profile(
        usb=False, wifi=False, gpu_source=None, cpu_cores=8, refresh_rate_hz=None,
        meminfo_detail=False, platform="ios",
    )
    ios_body = registry_body("real", ios_profile)
    swap = {m["metricId"]: m for m in ios_body["metrics"]}["memory.swap.mb"]
    assert swap["available"] is False
    assert swap["capabilities"]["reason"] == "platform_unsupported"


def test_available_metric_ids_mode_level() -> None:
    real_ids = set(__import__("app.registry", fromlist=["available_metric_ids"]).available_metric_ids("real"))
    synthetic_ids = set(__import__("app.registry", fromlist=["available_metric_ids"]).available_metric_ids("synthetic"))
    assert "gpu.freq.mhz" not in synthetic_ids and "gpu.freq.mhz" in real_ids
    for metric_id in ("thermal.cpu.c", "thermal.gpu.c", "thermal.btemp.c"):
        assert metric_id in real_ids and metric_id in synthetic_ids
    for metric_id in ("memory.swap.mb", "memory.vss.mb", "memory.available.mb"):
        assert metric_id in real_ids and metric_id not in synthetic_ids
