from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from .base import DeviceContext, ProbeResult, SampleRecord

# 指标 ID 固定（CM-001 METRIC_SOURCES §4）：
#   power.current.ma（counter，主源）；power.level.pct（counter，降级源）；
#   power.energy.mah（估算，METRIC_SOURCES §3 要点 3：电流均值 × 窗口时长 / 3600）
METRIC_CURRENT_MA = "power.current.ma"
METRIC_LEVEL_PCT = "power.level.pct"
METRIC_ENERGY_MAH = "power.energy.mah"

# 功耗采样默认间隔 1s（CM-TASK-017）
DEFAULT_INTERVAL_MS = 1000

BATTERY_DIR = "/sys/class/power_supply/battery"
# 单次 adb shell 批量读取多个 power_supply 节点（CM-001 设计 §6，减少进程启动开销）；
# 缺失/限权节点输出空值（2>/dev/null），由解析端按缺项处理。
BATTERY_READ_COMMAND = (
    f"echo current_now=$(cat {BATTERY_DIR}/current_now 2>/dev/null); "
    f"echo voltage_now=$(cat {BATTERY_DIR}/voltage_now 2>/dev/null); "
    f"echo capacity=$(cat {BATTERY_DIR}/capacity 2>/dev/null); "
    f"echo charge_full=$(cat {BATTERY_DIR}/charge_full 2>/dev/null)"
)

_MS_PER_MINUTE = 60_000.0
_UA_PER_MA = 1000.0
_UAH_PER_MAH = 1000.0  # 内核 power_supply ABI：charge_full 单位 µAh
# µA × µV = 1e-12 W = 1e-9 mW
_MW_PER_UA_UV = 1e-9


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _crosscheck_tags(values: dict[str, float]) -> dict[str, float]:
    """互核输入留痕（S2 power-energy-crosscheck，2026-11-10）：capacity/charge_full
    进能量记录 tags（capacityPct / chargeFullMah，µAh→mAh /1000）——会话级互核
    （主链 Σ电流积分 vs 降级链 charge_full×|Δcapacity|）的输入在会话内齐备，
    不依赖读取时序回放。缺项键省略（不伪造）。"""
    tags: dict[str, float] = {}
    capacity_pct = values.get("capacity")
    if capacity_pct is not None:
        tags["capacityPct"] = capacity_pct
    charge_full_uah = values.get("charge_full")
    if charge_full_uah:
        tags["chargeFullMah"] = round(charge_full_uah / _UAH_PER_MAH, 3)
    return tags


def parse_power_supply(stdout: str) -> dict[str, float]:
    """解析 `echo key=$(cat ...)` 批量输出 -> {key: 数值}；空值/非数值的键省略。"""
    values: dict[str, float] = {}
    for line in stdout.splitlines():
        key, sep, raw = line.strip().partition("=")
        if not sep or not raw:
            continue
        try:
            values[key.strip()] = float(raw.strip())
        except ValueError:
            continue
    return values


