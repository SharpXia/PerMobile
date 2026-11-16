"""指标注册表（契约 §5 + §20/§21 M6 扩展）。

M1 既有 11 项口径与 perfeval samplers/ 实测一致（ARCH §5 评审修订）：
  cpu.total.pct / cpu.proc.pct   （cpu_sampler.py:13-14）
  memory.pss.peak_mb             （mem_sampler.py:10）
  net.rx.kbps / net.tx.kbps      （net_sampler.py:10-11）
  power.current.ma / power.level.pct / power.energy.mah（power_sampler.py:11-13）
  gpu.busy.pct / thermal.max.c   （gpu_thermal_sampler.py:10-11）
frame.fps.mean 为 M1 轮询通道（ARCH §9.3）；real 侧 gfxinfo 周期 reset+轮询于
2026-11-02 落地（services/fps_sampler.py），synthetic/real 均可用。

M6（契约 §20.1/§21.2/§21.3，§11.1 不升版本）：
- 每指标新增可选 ``capabilities``（modes/platform/reason）与 ``description``；
- 注册表新增 7 项：cpu.freq.mhz / cpu.proc.normalized.pct / cpu.proc.thread.pct
  （R16）、memory.detail.{native,dalvik,gfx,gl}_mb（R17，Memory Detail 勾选组）；
- ``registry_body`` 接受可选设备 profile，available 判定 = 模式可用性 ∩ 设备能力
  （交集判定模型见 capability_matrix.py，契约 §20.3）；
- 全部条目透出 ``defaultIntervalMs``（§20.1/§21.2 golden 形态；§11.1 兼容新增）。

M-iOS（契约 §23，2026-11-03，§11.1 不升版本）：
- ``simulator`` 作为第三 provider 模式（connection/modes 新增枚举值）；
- 注册表新增 2 项 iOS Simulator 指标（platform="ios"、modes=("simulator",)）：
  ``cpu.proc.host.pct`` / ``memory.rss.mb``——**模拟器宿主口径**（iOS 应用进程在
  macOS 宿主以宿主进程运行，ps 的是宿主进程；数值与真机 iOS Xcode/PerfDog 口径
  不可直接对比，description 携带口径声明）；
- FPS：模拟器无逐帧公开 API → frame.fps.mean 对 ios 平台按平台不匹配判
  platform_unsupported（不出点不造数，§23.2 note 声明）。

2026-11-10 扩展（契约 §21.2/§21.3 delta，§11.1 不升版本）：
- ``gpu.freq.mhz``（L2，real-only，gpu_source_required）——kgsl 频率序列
  （clock_mhz 主源、devfreq/cur_freq 降源 Hz→MHz 归一，SM_F9560 实测可读）；
- ``thermal.cpu.c`` / ``thermal.gpu.c`` / ``thermal.btemp.c``——温度分量序列
  （每分量取该类 zone 的 max；mType 0/1/2 枚举，sysfs 降级按名字归类）；
- ``memory.swap.mb`` / ``memory.vss.mb`` / ``memory.available.mb``——Swap PSS /
  VSS / 设备级可用内存（swap/vss 挂 ``meminfo_detail_required`` 整组门禁，
  available 为设备级不挂门禁；三项 synthetic 无建模 → synthetic_available=False）。

R24 真机切片（契约 §26，2026-11-16，§11.1 不升版本）：
- 注册表新增 2 项 iOS 真机指标（platform="ios"、modes=("usb","wifi")、
  real_available=True / simulator_available=False / synthetic_available=False）：
  ``cpu.proc.xctrace.pct`` / ``memory.footprint.mb``——**真机 Instruments 口径**
  （主通道 xcrun xctrace Activity Monitor 模板 + devicectl 编排，勘察本机实测
  xcrun 72 / xctrace 27.2，4s×2 trace 成功、export 抽到 %CPU 10ms 粒度与
  29.89 MiB footprint 真值行）；
- ``_BY_ID`` 按 metricId 唯一（registry.py 底部映射）→ 必须新增 id，**不复用**
  ``cpu.proc.pct`` 行（该行 platform="android"，真机 iOS 设备按 §20.3 判定第 1 条
  需要 platform="ios" 的独立行）；
- 既有 simulator 2 行（§23.2：cpu.proc.host.pct / memory.rss.mb）逐字段不动；
- §20.1 reason 码**不扩充**：真机 iOS 的可用性判定全部落在既有枚举——
  Android 指标对 ios 设备 = platform_unsupported（判定第 1 条）、模式交集空/
  通道未落地 = not_landed（判定第 2 条/第 0 项）；``xctrace_unavailable`` 是
  §3 HTTP 错误码（503，采集执行面），不进 §20.1 reason 枚举。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ApiError

# reason 码枚举（契约 §20.1 表；人工文案由前端映射表渲染）
REASON_KGSL_MISSING = "kgsl_missing"
REASON_POWER_WIFI_ONLY = "power_wifi_only"
REASON_NOT_LANDED = "not_landed"
REASON_PLATFORM_UNSUPPORTED = "platform_unsupported"
# 实机验证新增（2026-11-03，契约 §20.1 枚举走 §11.1 扩展）：power_supply sysfs 被
# 厂商 SELinux 策略屏蔽（SM_F9560 实测 `/sys/class/power_supply/` Permission denied），
# power.* 三指标在该设备实际无数据源——探测不可读时置此 reason（能力缺失走
# available=false，不静默出空图）。
REASON_POWER_SYSFS_BLOCKED = "power_sysfs_blocked"
REASON_CODES = (
    REASON_KGSL_MISSING,
    REASON_POWER_WIFI_ONLY,
    REASON_NOT_LANDED,
    REASON_PLATFORM_UNSUPPORTED,
    REASON_POWER_SYSFS_BLOCKED,
)

# 内存 Detail 勾选组（契约 §21.3；官方损耗提示文案进 description，[F§3.8][I§2.4]）
MEMORY_DETAIL_GROUP = (
    "memory.detail.native_mb",
    "memory.detail.dalvik_mb",
    "memory.detail.gfx_mb",
    "memory.detail.gl_mb",
)

# 功耗勾选组（perfeval power_sampler 数据源 = /sys/class/power_supply/battery/*；
# 采样器无兜底源，sysfs 不可读即整组无数据）
POWER_GROUP = (
    "power.current.ma",
    "power.level.pct",
    "power.energy.mah",
)

_MEMORY_DETAIL_DESCRIPTION = (
    "官方提示：极限测试（超高帧率）建议不勾选 Memory Detail，"
    "部分低端机型有性能损耗（[F§3.8][I§2.4]）"
)

_CPU_NORMALIZED_DESCRIPTION = (
    "规范化=未规范化×(Σ当前频率/Σ最大频率)（[F§3.6]），官方建议默认展示规范化值；"
    "与 cpu.proc.pct 未规范化口径不可直接对比"
)


@dataclass(frozen=True)
class MetricSpec:
    """注册表条目：§5 既有字段 + M6 ``modes``/``platform``/``description``。

    - ``gpu_source_required``：硬性资源依赖（§20.3 判定第 3 条）——gpu.* 要求设备
      ``profile.gpuSource`` 非空（"kgsl"），否则 available=false + kgsl_missing；
    - ``meminfo_detail_required``：dumpsys meminfo 明细段硬性依赖（§21.3，2026-11-16
      修订：明细段**通道存在**——native/dalvik 任一可解析——即整组可勾；单项行缺失
      由采样层逐项不出点；明细段整体缺失 reason=not_landed）；
    - ``simulator_available``：M-iOS（契约 §23）——simulator 模式（iOS Simulator）
      下采集通道是否落地；既有 Android 指标恒 False（simulator 模式只服务 ios
      平台设备，platform 判定先行挡下）。
    """

    metric_id: str
    unit: str
    direction: str
    evidence_level: str
    default_interval_ms: int
    synthetic_available: bool
    real_available: bool
    modes: tuple[str, ...] = ("usb", "wifi")
    platform: str = "android"
    description: str | None = None
    gpu_source_required: bool = False
    meminfo_detail_required: bool = False
    simulator_available: bool = False
    tags_hint: tuple[str, ...] = field(default=())


METRICS: tuple[MetricSpec, ...] = (
    # 默认间隔对齐契约 §5 缺省表（单一事实源）：cpu.*/net.*/power.*=1000；
    # memory.*/gpu.busy.pct/thermal.max.c=2000；frame.fps.mean=500
    MetricSpec("cpu.total.pct", "%", "lower_better", "L1", 1000, True, True),
    MetricSpec("cpu.proc.pct", "%", "lower_better", "L1", 1000, True, True),
    MetricSpec("memory.pss.peak_mb", "MB", "lower_better", "L1", 2000, True, True),
    MetricSpec("net.rx.kbps", "kbps", "none", "L1", 1000, True, True),
    MetricSpec("net.tx.kbps", "kbps", "none", "L1", 1000, True, True),
    MetricSpec("power.current.ma", "mA", "none", "L1", 1000, True, True),
    # power-3 修复（2026-11-06 体检）：real 通道该指标仅在降级路径产出，值为
    # 电量差分速率（%/min，越高掉电越快）而非电量百分比——unit/direction 按
    # 实际口径登记（旧 "%"+higher_better 使前端值卡渲染成「↑ 越高越好」误导）
    MetricSpec("power.level.pct", "%/min", "lower_better", "L1", 1000, True, True),
    MetricSpec("power.energy.mah", "mAh", "lower_better", "L2", 1000, True, True),
    MetricSpec(
        "gpu.busy.pct", "%", "lower_better", "L2", 2000, False, True,
        gpu_source_required=True,
    ),
    MetricSpec(
        "gpu.freq.mhz", "MHz", "none", "L2", 2000, False, True,
        gpu_source_required=True,
        description=(
            "GPU Clock：kgsl 当前频率（clock_mhz 主源，devfreq/cur_freq 降源 Hz→MHz 归一，"
            "SM_F9560 实测可读）；观察 GPU 降频"
        ),
    ),
    MetricSpec("thermal.max.c", "°C", "lower_better", "L1", 2000, True, True),
    # ---- 2026-11-10 扩展（gap thermal-component-split）：温度分量序列 ----
    # 分量 = 该类 zone 的 max（mType 0/1/2 枚举，sysfs 降级按名字归类）；某分量无
    # zone 不出点（unavailable，不填 0）。NPU 无对应枚举（SM_F9560/模拟器实测均无
    # NTemp zone），不注册行。
    MetricSpec("thermal.cpu.c", "°C", "lower_better", "L1", 2000, True, True),
    MetricSpec("thermal.gpu.c", "°C", "lower_better", "L1", 2000, True, True),
    MetricSpec("thermal.btemp.c", "°C", "lower_better", "L1", 2000, True, True),
    # frame.fps.mean：M1 新增轮询通道（ARCH §9.3）；real 于 2026-11-02 落地
    #（gfxinfo 周期 reset+轮询，services/fps_sampler.py），synthetic/real 均可用
    MetricSpec("frame.fps.mean", "fps", "higher_better", "L1", 500, True, True),
    # ---- M6（契约 §21.2）：CPU 高阶三指标（R16，capabilities 见 capability_matrix）----
    MetricSpec(
        "cpu.freq.mhz", "MHz", "none", "L1", 2000, True, True,
        tags_hint=("core",),
    ),
    MetricSpec(
        "cpu.proc.normalized.pct", "%", "lower_better", "L1", 1000, True, True,
        description=_CPU_NORMALIZED_DESCRIPTION,
    ),
    MetricSpec(
        "cpu.proc.thread.pct", "%", "lower_better", "L1", 2000, True, True,
        tags_hint=("thread",),
    ),
    # ---- M6（契约 §21.3）：内存 Detail 勾选组（R17；明细段硬性依赖整组判定）----
    MetricSpec(
        "memory.detail.native_mb", "MB", "lower_better", "L1", 2000, True, True,
        description=_MEMORY_DETAIL_DESCRIPTION, meminfo_detail_required=True,
    ),
    MetricSpec(
        "memory.detail.dalvik_mb", "MB", "lower_better", "L1", 2000, True, True,
        description=_MEMORY_DETAIL_DESCRIPTION, meminfo_detail_required=True,
    ),
    MetricSpec(
        "memory.detail.gfx_mb", "MB", "lower_better", "L1", 2000, True, True,
        description=_MEMORY_DETAIL_DESCRIPTION, meminfo_detail_required=True,
    ),
    MetricSpec(
        "memory.detail.gl_mb", "MB", "lower_better", "L1", 2000, True, True,
        description=_MEMORY_DETAIL_DESCRIPTION, meminfo_detail_required=True,
    ),
    # ---- 2026-11-10 扩展（gap memory-swap-vss-available）：内存三指标 ----
    # swap/vss 与明细段同源（dumpsys meminfo 输出 + /proc/<pid>/status VmSize），
    # 挂 meminfo_detail_required 整组门禁（available=false + not_landed，同 §21.3）；
    # available 为设备级（/proc/meminfo MemAvailable），不挂进程明细段门禁。
    # 三项 synthetic 无建模 → synthetic_available=False（§5 available=false 规则同
    # gpu.busy.pct 先例：UI 显示但禁勾，不写 0）。
    MetricSpec(
        "memory.swap.mb", "MB", "lower_better", "L1", 2000, False, True,
        meminfo_detail_required=True,
        description=(
            "Swap PSS：dumpsys meminfo App Summary 的 TOTAL SWAP PSS（机型缺该行时无数据，不出点）"
        ),
    ),
    MetricSpec(
        "memory.vss.mb", "MB", "lower_better", "L1", 2000, False, True,
        meminfo_detail_required=True,
        description=(
            "VSS：/proc/<pid>/status 的 VmSize（SM_F9560 实测 dumpsys meminfo 明细表无 VSS 列）"
        ),
    ),
    MetricSpec(
        "memory.available.mb", "MB", "lower_better", "L1", 2000, False, True,
        description=(
            "设备级可用内存：/proc/meminfo 的 MemAvailable（非进程指标，SM_F9560 实测可读）"
        ),
    ),
    # ---- M-iOS（契约 §23.2）：iOS Simulator 宿主口径双指标 ----
    # CPU/内存均采自 macOS 宿主进程表（模拟器内 iOS 应用 = 宿主进程），
    # 数值与真机 iOS（Xcode/PerfDog 口径）不可直接对比——description 携带口径声明。
    MetricSpec(
        "cpu.proc.host.pct", "%", "lower_better", "L1", 1000, False, False,
        modes=("simulator",), platform="ios",
        simulator_available=True,
        description=(
            "模拟器宿主口径：被测 App 进程在本机（macOS）的 CPU 占用率"
            "（ps %CPU，衰减均值，多核可 >100%）；与真机 iOS（Xcode 使用率/核数、"
            "PerfDog 未规范化口径）不可直接对比（[F§3.6] 平台口径差异）"
        ),
    ),
    MetricSpec(
        "memory.rss.mb", "MB", "lower_better", "L1", 2000, False, False,
        modes=("simulator",), platform="ios",
        simulator_available=True,
        description=(
            "模拟器宿主口径：被测 App 进程在本机（macOS）的常驻内存 RSS；"
            "与 Android PSS（memory.pss.peak_mb）及真机 iOS FootPrint "
            "（[F§3.8]）口径均不同，数值不可直接对比"
        ),
    ),
    # ---- R24 真机切片（契约 §26.3，2026-11-16，§11.1 不升版本）----
    # 真机 iOS 双指标：主通道 = xcrun xctrace（Activity Monitor 模板为主，
    # Time Profiler 备细粒度）+ devicectl 编排（预热/lockState → （可选 process
    # launch）→ record --attach <进程名> --time-limit Ns --no-prompt → export
    # --toc/--xpath → 解析 activity-monitor-process-live 出 CPU% + physical
    # footprint ~1s 双序列）。窗口批采样语义/锚点与诊断见契约 §26.4/§26.5。
    # metricId 按 _BY_ID 唯一——不复用 cpu.proc.pct 行（platform 判定不同）。
    MetricSpec(
        "cpu.proc.xctrace.pct", "%", "lower_better", "L1", 1000, False, True,
        modes=("usb", "wifi"), platform="ios",
        description=(
            "真机 Instruments 口径：xctrace（Activity Monitor 模板，--attach 进程名）"
            "%CPU 序列（export 实测 10ms 粒度，聚合到采样窗出点）；与模拟器宿主口径"
            "（cpu.proc.host.pct）及 Android 未规范化口径（cpu.proc.pct）均不可直接"
            "对比（[F§3.6] 平台口径差异）"
        ),
    ),
    MetricSpec(
        "memory.footprint.mb", "MB", "lower_better", "L1", 2000, False, True,
        modes=("usb", "wifi"), platform="ios",
        description=(
            "真机 Instruments 口径：xctrace Activity Monitor 的 physical FootPrint "
            "序列；与 Android PSS（memory.pss.peak_mb）及模拟器 RSS（memory.rss.mb）"
            "口径均不同，数值不可直接对比"
        ),
    ),
)

_BY_ID: dict[str, MetricSpec] = {spec.metric_id: spec for spec in METRICS}


def get_metric(metric_id: str) -> MetricSpec:
    spec = _BY_ID.get(metric_id)
    if spec is None:
        raise ApiError(
            "validation_error",
            f"未注册的 metricId: {metric_id}",
            details={"metricId": metric_id},
        )
    return spec


def is_registered(metric_id: str) -> bool:
    return metric_id in _BY_ID


def mode_available(spec: MetricSpec, mode: str) -> bool:
    """模式维度可用性（交集判定的第 0 项：采集通道在本模式是否落地）。

    mode ∈ real | synthetic | simulator（M-iOS 契约 §23：simulator 模式仅服务
    iOS Simulator 设备，通道由 ``simulator_available`` 承载）。
    """
    if mode == "real":
        return spec.real_available
    if mode == "simulator":
        return spec.simulator_available
    return spec.synthetic_available


def available_metric_ids(mode: str) -> list[str]:
    """mode ∈ real | synthetic；available=false 指标 UI 显示但禁勾（契约 §5）。

    M6 注：本函数保持「无设备上下文」的模式级口径（会话缺省指标集沿用）；
    带设备的交集判定走 registry_body + capability_matrix。
    """
    return [spec.metric_id for spec in METRICS if mode_available(spec, mode)]


def registry_body(mode: str, profile: "dict | None" = None) -> dict:
    """契约 §5 注册表 + §20.1/§21.2/§21.3 M6 扩展。

    ``profile``（可选，capability_matrix.device_profile 形状）：提供时每指标
    available = 模式可用性 ∩ 设备能力交集判定（capability_matrix.judge），
    不可用且原因可判 → capabilities 附机器可读 ``reason``；未提供时 available
    仅按模式判定（无设备上下文，不出 reason）。
    """
    from .services.capability_matrix import judge_metric

    metrics = []
    for spec in METRICS:
        available = mode_available(spec, mode)
        reason: str | None = None
        if profile is not None:
            available, reason = judge_metric(spec, mode, profile)
            # meminfo_detail_required 整组门禁（§21.3 同款）：judge_metric 按
            # MEMORY_DETAIL_GROUP 成员判定；2026-11-10 扩展行（memory.swap.mb/
            # memory.vss.mb）以 spec.meminfo_detail_required 标记挂同一门禁——
            # 明细段探测缺失/失败 → available=false + not_landed。reason 优先级
            # 与 §20.3 判定一致：platform_unsupported（判定第 1 条）优先，不覆写。
            if (
                spec.meminfo_detail_required
                and profile.get("meminfoDetail") is not True
                and reason != REASON_PLATFORM_UNSUPPORTED
            ):
                available = False
                reason = REASON_NOT_LANDED
        entry: dict = {
            "metricId": spec.metric_id,
            "unit": spec.unit,
            "direction": spec.direction,
            "evidenceLevel": spec.evidence_level,
            "defaultIntervalMs": spec.default_interval_ms,
            "available": available,
        }
        if spec.description is not None:
            entry["description"] = spec.description
        capabilities: dict = {"modes": list(spec.modes), "platform": spec.platform}
        if not available and reason is not None:
            capabilities["reason"] = reason
        entry["capabilities"] = capabilities
        metrics.append(entry)
    return {"mode": mode, "metrics": metrics}
