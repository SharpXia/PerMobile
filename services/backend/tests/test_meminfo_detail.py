"""R17 内存明细（契约 §21.3）+ 2026-11-10 扩展（gap memory-swap-vss-available）。

golden 四明细项以 m6_fixtures 语料对拍；部分机型形态（无 GL/Graphics）单项
None、整组不可勾。2026-11-10 扩展：swap/vss 两键同次解析 + MemDetailSampler
增发 memory.swap.mb / memory.vss.mb / memory.available.mb 三 ID（缺行出 null 点，
value=None，append_null_record 同款语义不伪造 0）；真机 dumpsys meminfo 样本
（SM_F9560，Android 16）固化为 golden（meminfo_smf9560.txt / proc_meminfo_
smf9560.txt / proc_status_smf9560.txt）——真机事实：App Summary TOTAL 行为
``TOTAL PSS: … TOTAL RSS: … TOTAL SWAP PSS: n``，明细表无 VSS 列，
dumpsys 输出不含 MemAvailable（设备级取 /proc/meminfo）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

_REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDEN_ROOT = _REPO_ROOT / "perfeval" / "fixtures" / "android" / "golden"

from perfeval.collectors.android.samplers.base import DeviceContext  # noqa: E402

from app.registry import MEMORY_DETAIL_GROUP, get_metric  # noqa: E402
from app.services.meminfo_detail import (  # noqa: E402
    DETAIL_KEYS,
    METRIC_AVAILABLE_MB,
    METRIC_SWAP_MB,
    METRIC_VSS_MB,
    MemDetailSampler,
    detail_channel_available,
    detail_complete,
    parse_meminfo_detail,
    parse_proc_meminfo_available_mb,
    parse_proc_memory_snapshot,
    parse_vm_size_mb,
)
from m6_fixtures import (  # noqa: E402
    MEMINFO_DETAIL_FIXTURE,
    MEMINFO_DETAIL_GOLDEN_MB,
    MEMINFO_DETAIL_PARTIAL_FIXTURE,
)


def _golden(name: str) -> str:
    return (GOLDEN_ROOT / name).read_text(encoding="utf-8")


# ---- 解析（golden 对拍）----


def test_parse_meminfo_detail_golden_four_items() -> None:
    detail = parse_meminfo_detail(MEMINFO_DETAIL_FIXTURE)
    assert {key: detail[key] for key in DETAIL_KEYS} == MEMINFO_DETAIL_GOLDEN_MB
    assert detail_complete(detail) is True


def test_parse_meminfo_detail_partial_device() -> None:
    """部分机型：无 GL mtrack/Graphics → 单项 None（不填假零值），采样层逐项出点。"""
    detail = parse_meminfo_detail(MEMINFO_DETAIL_PARTIAL_FIXTURE)
    assert detail["native_mb"] == 8.0    # 8,192 KB
    assert detail["dalvik_mb"] == 6.5    # 6,144 + 512 KB
    assert detail["gfx_mb"] is None
    assert detail["gl_mb"] is None
    assert detail_complete(detail) is False


def test_detail_channel_available_partial_device_passes() -> None:
    """§21.3 门禁修订（2026-11-16）：图形行全缺（SM_F9560/模拟器 system 实测形态）
    但 native/dalvik 可解析 → 通道存在 → 整组可勾（旧 detail_complete 判据误禁）。"""
    detail = parse_meminfo_detail(MEMINFO_DETAIL_PARTIAL_FIXTURE)
    assert detail_channel_available(detail) is True
    assert detail_complete(detail) is False  # 判据分离：complete 仍是四项全齐口径


def test_detail_channel_available_real_device_gfx_missing() -> None:
    """SM_F9560 真机形态：有 GL mtrack、无 Gfx dev/EGL（gfx_mb=None）→ 通道放行。"""
    text = (
        "** MEMINFO in pid 2748 [system] **\n"
        "  Native Heap    75196    71412     3772    20319    78068\n"
        "  Dalvik Heap   144856   143556     1240     4036   150436\n"
        "   GL mtrack     5064     5064        0        0     5064\n"
        "        TOTAL   405194   252796    60172    32151   656048\n"
    )
    detail = parse_meminfo_detail(text)
    assert detail["native_mb"] is not None
    assert detail["gfx_mb"] is None
    assert detail["gl_mb"] is not None
    assert detail_channel_available(detail) is True


def test_detail_channel_available_no_section() -> None:
    """明细段整体缺失（含 None）→ 通道不存在（not_landed 口径不变）。"""
    assert detail_channel_available(None) is False
    assert detail_channel_available(parse_meminfo_detail("No process found\n")) is False


def test_parse_meminfo_detail_no_section_returns_none() -> None:
    assert parse_meminfo_detail("No process found\n") is None
    assert parse_meminfo_detail("") is None
    # App Summary 段不跨段解析（perfeval 既有解析域）
    app_summary_only = "App Summary\n           Java Heap:   26,000\n"
    assert parse_meminfo_detail(app_summary_only) is None


def test_parse_meminfo_detail_ignores_app_summary_section() -> None:
    """App Summary 的 Native Heap: 46,880（Private Dirty 口径）不覆盖明细段
    PSS Total 口径（47,104）——两段语义不同，不得混采。"""
    detail = parse_meminfo_detail(MEMINFO_DETAIL_FIXTURE)
    assert detail["native_mb"] == 46.0  # 47,104 KB，非 App Summary 的 46,880 KB


# ---- 2026-11-10 扩展：swap / vss 解析（真机样本 + 机型行名差异容忍）----


def test_parse_meminfo_detail_real_smf9560_swap_line() -> None:
    """真机样本（SM_F9560，Android 16）：App Summary TOTAL 行
    ``TOTAL PSS: … TOTAL RSS: … TOTAL SWAP PSS: 93646`` → swap_mb=91.451；
    dumpsys 明细表无 VSS 列 → vss_mb=None（VSS 由 /proc/<pid>/status 兜底）。"""
    detail = parse_meminfo_detail(_golden("meminfo_smf9560.txt"))
    assert detail["swap_mb"] == 91.451  # 93,646 KB
    assert detail["vss_mb"] is None
    assert detail["native_mb"] == 0.816  # 836 KB（2026-11-10 实测读数）
    assert detail["gfx_mb"] is None and detail["gl_mb"] is None


def test_parse_meminfo_detail_swap_label_variants() -> None:
    """机型行名差异容忍：TOTAL SWAP PSS / TOTAL SWAP (PSS) / TOTAL SWAP 三形态。"""
    base = MEMINFO_DETAIL_PARTIAL_FIXTURE
    swapped = base.replace(
        " App Summary",
        " App Summary\n           TOTAL PSS: 14,848   TOTAL RSS: 20,000"
        "   TOTAL SWAP PSS: 4,096",
    )
    assert parse_meminfo_detail(swapped)["swap_mb"] == 4.0
    paren = swapped.replace("TOTAL SWAP PSS: 4,096", "TOTAL SWAP (PSS): 2,048")
    assert parse_meminfo_detail(paren)["swap_mb"] == 2.0
    plain = swapped.replace("TOTAL SWAP PSS: 4,096", "TOTAL SWAP: 1,024")
    assert parse_meminfo_detail(plain)["swap_mb"] == 1.0


def test_parse_meminfo_detail_oem_total_vss_row() -> None:
    """OEM 变体：明细段 TOTAL 行/行名携带 VSS（AOSP 明细表无此列，SM_F9560 实测）
    → vss_mb 取用；AOSP 原样 TOTAL 行（SwapPss 列无 vss 词）恒 None。"""
    oem = MEMINFO_DETAIL_PARTIAL_FIXTURE.replace(
        "       TOTAL    14,848   14,480        0",
        "       TOTAL    14,848   14,480        0        VSS 2,048 kB",
    )
    assert parse_meminfo_detail(oem)["vss_mb"] == 2.0
    aosp = parse_meminfo_detail(MEMINFO_DETAIL_FIXTURE)
    assert aosp["vss_mb"] is None  # SwapPss 列数值不误判为 VSS


# ---- 设备级 /proc 解析（memory.available.mb / VSS 兜底源）----


def test_parse_proc_meminfo_available_mb_real_sample() -> None:
    """SM_F9560 实测：/proc/meminfo MemAvailable: 4800560 kB → 4688.047 MB。"""
    assert parse_proc_meminfo_available_mb(_golden("proc_meminfo_smf9560.txt")) == 4688.047
    assert parse_proc_meminfo_available_mb("MemTotal: 100 kB\n") is None
    assert parse_proc_meminfo_available_mb("") is None


def test_parse_vm_size_mb_real_sample() -> None:
    """SM_F9560 实测：/proc/<pid>/status VmSize: 20472344 kB → 19992.523 MB。"""
    assert parse_vm_size_mb(_golden("proc_status_smf9560.txt")) == 19992.523
    assert parse_vm_size_mb("Name:\tapp\nPid:\t1\n") is None


def test_parse_proc_memory_snapshot() -> None:
    """采样器合并 shell 输出（echo key=value）解析；缺失节点输出空值 → None。"""
    snapshot = parse_proc_memory_snapshot(
        "pd_memavail=MemAvailable:    4800560 kB\npd_vss=VmSize:\t20472344 kB\n"
    )
    assert snapshot == {"memavail_mb": 4688.047, "vss_mb": 19992.523}
    assert parse_proc_memory_snapshot("pd_memavail=\npd_vss=\n") == {
        "memavail_mb": None, "vss_mb": None,
    }
    assert parse_proc_memory_snapshot("") == {"memavail_mb": None, "vss_mb": None}


# ---- 注册表 ----


def test_memory_detail_group_registered() -> None:
    assert MEMORY_DETAIL_GROUP == (
        "memory.detail.native_mb", "memory.detail.dalvik_mb",
        "memory.detail.gfx_mb", "memory.detail.gl_mb",
    )
    for metric_id in MEMORY_DETAIL_GROUP:
        spec = get_metric(metric_id)
        assert spec.unit == "MB"
        assert spec.direction == "lower_better"
        assert spec.evidence_level == "L1"
        assert spec.default_interval_ms == 2000
        assert "Memory Detail" in (spec.description or "")


def test_memory_swap_vss_available_registered() -> None:
    """2026-11-10 三行（契约 §21.3 delta）：swap/vss 挂 meminfo_detail_required
    整组门禁；available 设备级不挂；三项 synthetic 无建模（available=false 规则
    同 gpu.busy.pct 先例）。"""
    for metric_id in ("memory.swap.mb", "memory.vss.mb"):
        spec = get_metric(metric_id)
        assert (spec.unit, spec.direction, spec.evidence_level) == ("MB", "lower_better", "L1")
        assert spec.default_interval_ms == 2000
        assert spec.meminfo_detail_required is True
        assert spec.synthetic_available is False and spec.real_available is True
    available = get_metric("memory.available.mb")
    assert available.meminfo_detail_required is False
    assert (available.unit, available.direction) == ("MB", "lower_better")
    assert available.synthetic_available is False and available.real_available is True


# ---- MemDetailSampler（fixture 端到端）----


class _Adb:
    @staticmethod
    def command(*args: str, serial: str | None = None) -> list[str]:
        return ["adb", *(["-s", serial] if serial else []), *args]


def _runner_for(meminfo_stdout: str, proc_stdout: str = ""):
    """dumpsys meminfo + /proc 合并命令双路应答（pd_memavail 行识别）。"""

    def runner(command: list[str], timeout: float | None):
        joined = " ".join(command)
        if "pd_memavail" in joined:
            return SimpleNamespace(stdout=proc_stdout, returncode=0, stderr="")
        assert "dumpsys" in joined and "meminfo" in joined
        return SimpleNamespace(stdout=meminfo_stdout, returncode=0, stderr="")

    return runner


def _make_ctx(runner, package: str = "com.example.synthetic.game", elapsed: int = 500):
    return DeviceContext(
        adb=_Adb(), command_runner=runner, serial="S1",
        package_name=package, elapsed_ms=lambda: elapsed,
    )


def test_mem_detail_sampler_emits_four_records() -> None:
    sampler = MemDetailSampler(interval_ms=2000)
    ctx = _make_ctx(_runner_for(MEMINFO_DETAIL_FIXTURE, ""))
    assert sampler.probe(ctx).available is True
    records = sampler.sample(ctx)
    by_id = {r.metric_id: r.value for r in records}
    assert {k: v for k, v in by_id.items() if k.startswith("memory.detail.")} == {
        f"memory.detail.{key}": value
        for key, value in MEMINFO_DETAIL_GOLDEN_MB.items()
    }
    assert set(k for k in by_id if k.startswith("memory.detail.")) == {
        f"memory.detail.{k}" for k in ("native_mb", "dalvik_mb", "gfx_mb", "gl_mb")
    }
    assert all(r.ts_ms == 500 for r in records)
    assert all(r.tags["unit"] == "MB" for r in records)
    assert all(r.tags["collector"] == "sampler.mem_detail" for r in records)
    # 2026-11-10：三新 ID 恒出点（golden 语料无 swap 行、proc 应答为空 → null 点）
    assert by_id[METRIC_SWAP_MB] is None
    assert by_id[METRIC_VSS_MB] is None
    assert by_id[METRIC_AVAILABLE_MB] is None
    # metric_ids 全集含三新 ID；出点键 ⊆ 全集（明细缺失项不出点）
    assert {METRIC_SWAP_MB, METRIC_VSS_MB, METRIC_AVAILABLE_MB} <= set(sampler.metric_ids)
    assert set(by_id) <= set(sampler.metric_ids)


def test_mem_detail_sampler_real_sample_three_values() -> None:
    """真机样本端到端：swap 91.451 / vss 19992.523（/proc VmSize 兜底）/
    available 4688.047（/proc MemAvailable）；明细节点缺失项（gfx/gl）不出点。"""
    sampler = MemDetailSampler(interval_ms=2000)
    # pd_ 应答由真机 /proc 样本经 parse_proc_meminfo_available_mb 还原
    # （echo 行即真机 shell 的 grep 输出形态），保持 fixture 为单一事实源
    proc_avail_kb = int(parse_proc_meminfo_available_mb(_golden("proc_meminfo_smf9560.txt")) * 1024)
    proc_stdout = (
        f"pd_memavail=MemAvailable:    {proc_avail_kb} kB\n"
        "pd_vss=VmSize:\t20472344 kB\n"
    )

    def runner(command: list[str], timeout: float | None):
        joined = " ".join(command)
        if "pd_memavail" in joined:
            return SimpleNamespace(stdout=proc_stdout, returncode=0, stderr="")
        assert "dumpsys" in joined and "meminfo" in joined
        return SimpleNamespace(stdout=_golden("meminfo_smf9560.txt"), returncode=0, stderr="")

    records = sampler.sample(_make_ctx(runner))
    by_id = {r.metric_id: r.value for r in records}
    assert by_id[METRIC_SWAP_MB] == 91.451
    assert by_id[METRIC_VSS_MB] == 19992.523
    assert by_id[METRIC_AVAILABLE_MB] == 4688.047
    assert by_id["memory.detail.native_mb"] == 0.816
    assert "memory.detail.gfx_mb" not in by_id  # 缺行不出点（单项缺失语义不变）


def test_mem_detail_sampler_missing_lines_emit_null_points() -> None:
    """缺行出 null 点（value=None，append_null_record 同款语义）：无 swap 行 +
    proc 双节点缺失 → 三 ID 全 null，不填 0 不伪造。"""
    sampler = MemDetailSampler(interval_ms=2000)
    ctx = _make_ctx(
        _runner_for(MEMINFO_DETAIL_PARTIAL_FIXTURE, "pd_memavail=\npd_vss=\n")
    )
    records = sampler.sample(ctx)
    by_id = {r.metric_id: r.value for r in records}
    assert by_id == {
        "memory.detail.native_mb": 8.0,
        "memory.detail.dalvik_mb": 6.5,
        METRIC_SWAP_MB: None,
        METRIC_VSS_MB: None,
        METRIC_AVAILABLE_MB: None,
    }
    assert all(r.tags["unit"] == "MB" and r.tags["collector"] == "sampler.mem_detail"
               for r in records if r.value is None)


def test_mem_detail_sampler_dumpsys_vss_preferred_over_proc() -> None:
    """OEM dumpsys VSS 行在场时优先于 /proc VmSize（机型行名差异两级来源）。"""
    oem_dumpsys = MEMINFO_DETAIL_PARTIAL_FIXTURE.replace(
        "       TOTAL    14,848   14,480        0",
        "       TOTAL    14,848   14,480        0        VSS 1,024 kB",
    )
    sampler = MemDetailSampler(interval_ms=2000)

    def runner(command: list[str], timeout: float | None):
        joined = " ".join(command)
        if "pd_memavail" in joined:
            return SimpleNamespace(stdout="pd_vss=VmSize:\t20472344 kB\n", returncode=0, stderr="")
        return SimpleNamespace(stdout=oem_dumpsys, returncode=0, stderr="")

    by_id = {r.metric_id: r.value for r in sampler.sample(_make_ctx(runner))}
    assert by_id[METRIC_VSS_MB] == 1.0  # dumpsys VSS 行优先，非 /proc 的 19992.523


def test_mem_detail_sampler_partial_emits_available_only() -> None:
    """部分机型：无 GL/Graphics → 只出可解析项（明细四项缺行不出点语义不变）。"""
    sampler = MemDetailSampler(interval_ms=2000)
    ctx = _make_ctx(
        _runner_for(MEMINFO_DETAIL_PARTIAL_FIXTURE, "pd_memavail=\npd_vss=\n")
    )
    records = [r for r in sampler.sample(ctx) if r.metric_id.startswith("memory.detail.")]
    by_id = {r.metric_id: r.value for r in records}
    assert by_id == {"memory.detail.native_mb": 8.0, "memory.detail.dalvik_mb": 6.5}
    # 连续失败（整组缺失）累计诊断——恢复即清零
    sampler2 = MemDetailSampler(interval_ms=2000)

    def fail_runner(command: list[str], timeout: float | None):
        return SimpleNamespace(stdout="No process found\n", returncode=0, stderr="")

    ctx2 = DeviceContext(
        adb=_Adb(), command_runner=fail_runner, serial="S1",
        package_name="com.example.synthetic.game", elapsed_ms=lambda: 0,
    )
    assert sampler2.sample(ctx2) == []
    assert sampler2.sample(ctx2) == []
    assert any("detail" in d for d in sampler2.drain_diagnostics())


# ---- 2026-11-04 体检修复回归（mem-1：EGL mtrack 漏采）----


def test_parse_meminfo_detail_real_aosp_gfx_pools() -> None:
    """mem-1 修复：AOSP 6.0–16 及 main 的明细段从不打印 "Graphics" 行——图形三行
    恒为 Gfx dev/EGL mtrack/GL mtrack。EGL mtrack（gralloc/图形缓冲，第二图形池）
    计入 gfx_mb，不再被静默丢弃（旧实现 gfx 只等于 Gfx dev 单项，lower_better
    指标被低估=画面偏好看，且组仍 detail_complete 门禁放行）。"""
    text = """** MEMINFO in pid 12345 [com.example.game] **
                 Total    Dirty    Clean
  Native Heap    46,880   46,000        0
  Dalvik Heap    25,344   24,980        0
 Dalvik Other     4,096    4,080        0
      Gfx dev     8,192    8,120        0
   EGL mtrack    12,400   12,360        0
    GL mtrack     6,144    6,000        0
"""
    detail = parse_meminfo_detail(text)
    assert detail["native_mb"] == 45.781  # 46,880 KB
    assert detail["gfx_mb"] == 20.109     # 8,192 + 12,400 KB（旧实现 = 8.0）
    assert detail["gl_mb"] == 6.0
    assert detail_complete(detail) is True


def test_parse_meminfo_detail_graphics_alias_still_honored() -> None:
    """graphics 兼容别名保留：OEM 若在明细段打印 "Graphics" 行则并入 gfx_mb
    （真实 AOSP 输出无此行，不会双计——App Summary 的 Graphics 行在解析域外）。"""
    text = """** MEMINFO in pid 12346 [com.example.oem] **
                 Total    Dirty    Clean
  Native Heap     8,192    8,000        0
  Dalvik Heap     6,144    6,000        0
      Gfx dev     4,096    4,000        0
      Graphics    1,024    1,000        0
"""
    detail = parse_meminfo_detail(text)
    assert detail["gfx_mb"] == 5.0  # 4,096 + 1,024 KB
