from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

from ..adb import AdbClient

# 与 runner.CommandRunner 同签名；本地定义避免 runner <-> samplers 循环导入
CommandRunner = Callable[[list[str], float | None], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class SampleRecord:
    """单条采样记录，落盘契约见 samples.ndjson：{"tsMs","metricId","value","tags"}。"""

    metric_id: str
    value: float
    ts_ms: int = 0
    tags: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tsMs": self.ts_ms,
            "metricId": self.metric_id,
            "value": self.value,
            "tags": dict(self.tags),
        }


@dataclass(frozen=True)
class ProbeResult:
    """capability 探测结果（只读命令），不可用时给出可读原因。"""

    available: bool
    reason: str | None = None

    @classmethod
    def ok(cls) -> "ProbeResult":
        return cls(available=True)

    @classmethod
    def unavailable(cls, reason: str) -> "ProbeResult":
        return cls(available=False, reason=reason)


@dataclass(frozen=True)
class DeviceContext:
    """采样器运行上下文：复用可注入 command_runner，elapsed_ms 返回相对调度起点的毫秒数。"""

    adb: AdbClient
    command_runner: CommandRunner
    serial: str | None = None
    package_name: str | None = None
    elapsed_ms: Callable[[], int] = lambda: 0


@runtime_checkable
class Sampler(Protocol):
    """连续采样器协议（CM-001 设计 §6）。

    name 用于 diagnostic 命名（sampler.<name>.failed / sampler.<name>.unavailable）；
    interval_ms 为可选属性，缺省时使用调度器默认间隔。
    """

    name: str
    metric_ids: tuple[str, ...]

    def probe(self, ctx: DeviceContext) -> ProbeResult: ...

    def start(self, ctx: DeviceContext) -> None: ...

    def sample(self, ctx: DeviceContext) -> list[SampleRecord]: ...

    def stop(self) -> list[SampleRecord]: ...


