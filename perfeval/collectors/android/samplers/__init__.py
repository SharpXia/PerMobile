"""Android 连续采样器框架（CM-001 设计 §6）。

本包是采样器注册表：build_samplers(config) 依据 scenario YAML
`collectors.android.samplers` 构建采样器实例；未注册的采样器名忽略，
enabled=false 显式关闭，enabled="auto"（缺省）交由 probe() 在 run 开始时判定。
entry 中的其余配置键（如 cpu 的 `perCore: true`）按 camelCase→snake_case 转换后
透传给工厂签名中声明的同名参数；工厂未声明的键忽略（向后兼容）。
"""

import inspect
from collections.abc import Callable, Mapping
from typing import Any

from .base import DeviceContext, ProbeResult, SampleRecord, Sampler, SamplingScheduler
from .cpu_sampler import CpuSampler
from .gpu_thermal_sampler import GpuThermalSampler
from .mem_sampler import MemSampler
from .net_sampler import NetSampler
from .power_sampler import PowerSampler

# 采样器工厂注册表：name -> factory(**kwargs) -> Sampler。
# 工厂以关键字参数声明可配置项（interval_ms 及各自的选项参数），
# build_samplers 依据签名透传 scenario entry 配置键。
SAMPLER_FACTORIES: dict[str, Callable[..., Sampler]] = {}


def register_sampler(name: str, factory: Callable[..., Sampler]) -> None:
    SAMPLER_FACTORIES[name] = factory


register_sampler(
    "mem",
    lambda *, interval_ms=2000: MemSampler(interval_ms=interval_ms),
)
register_sampler(
    "cpu",
    lambda *, interval_ms=1000, include_per_core=False: CpuSampler(
        interval_ms=interval_ms, include_per_core=include_per_core
    ),
)
register_sampler(
    "net",
    lambda *, interval_ms=1000: NetSampler(interval_ms=interval_ms),
)
register_sampler(
    "power",
    lambda *, interval_ms=1000: PowerSampler(interval_ms=interval_ms),
)
register_sampler(
    "gpu_thermal",
    lambda *, interval_ms=1000: GpuThermalSampler(interval_ms=interval_ms),
)


def build_samplers(config: Mapping[str, Any] | None) -> list[Sampler]:
    """按配置构建采样器列表；未知名称忽略，enabled=false 跳过。

    intervalMs 未指定时使用采样器自身默认值（如 meminfo 采样默认 2s，设计 §5），
    不以全局默认覆盖；entry 其余配置键（如 `perCore`）按工厂签名透传。
    """
    samplers: list[Sampler] = []
    if not isinstance(config, Mapping):
        return samplers
    for name, entry in config.items():
        factory = SAMPLER_FACTORIES.get(str(name))
        if factory is None:
            continue
        enabled, interval_ms, extra_kwargs = _resolve_entry(entry)
        if not enabled:
            continue
        kwargs: dict[str, Any] = {}
        if interval_ms is not None:
            kwargs["interval_ms"] = interval_ms
        kwargs.update(_factory_kwargs(factory, extra_kwargs))
        samplers.append(factory(**kwargs))
    return samplers


def samplers_from_config(
    config: Mapping[str, Any] | None,
    *,
    sampling_off: bool = False,
) -> list[Sampler]:
    """--sampling-off 时的短路入口：直接返回空列表。"""
    if sampling_off:
        return []
    return build_samplers(config)


def _camel_to_snake(name: str) -> str:
    return "".join(
        f"_{char.lower()}" if char.isupper() else char for char in name
    ).lstrip("_")


# scenario entry 配置键 → 工厂参数名别名（未列出的键按 camelCase→snake_case 匹配）
_ENTRY_KEY_ALIASES: dict[str, str] = {"perCore": "include_per_core"}


def _factory_param_key(key: str) -> str:
    if key in _ENTRY_KEY_ALIASES:
        return _ENTRY_KEY_ALIASES[key]
    return _camel_to_snake(key)


def _resolve_entry(entry: Any) -> tuple[bool, int | None, dict[str, Any]]:
    """解析 scenario entry：返回 (enabled, intervalMs, 其余待透传配置键)。"""
    if entry is None or entry is True:
        return True, None, {}
    if entry is False:
        return False, None, {}
    if isinstance(entry, Mapping):
        enabled = entry.get("enabled", "auto")
        raw_interval = entry.get("intervalMs")
        interval_ms: int | None = None
        if isinstance(raw_interval, (int, float)) and not isinstance(raw_interval, bool) and raw_interval > 0:
            interval_ms = int(raw_interval)
        extra = {
            _factory_param_key(str(key)): value
            for key, value in entry.items()
            if key not in ("enabled", "intervalMs")
        }
        if enabled is False or enabled == "false":
            return False, interval_ms, extra
        return True, interval_ms, extra
    return True, None, {}


def _factory_kwargs(factory: Callable[..., Sampler], extra: Mapping[str, Any]) -> dict[str, Any]:
    """按工厂签名过滤透传键：未声明的配置键忽略（现网 YAML 行为不变）。"""
    try:
        parameters = inspect.signature(factory).parameters
    except (TypeError, ValueError):
        return {}
    accepts_any = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )
    if accepts_any:
        return dict(extra)
    return {key: value for key, value in extra.items() if key in parameters}


__all__ = [
    "CpuSampler",
    "DeviceContext",
    "GpuThermalSampler",
    "MemSampler",
    "NetSampler",
    "PowerSampler",
    "ProbeResult",
    "SampleRecord",
    "Sampler",
    "SamplingScheduler",
    "SAMPLER_FACTORIES",
    "build_samplers",
    "register_sampler",
    "samplers_from_config",
]
