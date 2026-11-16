"""能力门禁矩阵（M6 R21，契约 §20；架构落点 [P§5.6] CapabilityMatrix）。

勾选面板可用性 = 指标 ``capabilities`` ∩ 设备 ``profile`` 的交集判定（契约 §20.3 硬性）：

1. ``capabilities.platform`` 与设备 ``platform`` 一致；
2. ``capabilities.modes`` 与 ``profile`` 中为 true 的连接模式（usb/wifi）交集非空；
3. 设备资源满足该指标的硬性依赖（``gpu.*`` 要求 ``profile.gpuSource`` 非空；
   ``memory.detail.*`` 要求 dumpsys meminfo 明细段可解析——§21.3「四项全部可用才
   整组可勾」）。

三条全过 → 可勾选；任一不过 → 禁勾，``capabilities.reason`` 机器可读码（§20.1
枚举，人工文案由前端映射表渲染）。服务端 §5 ``available`` 仍是权威判定；本模块是
判定输入的显式计算，`GET /api/metrics?deviceId=` 与 `POST /api/sessions` 落点共用
（避免客户端本地判定与服务端仲裁漂移）。

profile 形状（契约 §20.2， wires 形状仅 5 字段；M-iOS §23.1 扩展 simulator）::

    {"usb": bool, "wifi": bool, "simulator": bool, "gpuSource": "kgsl" | None,
     "cpuCores": int, "refreshRateHz": int}

内部探测数据（不序列化，仅判定输入）：``meminfoDetail``（bool | None，real 模式
dumpsys meminfo 明细段探测结果；None=探测失败按缺失处理，不猜——契约 §20.2
「探测失败置 null（不猜）」同哲学）；``powerSupply``（bool | None，real 模式
``/sys/class/power_supply/battery`` 可读性探测——2026-11-03 实机验证新增：
SM_F9560 被 SELinux 拒绝该目录，power.* 三指标无数据源，置
``reason=power_sysfs_blocked``（契约 §20.1 枚举 §11.1 扩展）而非静默出空图）；
``platform``（"android" | "ios"——M-iOS §23.1 扩展第二个平台值）。
"""

from __future__ import annotations

from typing import Any

from ..registry import (
    MEMORY_DETAIL_GROUP,
    POWER_GROUP,
    REASON_KGSL_MISSING,
    REASON_NOT_LANDED,
    REASON_PLATFORM_UNSUPPORTED,
    REASON_POWER_SYSFS_BLOCKED,
    MetricSpec,
    mode_available,
)

PROFILE_FIELDS = ("usb", "wifi", "simulator", "gpuSource", "cpuCores", "refreshRateHz")

# 连接模式全集（M-iOS §23.1：新增第三枚举值 "simulator"，§11.1 兼容扩展）
CONNECTION_MODES = ("usb", "wifi", "simulator")


def device_profile(
    *,
    usb: bool,
    wifi: bool,
    gpu_source: str | None,
    cpu_cores: int | None,
    refresh_rate_hz: int | None,
    meminfo_detail: bool | None = None,
    power_supply: bool | None = None,
    platform: str = "android",
    simulator: bool | None = None,
) -> dict[str, Any]:
    """构造 profile dict：契约字段 + 内部探测字段（不序列化）。

    探测失败（cpu_cores/refresh_rate_hz 为 None）按 null 落字段（契约 §20.2）；
    gpu_source 非 "kgsl"（含探测失败 None）统一落 None → gpu.busy.pct 判
    kgsl_missing（无 GPU 利用率源 = 不可用，不区分「无此硬件」与「探测失败」的
    对外语义——两者该指标都不可用）。

    M-iOS（§23.1）：``platform`` 支持 "ios"；``simulator`` 为该设备的连接模式
    标志（iOS Simulator 行恒 True、Android 设备恒 False）。``simulator=None``
    时按 platform 推导（ios→True，android→False），兼容既有调用点。
    R24 真机切片（契约 §26，2026-11-16）：真机 iOS 行 ``platform="ios"`` 但
    ``simulator=False``（simulator 是连接模式标志非平台标志，§20.2 修订）——
    **真机 profile 必须显式传 ``simulator=False``**，None 推导（ios→True）仅适用
    于 §23 既有 simulator 调用点。
    """
    if simulator is None:
        simulator = platform == "ios"
    return {
        "platform": platform,  # 内部判定输入（§20.3 第 1 条）；wire 形状不含
        "usb": bool(usb),
        "wifi": bool(wifi),
        "simulator": bool(simulator),  # M-iOS §23.1：第三连接模式标志
        "gpuSource": gpu_source if gpu_source == "kgsl" else None,
        "cpuCores": cpu_cores,
        "refreshRateHz": refresh_rate_hz,
        "meminfoDetail": meminfo_detail,
        "powerSupply": power_supply,
    }


def wire_profile(profile: dict[str, Any] | None) -> dict[str, Any] | None:
    """profile → 契约 §20.2 wire 形状（剥掉内部探测字段；None 原样）。"""
    if profile is None:
        return None
    return {field: profile.get(field) for field in PROFILE_FIELDS}


def judge_metric(
    spec: MetricSpec, mode: str, profile: dict[str, Any]
) -> tuple[bool, str | None]:
    """交集判定（契约 §20.3 三条）：返回 (available, reason | None)。

    reason 仅在 available=false 且原因可判时给出；设备资源依赖的原因
    （kgsl_missing）优先于模式未落地（not_landed）——与 §20.1 golden 示例
    （gpu.busy.pct 在 gpuSource=null 设备上 reason=kgsl_missing）一致。
    """
    # 第 1 条：平台
    if spec.platform != profile.get("platform", "android"):
        return False, REASON_PLATFORM_UNSUPPORTED
    # 第 3 条：设备硬性资源依赖（先判——原因优先级见 docstring）
    if spec.gpu_source_required and profile.get("gpuSource") != "kgsl":
        return False, REASON_KGSL_MISSING
    if spec.metric_id in MEMORY_DETAIL_GROUP and profile.get("meminfoDetail") is not True:
        # None=探测失败 / False=无明细段：同一落点 not_landed（§21.3 契约原文口径）
        return False, REASON_NOT_LANDED
    if spec.metric_id in POWER_GROUP and profile.get("powerSupply") is not True:
        # 2026-11-03 实机验证新增：power_supply sysfs 不可读（厂商 SELinux 屏蔽），
        # power.* 无数据源——探测失败 None 同落此 reason（不猜，不静默出空图）
        return False, REASON_POWER_SYSFS_BLOCKED
    # 第 2 条：连接模式交集（M-iOS §23.1：device_modes 含 simulator 第三枚举值）
    device_modes = {name for name in CONNECTION_MODES if profile.get(name)}
    if not (set(spec.modes) & device_modes):
        return False, REASON_NOT_LANDED
    # 第 0 项：模式可用性（采集通道在本模式是否落地）
    if not mode_available(spec, mode):
        return False, REASON_NOT_LANDED
    return True, None


def metric_available_on_device(
    metric_id: str, mode: str, profile: dict[str, Any] | None
) -> tuple[bool, str | None]:
    """会话落点判定入口：无 profile 时退化为模式级判定（§5 权威判定）。"""
    from ..registry import get_metric, is_registered

    if not is_registered(metric_id):
        return False, None
    spec = get_metric(metric_id)
    if profile is None:
        return mode_available(spec, mode), (
            REASON_NOT_LANDED if not mode_available(spec, mode) else None
        )
    return judge_metric(spec, mode, profile)