class SamplingScheduler:
    """单线程采样调度器：按 interval_ms 轮询全部 sampler，单 sampler 抛错只记 diagnostic 并继续。

    生命周期与 runner 对齐：probe() 在 run 开始时探测 -> start() 在 launch 前调用 ->
    stop() 在 collect 阶段第一个命令前调用（刷入 stop 读数）。
    """

    def __init__(
        self,
        samplers: Sequence[Sampler],
        *,
        context: DeviceContext,
        interval_ms: int = 1000,
        on_record: Callable[[SampleRecord], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        join_timeout_s: float = 5.0,
    ) -> None:
        self._all_samplers = list(samplers)
        self._active: list[Sampler] = list(samplers)
        self.interval_ms = max(1, int(interval_ms))
        self._on_record = on_record
        self._clock = clock
        self._join_timeout_s = max(0.0, float(join_timeout_s))
        self._diagnostics: list[str] = []
        self._next_due: dict[str, float] = {}
        self._started_at: float | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._stopped = False
        # 调度器统一提供 elapsed_ms（相对 start() 的毫秒数），采样器据此为记录打 ts
        self._context = replace(context, elapsed_ms=self._elapsed_ms)

    @property
    def diagnostics(self) -> list[str]:
        return list(self._diagnostics)

    def drain_diagnostics(self) -> list[str]:
        """取走并清空已累计的 diagnostic（runner 在生命周期节点接走，避免重复）。"""
        drained = list(self._diagnostics)
        self._diagnostics.clear()
        return drained

    @property
    def samplers(self) -> list[Sampler]:
        """全部注册的采样器（含 probe/start 阶段被剔除的），供 runner 统一 drain 内部诊断。"""
        return list(self._all_samplers)

    @property
    def active_names(self) -> list[str]:
        return [sampler.name for sampler in self._active]

    def _elapsed_ms(self) -> int:
        if self._started_at is None:
            return 0
        return int((self._clock() - self._started_at) * 1000)

    def probe(self) -> None:
        """capability 探测：不可用或探测抛错的 sampler 记 diagnostic 并从调度列表剔除。

        available=True 且携带 reason（子项降级，如 gpu_thermal 无 kgsl 但温度可用、
        net 降级整机 netstats）→ 记 ``sampler.<name>.degraded:<reason>``——gpu-3
        修复（2026-11-04 体检）：此前该 reason 无任何消费点，子源静默关停后报告
        层只能兜底「无采样数据」，降级原因对用户不可见；落盘后报告 chip 的
        sampler_hint 前缀匹配即可命中原文。
        """
        for sampler in self._all_samplers:
            try:
                result = sampler.probe(self._context)
            except Exception as exc:  # noqa: BLE001 - 单项能力缺失不允许影响其余指标
                self._active = [item for item in self._active if item.name != sampler.name]
                self._diagnostics.append(f"sampler.{sampler.name}.unavailable:{exc}")
                continue
            if not result.available:
                self._active = [item for item in self._active if item.name != sampler.name]
                reason = result.reason or "unknown"
                self._diagnostics.append(f"sampler.{sampler.name}.unavailable:{reason}")
            elif result.reason:
                # 采样器自带 "degraded: " 前缀（gpu_thermal/net/power 的
                # ProbeResult reason）不再叠加——复核意见③：语义已进 code token，
                # 落盘诊断只保留原因正文（probe 响应原样不动）
                reason = result.reason
                if reason.startswith("degraded: "):
                    reason = reason[len("degraded: "):]
                self._diagnostics.append(f"sampler.{sampler.name}.degraded:{reason}")

    def start(self) -> None:
        """记录基线并启动后台轮询线程；start 抛错的 sampler 记 diagnostic 并剔除。"""
        for sampler in list(self._active):
            try:
                sampler.start(self._context)
            except Exception as exc:  # noqa: BLE001 - 异常隔离
                self._active = [item for item in self._active if item.name != sampler.name]
                self._diagnostics.append(f"sampler.{sampler.name}.failed:{exc}")
        self._started_at = self._clock()
        self._stopped = False
        if self._active:
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="perfeval-sampling-scheduler",
                daemon=True,
            )
            self._thread.start()

    def poll_once(self) -> int:
        """执行一轮到期采样（同步，供测试与线程循环复用），返回本轮采样的 sampler 数。"""
        now = self._clock()
        sampled = 0
        for sampler in list(self._active):
            due = self._next_due.get(sampler.name)
            if due is not None and now < due:
                continue
            self._sample_sampler(sampler)
            self._next_due[sampler.name] = now + self._interval_seconds_of(sampler)
            sampled += 1
        return sampled

    def stop(self) -> None:
        """停止轮询并调用各 sampler.stop() 刷入终止读数（同样经异常隔离）；幂等。"""
        if self._stop_event.is_set() and self._thread is None and self._stopped:
            return
        self._stopped = True
        self._stop_event.set()
        if self._thread is not None:
            # join 超时不视为失败：线程在阻塞的 sample() 返回后看到 stop_event 自行退出，
            # 此处仍并发调用 sampler.stop() 刷入终止读数（采样器需自行保证并发安全）。
            self._thread.join(timeout=self._join_timeout_s)
            self._thread = None
        for sampler in list(self._active):
            try:
                self._emit(sampler.stop())
            except Exception as exc:  # noqa: BLE001 - 异常隔离
                self._diagnostics.append(f"sampler.{sampler.name}.failed:{exc}")

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            self.poll_once()
            self._stop_event.wait(self._wait_seconds())

    def _sample_sampler(self, sampler: Sampler) -> None:
        try:
            self._emit(sampler.sample(self._context))
        except Exception as exc:  # noqa: BLE001 - 单 sampler 抛错只记 diagnostic 并继续
            self._diagnostics.append(f"sampler.{sampler.name}.failed:{exc}")

    def _emit(self, records: Sequence[SampleRecord]) -> None:
        if self._on_record is None:
            return
        for record in records:
            self._on_record(record)

    def _interval_seconds_of(self, sampler: Sampler) -> float:
        interval_ms = getattr(sampler, "interval_ms", None)
        if not isinstance(interval_ms, (int, float)) or isinstance(interval_ms, bool) or interval_ms <= 0:
            interval_ms = self.interval_ms
        return max(1, int(interval_ms)) / 1000.0

    def _wait_seconds(self) -> float:
        now = self._clock()
        due_times = [self._next_due.get(sampler.name, now) for sampler in self._active]
        nearest = min(due_times) if due_times else now
        return max(0.0, min(nearest - now, self.interval_ms / 1000.0))
