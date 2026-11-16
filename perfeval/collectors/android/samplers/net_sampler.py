from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timezone

from .base import DeviceContext, ProbeResult, SampleRecord

# 指标 ID 固定（CM-001 METRIC_SOURCES §4）：sampler.net -> net.rx.kbps / net.tx.kbps
METRIC_RX_KBPS = "net.rx.kbps"
METRIC_TX_KBPS = "net.tx.kbps"

# 网络采样默认间隔 1s（CM-TASK-016）
DEFAULT_INTERVAL_MS = 1000

QTAGUID_STATS_PATH = "/proc/net/xt_qtaguid/stats"

# 速率口径（METRIC_SOURCES §3 差分指标）：kbps = Δbytes × 8 / Δt_ms（kilobits per second）
_KBPS_PER_BYTE_MS = 8.0

# qtaguid 行列布局：idx iface acct_tag_hex uid_tag_int cnt_set rx_bytes rx_packets tx_bytes ...
_QTAGUID_UID_COLUMN = 3
_QTAGUID_RX_COLUMN = 5
_QTAGUID_TX_COLUMN = 7
_QTAGUID_MIN_COLUMNS = 8

_UID_LINE_RE = re.compile(r"package:\S+ uid:(\d+)")
# dumpsys netstats NetworkStatsHistory 桶行：st=<epoch_s> rb=<rx> rp=<rx_pkts> tb=<tx> tp=<tx_pkts> op=<n>
_NETSTATS_BUCKET_RE = re.compile(
    r"st=(\d+)\s+rb=(\d+)\s+rp=(\d+)\s+tb=(\d+)\s+tp=(\d+)\s+op=(\d+)"
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_package_uid(stdout: str) -> int | None:
    """`pm list packages -U <pkg>` 输出 -> 目标 uid（如 `package:com.x uid:10112`）。"""
    match = _UID_LINE_RE.search(stdout)
    return int(match.group(1)) if match else None


def parse_qtaguid_stats(stdout: str) -> dict[int, tuple[int, int]]:
    """xt_qtaguid stats 按 uid 汇总 (rx_bytes, tx_bytes)：跨 iface/tag/cnt_set 求和。"""
    per_uid: dict[int, list[int]] = {}
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) < _QTAGUID_MIN_COLUMNS or parts[0] == "idx":
            continue
        try:
            uid = int(parts[_QTAGUID_UID_COLUMN])
            rx = int(parts[_QTAGUID_RX_COLUMN])
            tx = int(parts[_QTAGUID_TX_COLUMN])
        except ValueError:
            continue
        agg = per_uid.setdefault(uid, [0, 0])
        agg[0] += rx
        agg[1] += tx
    return {uid: (float(rx), float(tx)) for uid, (rx, tx) in per_uid.items()}


def parse_netstats_totals(stdout: str) -> tuple[int, int] | None:
    """dumpsys netstats 设备总量（Xt stats 优先，Dev stats 兜底）：
    累计 section 内全部 NetworkStatsHistory 桶的 rb/tb（uid=-1 即整机口径）。
    """
    for section in ("Xt stats:", "Dev stats:"):
        rx = tx = 0
        found = False
        in_section = False
        for line in stdout.splitlines():
            if not in_section:
                if line.strip() == section:
                    in_section = True
                continue
            if line and not line[0].isspace():
                break  # 顶层 section 边界（如 "Top openSession callers:"）
            match = _NETSTATS_BUCKET_RE.search(line)
            if match:
                found = True
                rx += int(match.group(2))
                tx += int(match.group(4))
        if found:
            return rx, tx
    return None


