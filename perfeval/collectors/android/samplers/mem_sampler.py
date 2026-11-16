from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from ..meminfo import parse_meminfo
from .base import DeviceContext, ProbeResult, SampleRecord

# 指标 ID 全链路固定（CM-001 §5）：周期采样使 "peak" 兑现序列真实最大值
METRIC_ID = "memory.pss.peak_mb"

# meminfo 周期采样默认间隔（CM-001 设计 §5）
DEFAULT_INTERVAL_MS = 2000

# PSS 读取连续失败达到该次数记一条 diagnostic（与 cpu_sampler 连续失败计数模式一致）；
# 恢复成功即清零，连续失败期间不重复记
PSS_FAILURE_DIAGNOSTIC_THRESHOLD = 2


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class MemSampler:
    """dumpsys meminfo 周期采样（默认 2s），轻量解析 PSS 总量（CM-001 设计 §5）。"""

    name = "mem"
    metric_ids = (METRIC_ID,)

    def __init__(
        self,
        *,
        interval_ms: int = DEFAULT_INTERVAL_MS,
        utc_now: Callable[[], str] | None = None,
    ) -> None:
        self.interval_ms = max(1, int(interval_ms))
        self._utc_now = utc_now or _utc_now_iso
        # 与 cpu/net/power/gpu_thermal 采样器同款内部诊断缓冲（runner 停止采样处统一 drain）
        self._diagnostics: list[str] = []
        self._pss_fail_streak = 0

    @property
    def diagnostics(self) -> list[str]:
        return list(self._diagnostics)

    def drain_diagnostics(self) -> list[str]:
        """取走并清空采样器内部累计的 diagnostic（与调度器 diagnostic 命名风格一致）。"""
        drained = list(self._diagnostics)
        self._diagnostics.clear()
        return drained

    def _meminfo_command(self, ctx: DeviceContext) -> list[str]:
        if not ctx.package_name:
            return []
        return ctx.adb.command("shell", "dumpsys", "meminfo", ctx.package_name, serial=ctx.serial)

    def _read_pss_mb(self, ctx: DeviceContext) -> float | None:
        command = self._meminfo_command(ctx)
        if not command:
            return None
        try:
            completed = ctx.command_runner(command, 30)
        except Exception:  # noqa: BLE001 - 单次读取失败按无数据处理，由调度器继续
            return None
        if completed.returncode != 0:
            return None
        parsed = parse_meminfo(completed.stdout)
        pss_mb = parsed.get("pss_mb")
        return float(pss_mb) if isinstance(pss_mb, (int, float)) else None

    def probe(self, ctx: DeviceContext) -> ProbeResult:
        """探测 meminfo 采集通道可用性，而非目标进程当前存活（smoke-fix2）。

        runner 生命周期中 probe 先于 launch 执行（probe→start→launch），本 run 的
        force_stop 已杀掉目标进程——此时 `dumpsys meminfo <pkg>` 输出 "No process
        found"（rc=0）属预期，launch 后进程必然存在，通道完全可用。仅 adb 命令
        本身失败（rc!=0 / 抛错 / 设备异常）才 unavailable。
        """
        if not ctx.package_name:
            return ProbeResult.unavailable("package_name missing")
        command = self._meminfo_command(ctx)
        if not command:
            return ProbeResult.unavailable("package_name missing")
        try:
            completed = ctx.command_runner(command, 30)
        except Exception as exc:  # noqa: BLE001 - 设备异常/超时按能力缺失，由调度器剔除
            return ProbeResult.unavailable(f"dumpsys meminfo command failed: {exc}")
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[:200]
            suffix = f": {detail}" if detail else ""
            return ProbeResult.unavailable(f"dumpsys meminfo failed rc={completed.returncode}{suffix}")
        return ProbeResult.ok()

    def start(self, ctx: DeviceContext) -> None:
        return None

    def sample(self, ctx: DeviceContext) -> list[SampleRecord]:
        pss_mb = self._read_pss_mb(ctx)
        if pss_mb is None:
            self._track_pss_miss()
            return []
        self._pss_fail_streak = 0  # 读取恢复，失败连击清零
        return [
            SampleRecord(
                metric_id=METRIC_ID,
                value=pss_mb,
                ts_ms=ctx.elapsed_ms(),
                tags={
                    "unit": "MB",
                    "collector": f"sampler.{self.name}",
                    # 采样窗口供 summary coverage 因子使用；tsUtc 墙钟供录屏/跨指标对齐（设计 §2）
                    "samplingWindowMs": self.interval_ms,
                    "tsUtc": self._utc_now(),
                },
            )
        ]

    def _track_pss_miss(self) -> None:
        self._pss_fail_streak += 1
        if self._pss_fail_streak == PSS_FAILURE_DIAGNOSTIC_THRESHOLD:
            self._diagnostics.append(
                f"sampler.{self.name}.pss.unavailable:dumpsys meminfo failed for "
                f"{PSS_FAILURE_DIAGNOSTIC_THRESHOLD} consecutive samples"
            )

    def stop(self) -> list[SampleRecord]:
        # 协议 stop() 不携带 ctx，无法补一次设备读数；周期采样可用时 collector 会抑制结束
        # 快照（避免双口径），最后不足一个 interval 的窗口缺口由"collect 前及时 stop"控制。
        return []