class PowerSampler:
    """功耗周期采样（CM-TASK-017，METRIC_SOURCES §3）。

    主源：current_now + voltage_now -> power.current.ma（符号约定按厂商取绝对值，
    值为 mA；原始带符号读数保留在 tags.currentRawMa——power-4 修复 2026-11-06：
    方向信息不再整体丢失，充/放电会话可经 tags 区分；电压仅用于 tags.powerMw
    上下文，µA×µV/1e9）。主源同时派生 power.energy.mah = 窗口端点电流均值 ×
    窗口时长 / 3600（METRIC_SOURCES §3 要点 3 公式落地，power-1 修复 2026-11-06：
    此前主源从不产出 energy，契约公式未实现、常见设备勾选后恒空）。
    降级：current_now 缺失时仅做 capacity（电量 level）差分 -> 电量斜率 %/min，
    并按 charge_full 估算窗口能耗 mAh（同一 energy 指标的 level 版本估算，
    semantic=estimated）；均不可用 -> unavailable（由 probe reason 承载）。

    能耗口径统一（power-2 修复 2026-11-06）：**每条 energy 记录 = 单窗口增量**，
    会话总量由 summary 层按积分（sum）聚合，不在此处累计。
    """

    name = "power"
    metric_ids = (METRIC_CURRENT_MA, METRIC_LEVEL_PCT, METRIC_ENERGY_MAH)

    def __init__(
        self,
        *,
        interval_ms: int = DEFAULT_INTERVAL_MS,
        utc_now: Callable[[], str] | None = None,
    ) -> None:
        self.interval_ms = max(1, int(interval_ms))
        self._utc_now = utc_now or _utc_now_iso
        # 降级链状态：(capacity_pct, ts_ms)
        self._level_baseline: tuple[float, int] | None = None
        # 主源电流积分状态：(窗口末端 |电流| mA, ts_ms)——energy = 端点均值×窗口/3600
        self._current_baseline: tuple[float, int] | None = None
        self._diagnostics: list[str] = []

    def drain_diagnostics(self) -> list[str]:
        drained = list(self._diagnostics)
        self._diagnostics.clear()
        return drained

    def _read(self, ctx: DeviceContext) -> dict[str, float] | None:
        command = ctx.adb.command("shell", BATTERY_READ_COMMAND, serial=ctx.serial)
        try:
            completed = ctx.command_runner(command, 10)
        except Exception:  # noqa: BLE001 - 单次读取失败按无数据处理，调度器继续
            return None
        if completed.returncode != 0:
            return None
        return parse_power_supply(completed.stdout)

    def probe(self, ctx: DeviceContext) -> ProbeResult:
        values = self._read(ctx)
        if values is None:
            return ProbeResult.unavailable(f"power_supply battery nodes unreadable: {BATTERY_DIR}/*")
        if "current_now" in values:
            return ProbeResult.ok()
        if "capacity" in values:
            return ProbeResult(available=True, reason="degraded: current_now unavailable, level slope only")
        return ProbeResult.unavailable(f"power_supply battery nodes unreadable: {BATTERY_DIR}/*")

    def start(self, ctx: DeviceContext) -> None:
        self._level_baseline = None
        self._current_baseline = None
        self._diagnostics.clear()
        values = self._read(ctx)
        if values is not None and "capacity" in values:
            self._level_baseline = (values["capacity"], ctx.elapsed_ms())
        if values is not None and "current_now" in values:
            self._current_baseline = (
                abs(values["current_now"]) / _UA_PER_MA,
                ctx.elapsed_ms(),
            )

    def sample(self, ctx: DeviceContext) -> list[SampleRecord]:
        values = self._read(ctx)
        if values is None:
            self._diagnostics.append("power_supply read failed; sample skipped")
            return []
        ts_ms = ctx.elapsed_ms()
        common_tags = {
            "collector": f"sampler.{self.name}",
            "samplingWindowMs": self.interval_ms,
            "tsUtc": self._utc_now(),
        }
        if "current_now" in values:
            return self._primary_records(values, ts_ms, common_tags)
        return self._degraded_records(values, ts_ms, common_tags)

    def _primary_records(
        self,
        values: dict[str, float],
        ts_ms: int,
        common_tags: dict[str, object],
    ) -> list[SampleRecord]:
        # 主源：current_now（µA）。符号方向按厂商差异取绝对值（METRIC_SOURCES §3）；
        # 原始带符号读数保留进 tags.currentRawMa（power-4 修复：方向信息不丢失，
        # 充/放电会话可经 tags 区分——厂商符号约定不统一，不在值层解读方向）。
        raw_current_ma = values["current_now"] / _UA_PER_MA
        current_ma = abs(raw_current_ma)
        tags: dict[str, object] = {
            **common_tags,
            "unit": "mA",
            "source": "current_now",
            "currentRawMa": round(raw_current_ma, 4),
        }
        if "voltage_now" in values:
            power_mw = abs(values["current_now"]) * values["voltage_now"] * _MW_PER_UA_UV
            tags["powerMw"] = round(power_mw, 3)
        records = [
            SampleRecord(metric_id=METRIC_CURRENT_MA, value=round(current_ma, 4), ts_ms=ts_ms, tags=tags)
        ]
        # 能耗派生（power-1 修复，METRIC_SOURCES §3 要点 3 公式）：
        # energy_mah = 窗口端点电流均值(mA) × 窗口时长(s) / 3600——每条记录为
        # 单窗口增量（power-2 口径统一），tags.samplingWindowMs 取实测窗口
        #（net-2 同修）。基线缺失（start 失败/首 tick）时仅建基线不出点。
        baseline = self._current_baseline
        self._current_baseline = (current_ma, ts_ms)
        if baseline is None:
            return records
        prev_ma, prev_ts = baseline
        delta_ms = ts_ms - prev_ts
        if delta_ms <= 0:
            return records
        mean_ma = (prev_ma + current_ma) / 2.0
        energy_mah = mean_ma * (delta_ms / 1000.0) / 3600.0
        records.append(
            SampleRecord(
                metric_id=METRIC_ENERGY_MAH,
                value=round(energy_mah, 4),
                ts_ms=ts_ms,
                tags={
                    **common_tags,
                    "unit": "mAh",
                    "source": "current_integral",
                    "semantic": "estimated",
                    "samplingWindowMs": delta_ms,
                    # 互核输入留痕（S2 power-energy-crosscheck）：主链能量记录同带
                    # capacity/charge_full 快照，会话级互核输入在会话内齐备
                    **_crosscheck_tags(values),
                },
            )
        )
        return records

    def _degraded_records(
        self,
        values: dict[str, float],
        ts_ms: int,
        common_tags: dict[str, object],
    ) -> list[SampleRecord]:
        # 降级：仅 capacity（电量 level %）差分。报告斜率 %/min + 能耗估算 mAh。
        baseline = self._level_baseline
        if "capacity" not in values:
            self._diagnostics.append("capacity unavailable in degraded mode; sample skipped")
            return []
        self._level_baseline = (values["capacity"], ts_ms)
        if baseline is None:
            return []
        prev_pct, prev_ts = baseline
        delta_ms = ts_ms - prev_ts
        if delta_ms <= 0:
            return []
        delta_pct = abs(values["capacity"] - prev_pct)
        if delta_pct == 0:
            return []
        slope_pct_per_min = delta_pct / (delta_ms / _MS_PER_MINUTE)
        # tags.samplingWindowMs 取实测差分窗口（net-2 修复：值为窗口差分派生，
        # 名义间隔标签会误导按窗口二次归一的消费方）
        level_tags = {
            **common_tags,
            "unit": "%/min",
            "source": "capacity_diff",
            "samplingWindowMs": delta_ms,
        }
        records = [
            SampleRecord(
                metric_id=METRIC_LEVEL_PCT,
                value=round(slope_pct_per_min, 4),
                ts_ms=ts_ms,
                tags=level_tags,
            )
        ]
        charge_full_uah = values.get("charge_full")
        if charge_full_uah:
            # 能耗估算（METRIC_SOURCES §3 要点 3 同一 energy 指标的 level 版本）：
            # 窗口耗电 mAh = charge_full(µAh)/1000 × |Δlevel%| / 100，标注
            # estimated；每条记录 = 单窗口增量（power-2 口径统一）。
            charge_full_mah = charge_full_uah / _UAH_PER_MAH
            energy_mah = charge_full_mah * delta_pct / 100.0
            records.append(
                SampleRecord(
                    metric_id=METRIC_ENERGY_MAH,
                    value=round(energy_mah, 4),
                    ts_ms=ts_ms,
                    tags={
                        **common_tags,
                        "unit": "mAh",
                        "source": "level_slope",
                        "semantic": "estimated",
                        "samplingWindowMs": delta_ms,
                        # 互核输入留痕（S2 power-energy-crosscheck）：降级链能量
                        # 记录同带 capacity/charge_full 快照（charge_full 为本链
                        # 估算输入，capacity 为窗口端点电量）
                        **_crosscheck_tags(values),
                    },
                )
            )
        return records

    def stop(self) -> list[SampleRecord]:
        # 与 mem_sampler 同口径：协议 stop() 不携带 ctx，无法补一次设备读数。
        return []