class NetSampler:
    """网络速率周期采样（CM-TASK-016，METRIC_SOURCES §3）。

    主源：/proc/net/xt_qtaguid/stats 按 uid 差分（uid 由 `pm list packages -U <pkg>`
    解析；多用户/Work profile 归一见 ``_qtaguid_totals``——net-1 修复，2026-11-04
    体检：qtaguid 按实际所在 user 记完整 uid（100000×userId+appId），副用户实例
    的流量行按 appId 归一命中，不再静默假零）；
    降级：dumpsys netstats 整机总量差分；qtaguid（新内核已移除）与 netstats 均不可用 -> unavailable。
    """

    name = "net"
    metric_ids = (METRIC_RX_KBPS, METRIC_TX_KBPS)

    # Android 多用户 uid 语义：uid = userId × 100000 + appId（`pm list packages -U`
    # 解析的是 user 0 口径 appId）
    _USER_ID_OFFSET = 100_000

    def __init__(
        self,
        *,
        interval_ms: int = DEFAULT_INTERVAL_MS,
        utc_now: Callable[[], str] | None = None,
    ) -> None:
        self.interval_ms = max(1, int(interval_ms))
        self._utc_now = utc_now or _utc_now_iso
        self._uid: int | None = None
        self._prefer_qtaguid = True
        # 差分基线：(rx_bytes, tx_bytes, ts_ms, source)——source 参与差分校验，
        # 回退源（整机总量）与主源（单 uid）口径不同，切换时必须重建基线（R1 修复）
        self._baseline: tuple[float, float, int, str] | None = None
        self._diagnostics: list[str] = []

    # --- 诊断（调度器只隔离 sampler 级异常；sampler 内子源失败经此透出） ---

    def drain_diagnostics(self) -> list[str]:
        drained = list(self._diagnostics)
        self._diagnostics.clear()
        return drained

    # --- 设备读数 ---

    def _run(self, ctx: DeviceContext, command: list[str], timeout: float) -> str | None:
        try:
            completed = ctx.command_runner(command, timeout)
        except Exception:  # noqa: BLE001 - 单次读取失败按无数据处理，调度器继续
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout

    def _resolve_uid(self, ctx: DeviceContext) -> int | None:
        if not ctx.package_name:
            return None
        command = ctx.adb.command("shell", "pm", "list", "packages", "-U", ctx.package_name, serial=ctx.serial)
        stdout = self._run(ctx, command, 15)
        return parse_package_uid(stdout) if stdout is not None else None

    def _read_qtaguid(self, ctx: DeviceContext) -> dict[int, tuple[int, int]] | None:
        command = ctx.adb.command("shell", "cat", QTAGUID_STATS_PATH, serial=ctx.serial)
        stdout = self._run(ctx, command, 10)
        if stdout is None:
            return None
        return parse_qtaguid_stats(stdout)

    def _read_netstats(self, ctx: DeviceContext) -> tuple[int, int] | None:
        command = ctx.adb.command("shell", "dumpsys", "netstats", serial=ctx.serial)
        stdout = self._run(ctx, command, 30)
        if stdout is None:
            return None
        return parse_netstats_totals(stdout)

    def _qtaguid_totals(self, per_uid: dict[int, tuple[float, float]]) -> tuple[float, float]:
        """目标 uid 的 (rx, tx) 总量：appId 归一匹配（net-1 修复）。

        qtaguid 按实际所在 user 记完整 uid（100000×userId+appId），`pm list
        packages -U` 解析的是 user 0 口径 appId——目标应用运行在副用户（如
        Secure Folder 等工作配置文件）时精确匹配查不到、曾以 (0,0) 兜底产出
        静默假零。按 ``uid % 100000 == appId`` 归一求和（同包多用户实例均为该
        应用流量；包 appId ≥ 1000，不会误吸 uid 0/100000 等 system 身份）。
        表中确无该 appId 行（应用尚无流量记录）→ (0, 0)，为真实零而非假零。
        """
        app_id = self._uid % self._USER_ID_OFFSET if self._uid is not None else None
        rx = tx = 0.0
        found = False
        for uid, (uid_rx, uid_tx) in per_uid.items():
            if app_id is not None and uid % self._USER_ID_OFFSET == app_id:
                rx += uid_rx
                tx += uid_tx
                found = True
        return (rx, tx) if found else (0.0, 0.0)

    def _read_totals(
        self, ctx: DeviceContext, *, prefer_qtaguid: bool
    ) -> tuple[tuple[float, float], str] | None:
        """按优先链读取目标 uid（或整机）累计字节数，返回 ((rx, tx), source)。"""
        if prefer_qtaguid and self._uid is not None:
            per_uid = self._read_qtaguid(ctx)
            if per_uid is not None:
                return self._qtaguid_totals(per_uid), "qtaguid"
        totals = self._read_netstats(ctx)
        if totals is not None:
            return (float(totals[0]), float(totals[1])), "netstats"
        return None

    # --- 协议实现 ---

    def probe(self, ctx: DeviceContext) -> ProbeResult:
        if not ctx.package_name:
            return ProbeResult.unavailable("package_name missing")
        self._uid = self._resolve_uid(ctx)
        primary_reason: str
        if self._uid is None:
            primary_reason = f"uid resolve failed for {ctx.package_name} (pm list packages -U)"
        elif self._read_qtaguid(ctx) is None:
            primary_reason = "xt_qtaguid stats unreadable"
        else:
            self._prefer_qtaguid = True
            return ProbeResult.ok()
        if self._read_netstats(ctx) is None:
            return ProbeResult.unavailable(f"{primary_reason}; dumpsys netstats unavailable")
        self._prefer_qtaguid = False
        return ProbeResult(available=True, reason="degraded: qtaguid unavailable, dumpsys netstats totals only")

    def start(self, ctx: DeviceContext) -> None:
        self._baseline = None
        self._diagnostics.clear()
        if ctx.package_name:
            self._uid = self._resolve_uid(ctx) or self._uid
        reading = self._read_totals(ctx, prefer_qtaguid=self._prefer_qtaguid)
        if reading is None:
            self._diagnostics.append("baseline read failed; first sample() re-baselines")
            return
        (rx, tx), source = reading
        self._prefer_qtaguid = source == "qtaguid"
        self._baseline = (rx, tx, ctx.elapsed_ms(), source)

    def sample(self, ctx: DeviceContext) -> list[SampleRecord]:
        reading = self._read_totals(ctx, prefer_qtaguid=self._prefer_qtaguid)
        if reading is None:
            self._diagnostics.append("all net sources read failed; sample skipped")
            return []
        (rx, tx), source = reading
        baseline = self._baseline
        ts_ms = ctx.elapsed_ms()
        self._baseline = (rx, tx, ts_ms, source)
        if baseline is None:
            return []
        prev_rx, prev_tx, prev_ts, prev_source = baseline
        if source != prev_source:
            # 回退源与基线口径不同（qtaguid 单 uid vs netstats 整机总量）：直接相减必产
            # 虚高尖峰，视同 re-baseline——本轮不产数，下一轮源稳定后正常差分（R1 修复）
            self._diagnostics.append(f"source switched {prev_source} -> {source}; re-baselined")
            return []
        delta_ms = ts_ms - prev_ts
        if delta_ms <= 0:
            return []
        delta_rx = rx - prev_rx
        delta_tx = tx - prev_tx
        if delta_rx < 0 or delta_tx < 0:
            # 计数器回绕/重启：重建基线，本跳点不产出（避免负速率污染序列）
            self._diagnostics.append("byte counter reset detected; re-baselined")
            return []
        tags = {
            "unit": "kbps",
            "collector": f"sampler.{self.name}",
            "source": source,
            # 实测差分窗口（net-2 修复：netstats 回退源秒级耗时/调度串行会拉长
            # 真实窗口，名义间隔标签会误导按窗口二次归一的消费方；kbps 值本身
            # 一直按真实 delta_ms 计算）
            "samplingWindowMs": int(delta_ms),
            "tsUtc": self._utc_now(),
        }
        return [
            SampleRecord(metric_id=METRIC_RX_KBPS, value=round(delta_rx * _KBPS_PER_BYTE_MS / delta_ms, 4), ts_ms=ts_ms, tags=tags),
            SampleRecord(metric_id=METRIC_TX_KBPS, value=round(delta_tx * _KBPS_PER_BYTE_MS / delta_ms, 4), ts_ms=ts_ms, tags=tags),
        ]

    def stop(self) -> list[SampleRecord]:
        # 与 mem_sampler 同口径：协议 stop() 不携带 ctx，无法补一次设备读数，
        # 末尾不足一个 interval 的缺口由"collect 前及时 stop"控制。
        return []
