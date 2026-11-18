"""FPS real 通道测试（矩阵 R01，契约 §5 注 2；2026-11-02 落地）。

FpsSampler 单测（fake command_runner 应答表：gfxinfo 累计计数序列）+ collector
装配断言（frame.fps.mean → fps 采样器进 SamplingScheduler）。真机端到端见
STATUS 验证记录。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from m6_fixtures import FakeAdb, completed  # noqa: E402


def _gfxinfo(total: int | None) -> str:
    if total is None:
        return "No process found"
    return (
        "** Graphics info for pid 1234 [com.example.game] **\n"
        f"Total frames rendered: {total}\n"
        "Janky frames: 0 (0.00%)\n"
    )


class _Clock:
    """可控单调钟（Δwall-time 精确可控）。"""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class _RunnerAdb:
    """按调用序出应答的 fake adb（reset → 轮询序列）。"""

    def __init__(self, totals: list[int | None]) -> None:
        self._totals = list(totals)
        self.commands: list[list[str]] = []

    def available(self) -> bool:
        return True

    def command(self, *args: str, serial: str | None = None) -> list[str]:
        return ["adb", *(["-s", serial] if serial else []), *args]

    def run(self, *args: str, serial: str | None = None, timeout: float | None = None):
        self.commands.append([*args, serial])
        total = self._totals.pop(0) if self._totals else None
        return completed("" if total is None else _gfxinfo(total))


def _ctx(adb: _RunnerAdb) -> SimpleNamespace:
    return SimpleNamespace(
        adb=adb,
        command_runner=lambda command, timeout=None: adb.run(*command[1:], serial=None),
        serial="S1",
        package_name="com.example.game",
        elapsed_ms=lambda: 0,
    )


def _sampler(adb: _RunnerAdb, clock: _Clock):
    from app.services.fps_sampler import FpsSampler

    return FpsSampler(interval_ms=500, monotonic=clock)


def test_fps_baseline_then_delta() -> None:
    """start（reset）建立基准；首个采样点即完整窗口（reset→首次读数）。"""
    # 应答序：probe 探测 1 次（值无关）→ start reset → 轮询序列
    adb = _RunnerAdb([0, 0, 63, 93])
    clock = _Clock()
    s = _sampler(adb, clock)
    ctx = _ctx(adb)
    assert s.probe(ctx).available is True
    s.start(ctx)
    clock.advance(1.0)
    records = s.sample(ctx)
    assert len(records) == 1
    rec = records[0]
    assert rec.metric_id == "frame.fps.mean"
    assert rec.value == 63.0  # 63 帧 / 1.0s
    assert rec.tags["source"] == "gfxinfo"
    assert rec.tags["unit"] == "fps"
    assert rec.tags["samplingWindowMs"] == 1000
    clock.advance(0.5)
    records = s.sample(ctx)
    assert records[0].value == 60.0  # Δ30 帧 / 0.5s
    assert records[0].tags["samplingWindowMs"] == 500
    # start 时发过一次 reset
    assert any("reset" in " ".join(map(str, c)) for c in adb.commands)


def test_fps_counter_regression_rebaselines_silently() -> None:
    """计数回退 = 进程重启：静默重建基准，该轮不出点。"""
    adb = _RunnerAdb([0, 63, 5, 63])
    clock = _Clock()
    s = _sampler(adb, clock)
    ctx = _ctx(adb)
    s.start(ctx)
    clock.advance(1.0)
    assert len(s.sample(ctx)) == 1
    clock.advance(0.5)
    assert s.sample(ctx) == []  # 回退轮：重建基准
    clock.advance(1.0)
    records = s.sample(ctx)
    assert len(records) == 1
    assert records[0].value == 58.0  # 5 → 63 帧 / 1.0s


def test_fps_read_failure_streak_diagnostic_once() -> None:
    """连续 ≥2 次读不到计数 → 一条通道诊断（恢复即清零），不逐次刷屏。"""
    adb = _RunnerAdb([0, None, None, 30])
    clock = _Clock()
    s = _sampler(adb, clock)
    ctx = _ctx(adb)
    s.start(ctx)
    clock.advance(0.5)
    assert s.sample(ctx) == []  # 失败 1
    clock.advance(0.5)
    assert s.sample(ctx) == []  # 失败 2 → 诊断
    assert s.drain_diagnostics() == ["fps.gfxinfo.no_counters"]
    clock.advance(0.5)
    records = s.sample(ctx)  # 恢复：30 帧 / 1.5s
    assert len(records) == 1
    assert records[0].value == 20.0
    assert s.drain_diagnostics() == []


def test_fps_probe_channel_failure() -> None:
    """gfxinfo 命令 rc!=0（非进程存活语义）→ probe unavailable。"""
    from app.services.fps_sampler import FpsSampler

    class _FailAdb:
        def available(self) -> bool:
            return True

        def command(self, *args: str, serial: str | None = None) -> list[str]:
            return ["adb", *args]

    class _FailRunner:
        def __call__(self, command, timeout=None):
            return completed("", 1)

    s = FpsSampler()
    ctx = SimpleNamespace(
        adb=_FailAdb(),
        command_runner=_FailRunner(),
        serial="S1",
        package_name="com.example.game",
        elapsed_ms=lambda: 0,
    )
    result = s.probe(ctx)
    assert result.available is False
    assert result.reason and "rc=1" in result.reason


def test_collector_wires_fps_sampler() -> None:
    """frame.fps.mean 在会话启用集 → 装配出 fps 采样器（统一 build_samplers 路径）。"""
    from perfeval.collectors.android.samplers import build_samplers

    # 与 collector_service 同款注册路径已随模块导入生效
    import app.services.collector_service  # noqa: F401

    samplers = build_samplers({"fps": {"intervalMs": 500}})
    assert len(samplers) == 1
    assert "frame.fps.mean" in samplers[0].metric_ids


def test_collector_drains_sampler_internal_diagnostics() -> None:
    """实机验证修复（2026-11-03）：sampler 内部级诊断也要进 SSE。

    此前后端只 drain 调度器级诊断——fps 通道升级（fallback_surfaceflinger/
    escalate_timestats）挂在 sampler 自身缓冲（perfeval runner.py:775-779 收口
    统一 drain 的同口径），会话内对用户不可见（真机实测复现）。
    """
    from types import SimpleNamespace

    from app.services import collector_service as mod
    from app.services.collector_service import RealCollector
    from app.sse import SseBus

    class _Sampler:
        name = "fps"
        metric_ids = ("frame.fps.mean",)
        interval_ms = 500

        def __init__(self) -> None:
            self._diags = ["fps.source.fallback_surfaceflinger"]

        def drain_diagnostics(self):
            drained = list(self._diags)
            self._diags.clear()
            return drained

        def sample(self, ctx):
            return []

    sched = SimpleNamespace(
        drain_diagnostics=lambda: [],
        samplers=[_Sampler()],
    )

    class _Session:
        id = "sess-drain"
        metrics: dict = {}

    bus = SseBus("sess-drain")
    collector = RealCollector.__new__(RealCollector)
    collector._scheduler = sched
    collector.session = _Session()
    collector._enabled_lock = __import__("threading").Lock()

    events: list[dict] = []
    orig_emit = collector.emit_diagnostic
    collector.emit_diagnostic = lambda diag, severity, message: events.append(  # type: ignore[method-assign]
        {"diag": diag, "severity": severity, "message": message}
    )
    collector._drain_diagnostics()
    assert [e["diag"] for e in events] == ["fps.source.fallback_surfaceflinger"]
    # 幂等：再 drain 一次无重复
    collector._drain_diagnostics()
    assert len(events) == 1


# ---- SurfaceFlinger 兜底（Display 侧，Vulkan/原生渲染盲区）----


class _RouteAdb:
    """按命令路由应答的 fake adb（gfxinfo 计数队列 / --list / --latency）。"""

    def __init__(self, handler) -> None:
        self._handler = handler
        self.commands: list[list[str]] = []

    def available(self) -> bool:
        return True

    def command(self, *args: str, serial: str | None = None) -> list[str]:
        return ["adb", *(["-s", serial] if serial else []), *args]

    def run(self, *args: str, serial: str | None = None, timeout: float | None = None):
        self.commands.append([*args, serial])
        out, rc = self._handler(" ".join(args))
        return completed(out, rc)


def _sf_ctx(adb: _RouteAdb) -> SimpleNamespace:
    return SimpleNamespace(
        adb=adb,
        command_runner=lambda command, timeout=None: adb.run(*command[1:], serial=None),
        serial="S1",
        package_name="com.example.game",
        elapsed_ms=lambda: 0,
    )


_LATENCY_NS = [100_000_000 + i * 16_000_000 for i in range(60)]  # 60 帧 × 16ms


def _latency_output(ns_list: list[int]) -> str:
    return "".join(f"0 {ns} {ns}\n" for ns in ns_list)


def test_fps_falls_back_to_surfaceflinger_and_counts() -> None:
    """gfxinfo 连续 3 个零帧窗口 → 活跃度选层（选最近有合成帧的层）→ 出 fps。"""
    totals = iter([0, 0, 0, 0, 0])  # probe/start + 3 个零帧窗口
    sv_calls = {"n": 0}

    def handler(joined: str) -> tuple[str, int]:
        if "--latency" in joined:
            if "SurfaceView" in joined:
                base = sv_calls["n"] * 60
                sv_calls["n"] += 1
                return _latency_output(
                    [100_000_000 + (base + i) * 16_000_000 for i in range(60)]
                ), 0
            return _latency_output([]), 0  # 非活跃图层零帧（选层应避开）
        if "--list" in joined:
            return (
                "com.example.game/com.example.game.SplashActivity#0\n"
                "com.example.game/com.example.game.MainActivity#0\n"
                "SurfaceView[com.example.game]#0(BLAST)\n"
            ), 0
        return _gfxinfo(next(totals)), 0

    adb = _RouteAdb(handler)
    clock = _Clock()
    s = _sampler(adb, clock)
    ctx = _sf_ctx(adb)
    s.start(ctx)
    for _ in range(3):  # 3 个零帧窗口，第 3 轮触发切换
        clock.advance(0.5)
        records = s.sample(ctx)
        assert len(records) == 1 and records[0].tags["source"] == "gfxinfo"
    assert "fps.source.fallback_surfaceflinger" in s.drain_diagnostics()
    # 活跃度选层：选中有合成帧的 SurfaceView，避开零帧的 Activity 层
    assert s._sf_layer == "SurfaceView[com.example.game]#0"  # # 后缀即图层名终点
    clock.advance(0.5)
    assert s.sample(ctx) == []  # SF 首轮建时间基准
    clock.advance(1.0)
    records = s.sample(ctx)
    assert len(records) == 1
    assert records[0].tags["source"] == "surfaceflinger"
    assert records[0].value == 60.0  # 60 个新时间戳 / 1.0s
    assert records[0].tags["samplingWindowMs"] == 1000

def test_fps_sf_no_layer_stays_gfxinfo() -> None:
    """无匹配图层 → 一条诊断、留在 gfxinfo 通道继续出点。"""
    totals = iter([0, 0, 0, 0, 45])  # start + 3 零帧窗口 + 恢复帧

    def handler(joined: str) -> tuple[str, int]:
        if "--latency" in joined or "--list" in joined:
            return "", 0
        return _gfxinfo(next(totals)), 0

    adb = _RouteAdb(handler)
    clock = _Clock()
    s = _sampler(adb, clock)
    ctx = _sf_ctx(adb)
    s.start(ctx)
    for _ in range(3):
        clock.advance(0.5)
        s.sample(ctx)
    assert "fps.surfaceflinger.no_layer" in s.drain_diagnostics()
    clock.advance(1.0)
    records = s.sample(ctx)
    assert records[0].tags["source"] == "gfxinfo"  # 未切换
    assert records[0].value == 45.0


def test_fps_sf_latency_fail_reverts_to_gfxinfo() -> None:
    """SF latency 采样期失败 → 诊断并回退 gfxinfo（基准重建后恢复出点）。"""
    state = {"n": 0}
    lat = {"n": 0}

    def handler(joined: str) -> tuple[str, int]:
        if "--latency" in joined:
            lat["n"] += 1
            if lat["n"] == 1:  # 选层探测（唯一候选，零帧也入选）
                return _latency_output([]), 0
            return "", 1  # 采样期失败
        if "--list" in joined:
            return "com.example.game/com.example.game.MainActivity#0\n", 0
        state["n"] += 1
        return _gfxinfo(0 if state["n"] <= 5 else 45), 0

    adb = _RouteAdb(handler)
    clock = _Clock()
    s = _sampler(adb, clock)
    ctx = _sf_ctx(adb)
    s.start(ctx)
    for _ in range(3):
        clock.advance(0.5)
        s.sample(ctx)
    clock.advance(0.5)
    assert s.sample(ctx) == []  # 切到 SF；latency 失败 → 回退诊断
    assert "fps.surfaceflinger.unavailable" in s.drain_diagnostics()
    clock.advance(1.0)
    assert s.sample(ctx) == []  # gfxinfo 基准重建轮
    clock.advance(1.0)
    records = s.sample(ctx)
    assert records[0].tags["source"] == "gfxinfo"
    assert records[0].value == 45.0

# ---- timestats 三级通道（新 Android BLAST 下 --latency 废弃，实测 SM_F9560）----


_TS_DUMP = """SurfaceFlinger TimeStats:
displayRefreshRate = 120 fps
layerName = com.other.app/com.other.app.MainActivity
packageName = com.other.app
totalFrames = 7
layerName = com.example.game/com.example.game.MainActivity
packageName = com.example.game
totalFrames = {total}
layerName = com.example.game/com.example.game:push
packageName =
totalFrames = 12
"""


def test_fps_latency_invalid_escalates_to_timestats() -> None:
    """--latency 仅回刷新周期行（新 Android）→ 直接升级 timestats 差分出 fps。"""
    gtotals = iter([0, 0, 0, 0, 0])  # probe/start + 3 个零帧窗口
    ts_totals = iter([100, 252, 312])

    def handler(joined: str) -> tuple[str, int]:
        if "--timestats" in joined:
            if "-dump" in joined:
                return _TS_DUMP.format(total=next(ts_totals)), 0
            return "", 0  # -clear/-enable/-disable
        if "--latency" in joined:
            return "8333333\n", 0  # 仅刷新周期（120Hz），无时间戳
        if "--list" in joined:
            return "com.example.game/com.example.game.MainActivity#0\n", 0
        if "pm" in joined and "-U" in joined:
            return "", 0  # uid 解析失败 → packageName 精确相等兜底
        return _gfxinfo(next(gtotals)), 0

    adb = _RouteAdb(handler)
    clock = _Clock()
    s = _sampler(adb, clock)
    ctx = _sf_ctx(adb)
    s.start(ctx)
    for _ in range(3):
        clock.advance(0.5)
        s.sample(ctx)
    clock.advance(0.5)
    assert s.sample(ctx) == []  # 首个 SF 采样：检测 latency 仅回周期行 → 升级
    drained = s.drain_diagnostics()
    assert "fps.surfaceflinger.latency_unavailable" in drained
    assert "fps.source.escalate_timestats" in drained
    assert s._mode == "timestats"
    clock.advance(1.0)
    assert s.sample(ctx) == []  # timestats 首轮建基准（主层累计 100）
    clock.advance(1.0)
    records = s.sample(ctx)
    assert records[0].tags["source"] == "surfaceflinger"
    assert records[0].tags["method"] == "timestats"
    # 最活跃层逐块差分：252-100=152。:push 子进程层（packageName 空）与
    # com.other.app 层不命中——不再跨块求和（2026-11-06 修复）
    assert records[0].value == 152.0
    assert records[0].tags["layer"] == "com.example.game/com.example.game.MainActivity"
    clock.advance(1.0)
    assert s.sample(ctx)[0].value == 60.0  # 312-252


def test_fps_timestats_blocks_exact_match_no_substring() -> None:
    """块匹配仅取精确命中（uid 精确相等；uid 不可用时 packageName 精确相等）。

    旧实现「uid OR 包名子串 OR 图层名子串 + 跨块求和」已被 2026-11-06 修复移除：
    子包/子进程层不再被子串误吸入（真机 358~361fps 根因之一）。
    """
    from app.services.fps_sampler import FpsSampler

    dump = _TS_DUMP.format(total=240)
    adb = _RouteAdb(lambda joined: (dump, 0) if "--timestats" in joined and "-dump" in joined else ("", 0))
    s = FpsSampler()
    ctx = _sf_ctx(adb)
    s._ts_set_enabled(ctx, True)
    blocks = s._read_ts_blocks(ctx)
    assert blocks == {
        # 仅 packageName 精确相等的主进程层（:push 层 packageName 为空 → 不命中）
        ("com.example.game/com.example.game.MainActivity", "com.example.game", ""): 240,
    }
    s._ts_set_enabled(ctx, False)


def test_fps_timestats_uid_match_when_names_redacted() -> None:
    """图层名脱敏为空（实测 One UI）→ 按 uid 归属命中目标包的层。"""
    from app.services.fps_sampler import FpsSampler

    dump = (
        "SurfaceFlinger TimeStats:\n"
        "uid = 10123\n"
        "layerName = \n"
        "packageName = \n"
        "totalFrames = 88\n"
        "uid = 10780\n"
        "layerName = \n"
        "packageName = \n"
        "totalFrames = 55\n"
    )

    def handler(joined: str) -> tuple[str, int]:
        if "--timestats" in joined and "-dump" in joined:
            return dump, 0
        if "pm" in joined and "-U" in joined:
            return "package:com.example.game uid:10780\n", 0
        return "", 0

    adb = _RouteAdb(handler)
    s = FpsSampler()
    ctx = _sf_ctx(adb)
    assert s._resolve_pkg_uid(ctx) == "10780"
    assert s._read_ts_blocks(ctx) == {("", "", "10780"): 55}  # 仅 uid=10780 的层


# ---- 实机 golden 回填（2026-11-03，SM_F9560 / Android 16 真机 dump）----


def test_fps_timestats_uid_match_on_real_sm9560_dump() -> None:
    """真机 `--timestats -dump` 样本（tests/fixtures/real_sm9560_timestats.txt）。

    SM_F9560 实测块字段顺序为 uid 在 layerName **之前**（契约「字段顺序不定」
    的实机印证），图层名含包名（`com.perfeval.gamebox/…#3127`）、packageName 为空、
    脱敏层名字面 `none`。uid=10780（前台应用）→ 逐块取 {块身份: totalFrames}。
    """
    from pathlib import Path

    from app.services.fps_sampler import FpsSampler

    real_dump = (
        Path(__file__).resolve().parent
        / "fixtures"
        / "real_sm9560_timestats.txt"
    ).read_text(encoding="utf-8")

    class _RealDumpAdb(_RouteAdb):
        """timestats -dump 回真实样本、pm list packages -U 回真机 uid 表。"""

        def __init__(self) -> None:
            super().__init__(lambda joined: (real_dump, 0) if "timestats" in joined else ("", 0))

    adb = _RealDumpAdb()
    # 真机目标包（dump 中图层名含 com.perfeval.gamebox）
    ctx = SimpleNamespace(
        adb=adb,
        command_runner=lambda command, timeout=None: adb.run(*command[1:], serial=None),
        serial="S1",
        package_name="com.perfeval.gamebox",
        elapsed_ms=lambda: 0,
    )
    sampler = FpsSampler(interval_ms=500)
    # uid 归属主路径：真机 `pm list packages -U` 该应用行 → uid:10780
    # （直接注入缓存，_resolve_pkg_uid 的 pm 路径由上面的 redacted 测试覆盖）
    sampler._pkg_uid = "10780"
    assert sampler._read_ts_blocks(ctx) == {
        ("com.perfeval.gamebox/com.perfeval.gamebox.activity.HomePageActivity$_32369#3127", "", "10780"): 124,
        ("none", "", "10780"): 0,  # 脱敏层名字面 none（11-03 真机实测）
    }

    # uid 不可用 + 真机 packageName 全空 → 无可靠归属键 → None（不出假零；
    # 旧「图层名子串」辅路径已随 2026-11-06 修复移除）
    sampler2 = FpsSampler(interval_ms=500)
    sampler2._pkg_uid = None
    assert sampler2._read_ts_blocks(ctx) is None

    # uid 不匹配的应用（目标包不在 dump/uid 表）→ 无命中 → None（不出假零）
    sampler3 = FpsSampler(interval_ms=500)
    sampler3._pkg_uid = "10800"
    other_ctx = SimpleNamespace(
        adb=adb,
        command_runner=lambda command, timeout=None: adb.run(*command[1:], serial=None),
        serial="S1",
        package_name="com.other.app",
        elapsed_ms=lambda: 0,
    )
    assert sampler3._read_ts_blocks(other_ctx) is None


# ---- SM_F9560 多图层形态样本（2026-11-06 修复回归，块模板固化真机 dump）----


_MULTILAYER_FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "real_sm9560_timestats_multilayer.txt"
)


def _real_multilayer_ctx(adb: _RouteAdb) -> SimpleNamespace:
    return SimpleNamespace(
        adb=adb,
        command_runner=lambda command, timeout=None: adb.run(*command[1:], serial=None),
        serial="S1",
        package_name="com.perfeval.gamebox",
        elapsed_ms=lambda: 0,
    )


def test_fps_timestats_multilayer_blocks_dedupe_and_filter() -> None:
    """SM_F9560 形态样本逐块解析：同身份重复块去重取 max、子包/系统层排除。"""
    from app.services.fps_sampler import FpsSampler

    real_dump = _MULTILAYER_FIXTURE.read_text(encoding="utf-8")

    def handler(joined: str) -> tuple[str, int]:
        if "--timestats" in joined and "-dump" in joined:
            return real_dump, 0
        if "pm" in joined and "-U" in joined:
            return "package:com.perfeval.gamebox uid:10780\n", 0
        return "", 0

    adb = _RouteAdb(handler)
    s = FpsSampler()
    ctx = _real_multilayer_ctx(adb)
    assert s._read_ts_blocks(ctx) == {
        # 主窗口层：同一 (uid, layerName) 出现 2 块（2026-11-04 真机实测形态）
        # → 去重取 max，不求和（旧实现双计 248）
        ("com.perfeval.gamebox/com.perfeval.gamebox.activity.HomePageActivity$_32369#3127", "", "10780"): 124,
        # 同 uid 的 SurfaceView 层（多图层求和 bug 的真实形态）
        ("SurfaceView[com.perfeval.gamebox](BLAST)#3128", "", "10780"): 236,
        # 脱敏空图层名块如实保留（0 帧）
        ("", "", "10780"): 0,
    }
    # 未命中：子包层（uid 10801——旧前缀子串会把 com.perfeval.gamebox.bilibili 吸进来）
    # 与系统 NotificationShade 层（uid 10045，重复块 ×2 去重为 55）

    # uid 恰为子包 uid：命中的是子包层（uid 归属优先于名字相似性）
    s_sub = FpsSampler()
    s_sub._pkg_uid = "10801"
    assert s_sub._read_ts_blocks(ctx) == {
        ("com.perfeval.gamebox.bilibili/com.perfeval.gamebox.bilibili.player.PlayerActivity#200",
         "com.perfeval.gamebox.bilibili", "10801"): 180,
    }


def test_fps_timestats_sm9560_multilayer_dump_active_layer_only() -> None:
    """SM_F9560 形态样本端到端：逐块差分取最活跃层，不跨块求和。

    样本固化 2026-11-04 真机形态（同 uid 多图层、子包图层、同块身份重复块、
    脱敏空层名）：旧「uid OR 子串匹配 + 跨块求和」在本样本差分得 486fps
    （>120Hz 刷新率，PD_20261104_145547 实测 358~361 的同形态根因）。
    """
    from app.services.fps_sampler import FpsSampler

    real_dump = _MULTILAYER_FIXTURE.read_text(encoding="utf-8")
    # 第 1 轮基准：各层累计调低（重复块同步替换，保持重复形态）
    dump1 = (
        real_dump
        .replace("totalFrames = 124", "totalFrames = 24")  # 主窗口层 ×2（重复块）
        .replace("totalFrames = 236", "totalFrames = 50")  # SurfaceView 层
        .replace("totalFrames = 180", "totalFrames = 30")  # 子包层
        .replace("totalFrames = 55", "totalFrames = 11")   # NotificationShade ×2
    )
    dumps = iter([dump1, real_dump, real_dump])

    def handler(joined: str) -> tuple[str, int]:
        if "--timestats" in joined:
            if "-dump" in joined:
                return next(dumps), 0
            return "", 0  # -clear/-enable/-disable
        if "pm" in joined and "-U" in joined:
            return "package:com.perfeval.gamebox uid:10780\n", 0
        return "", 0

    adb = _RouteAdb(handler)
    clock = _Clock()
    s = FpsSampler(interval_ms=500, monotonic=clock)
    ctx = _real_multilayer_ctx(adb)
    s._escalate_timestats(ctx)  # -clear/-enable 并切 timestats 模式
    clock.advance(1.0)
    assert s.sample(ctx) == []  # 首轮建基准（逐块）
    clock.advance(1.0)
    records = s.sample(ctx)
    assert len(records) == 1
    rec = records[0]
    # 各层 Δ：主窗口 100、SurfaceView 186、脱敏空名 0 → 取最活跃层 186
    # （不求和 286；子包层 uid 不同不计——旧子串匹配会 +150）
    assert rec.value == 186.0
    assert rec.tags["layer"] == "SurfaceView[com.perfeval.gamebox](BLAST)#3128"
    assert rec.tags["source"] == "surfaceflinger"
    assert rec.tags["method"] == "timestats"
    assert rec.tags["unit"] == "fps"
    assert rec.tags["samplingWindowMs"] == 1000
    clock.advance(1.0)
    assert s.sample(ctx)[0].value == 0.0  # dump 不变 → 全零窗口如实出 0


def test_fps_timestats_no_matching_blocks_diagnostic_once() -> None:
    """升级 timestats 后无任何命中块 → 一条一次性诊断（不逐轮刷屏、不出假零）。"""
    from app.services.fps_sampler import FpsSampler

    def handler(joined: str) -> tuple[str, int]:
        if "--timestats" in joined:
            if "-dump" in joined:
                return "SurfaceFlinger TimeStats:\ntotalFrames = 0\n", 0
            return "", 0
        if "pm" in joined and "-U" in joined:
            return "package:com.example.game uid:10780\n", 0
        return "", 0

    adb = _RouteAdb(handler)
    clock = _Clock()
    s = FpsSampler(interval_ms=500, monotonic=clock)
    ctx = _sf_ctx(adb)
    s._escalate_timestats(ctx)  # -clear/-enable 并切 timestats 模式
    s.drain_diagnostics()  # 清掉升级诊断，聚焦无命中块诊断
    for _ in range(3):
        clock.advance(0.5)
        assert s.sample(ctx) == []
    assert s.drain_diagnostics() == ["fps.timestats.no_matching_blocks"]
    clock.advance(0.5)
    assert s.sample(ctx) == []
    assert s.drain_diagnostics() == []  # 一次性，不重复


# ---- 复核意见①/②回归（2026-11-06）----


def _multi_gfxinfo(sections: list[tuple[int, int]]) -> str:
    """多进程包的 `dumpsys gfxinfo <pkg>` 输出：每进程一段，各带独立计数行。"""
    return "".join(
        _gfxinfo(total).replace("pid 1234", f"pid {pid}") for pid, total in sections
    )


def test_fps_gfxinfo_multiprocess_sections_delta_not_sum() -> None:
    """复核意见①：gfxinfo 主通道对多进程包逐段差分取最活跃段，不跨段求和。

    各进程 HWUI 同受 vsync 门控，跨段求和与 timestats 跨块求和同型（N×刷新率）；
    CLI 侧 gfxinfo.py 按段圈定 scope 取单值的同型口径对齐。"""
    outputs = iter([
        _gfxinfo(0),                                    # start reset（单段，基线 0）
        _multi_gfxinfo([(1234, 100), (1300, 12)]),      # 基线后首窗
        _multi_gfxinfo([(1234, 163), (1300, 72)]),      # 第二窗
    ])
    adb = _RouteAdb(lambda joined: (next(outputs), 0))
    clock = _Clock()
    s = _sampler(adb, clock)
    ctx = _sf_ctx(adb)
    s.start(ctx)
    clock.advance(1.0)
    records = s.sample(ctx)
    assert len(records) == 1
    # Δ main=100、push=12 → 取最活跃段 100（旧跨段求和 = 112）
    assert records[0].value == 100.0
    assert records[0].tags["source"] == "gfxinfo"
    clock.advance(1.0)
    records = s.sample(ctx)
    # Δ main=63、push=60 → 取 63（旧跨段求和 = 123，超刷新率形态）
    assert records[0].value == 63.0


def test_fps_gfxinfo_process_restart_rebaselines_silently() -> None:
    """复核意见①回归：任一进程段计数回退（目标进程重启）→ 静默 re-baseline，
    该窗不出点；恢复后正常逐段差分。"""
    outputs = iter([
        _gfxinfo(0),                                  # start reset
        _multi_gfxinfo([(1234, 100), (1300, 12)]),    # 首窗 100fps
        _multi_gfxinfo([(1234, 5), (1300, 12)]),      # 主进程段回退（重启）→ re-baseline
        _multi_gfxinfo([(1234, 60), (1300, 72)]),     # 恢复：Δ main=55、push=60 → 60
    ])
    adb = _RouteAdb(lambda joined: (next(outputs), 0))
    clock = _Clock()
    s = _sampler(adb, clock)
    ctx = _sf_ctx(adb)
    s.start(ctx)
    clock.advance(1.0)
    assert s.sample(ctx)[0].value == 100.0
    clock.advance(1.0)
    assert s.sample(ctx) == []  # 回退轮：重建基准
    clock.advance(1.0)
    records = s.sample(ctx)
    assert len(records) == 1
    assert records[0].value == 60.0


def test_fps_timestats_all_new_identities_window_skips_point() -> None:
    """复核意见②：timestats 命中块全部换新身份（UI 应用切 Activity 换 layerName
    且无持久层）——无窗口内差分基线，真实 fps 未知，按读失败同径不出点（不产
    单点假 0），仅重建基线供下一窗口差分。"""
    from app.services.fps_sampler import FpsSampler

    set_a = (
        "SurfaceFlinger TimeStats:\n"
        "uid = 10780\n"
        "layerName = com.example.game/com.example.game.HomeActivity\n"
        "packageName = \n"
        "totalFrames = 100\n"
    )
    set_b_first = (
        "SurfaceFlinger TimeStats:\n"
        "uid = 10780\n"
        "layerName = com.example.game/com.example.game.GameActivity\n"
        "packageName = \n"
        "totalFrames = 30\n"
    )
    set_b_next = set_b_first.replace("totalFrames = 30", "totalFrames = 60")
    outputs = iter([set_a, set_b_first, set_b_next])

    def handler(joined: str) -> tuple[str, int]:
        if "--timestats" in joined:
            if "-dump" in joined:
                return next(outputs), 0
            return "", 0
        if "pm" in joined and "-U" in joined:
            return "package:com.example.game uid:10780\n", 0
        return "", 0

    adb = _RouteAdb(handler)
    clock = _Clock()
    s = FpsSampler(interval_ms=500, monotonic=clock)
    ctx = _sf_ctx(adb)
    s._escalate_timestats(ctx)
    s.drain_diagnostics()
    clock.advance(1.0)
    assert s.sample(ctx) == []  # 首轮建基准（A 层 100）
    clock.advance(1.0)
    assert s.sample(ctx) == []  # 块全部换新身份（B 层）：无基线不出点（不出假 0）
    clock.advance(1.0)
    records = s.sample(ctx)
    assert len(records) == 1
    assert records[0].value == 30.0  # B 层 60-30：新基线已立，正常差分


# ---- 窗口接线（契约 §21.5，S2 2026-11-10：三通道按窗口过滤 + 不适用通道诊断）----


def test_sampler_by_metric_keys_subset_of_registry() -> None:
    """一致性（缺口 notes ④）：SAMPLER_BY_METRIC 键 ⊆ registry.METRICS 键。

    防两流漂移：S2 登记不得先于/超出 S1 注册表行——S1 待落地映射
    （S1_PENDING_METRIC_MAPPINGS）在注册表行落地前保持 inert（不进活跃映射），
    S1 落地后自动激活并纳入本断言的子集校验。
    """
    from app.registry import METRICS
    from app.services.collector_service import (
        S1_PENDING_METRIC_MAPPINGS,
        SAMPLER_BY_METRIC,
    )

    registered = {spec.metric_id for spec in METRICS}
    assert set(SAMPLER_BY_METRIC) <= registered
    for metric_id in S1_PENDING_METRIC_MAPPINGS:
        if metric_id not in registered:
            assert metric_id not in SAMPLER_BY_METRIC  # 未落地 → inert


def _multi_gfxinfo_named(sections: list[tuple[int, int, str]]) -> str:
    """段头带进程名的 `dumpsys gfxinfo <pkg>` 输出（AOSP 形态 [process_name]）。"""
    return "".join(
        f"** Graphics info for pid {pid} [{name}] **\n"
        f"Total frames rendered: {total}\n"
        for pid, total, name in sections
    )


def test_fps_window_activity_gfxinfo_filters_subprocess() -> None:
    """activity 窗口：gfxinfo 子进程段（:push，非 Activity 承载进程）整段排除。"""
    outputs = iter([
        _multi_gfxinfo_named([(1234, 0, "com.example.game")]),        # start reset（主进程）
        _multi_gfxinfo_named([                                        # 首窗：子进程更活跃
            (1234, 100, "com.example.game"),
            (1300, 200, "com.example.game:push"),
        ]),
    ])
    adb = _RouteAdb(lambda joined: (next(outputs), 0))
    clock = _Clock()
    from app.services.fps_sampler import FpsSampler

    s = FpsSampler(interval_ms=500, monotonic=clock, fps_window={"type": "activity"})
    ctx = _sf_ctx(adb)
    s.start(ctx)
    assert "fps.window.activity.applied" in s.drain_diagnostics()
    clock.advance(1.0)
    records = s.sample(ctx)
    assert len(records) == 1
    # 子进程段（Δ200）被窗口过滤排除：仅主进程段差分 100（不过滤会取 200）
    assert records[0].value == 100.0
    assert records[0].tags["fpsWindow"] == "activity"
    assert records[0].tags["source"] == "gfxinfo"


def test_fps_window_surface_view_starts_sf_channel() -> None:
    """surface_view 窗口：起步即走 SF 通道，图层发现/选层限定 SurfaceView 层。"""
    lat_calls = {"n": 0}

    def handler(joined: str) -> tuple[str, int]:
        if "--latency" in joined:
            if "SurfaceView" in joined:
                # 每次调用出新一批 60 个时间戳（16ms 一帧，向后推进）
                lat_calls["n"] += 1
                base = lat_calls["n"] * 60
                return _latency_output(
                    [100_000_000 + (base + i) * 16_000_000 for i in range(60)]
                ), 0
            return _latency_output([]), 0  # 组件层零帧（选层应避开）
        if "--list" in joined:
            return (
                "com.example.game/com.example.game.MainActivity#0\n"
                "SurfaceView[com.example.game]#0(BLAST)\n"
            ), 0
        return _gfxinfo(0), 0

    adb = _RouteAdb(handler)
    clock = _Clock()
    from app.services.fps_sampler import FpsSampler

    s = FpsSampler(interval_ms=500, monotonic=clock, fps_window={"type": "surface_view"})
    ctx = _sf_ctx(adb)
    s.start(ctx)
    drained = s.drain_diagnostics()
    assert "fps.window.surface_view.applied" in drained
    assert s._mode == "surfaceflinger"
    assert s._sf_layer == "SurfaceView[com.example.game]#0"
    # 起步即 SF：start 不做 gfxinfo reset（Display 侧通道接管）
    assert not any("gfxinfo" in " ".join(map(str, c)) for c in adb.commands)
    clock.advance(0.5)
    assert s.sample(ctx) == []  # 首轮建时间基准
    clock.advance(1.0)
    records = s.sample(ctx)
    assert len(records) == 1
    assert records[0].tags["source"] == "surfaceflinger"
    assert records[0].tags["method"] == "latency"
    assert records[0].tags["fpsWindow"] == "surface_view"
    assert records[0].value == 60.0  # 60 个新时间戳 / 1.0s


def test_fps_window_surface_view_timestats_filters_layers() -> None:
    """timestats 显式窗口过滤：surface_view 窗口只取 SurfaceView 块差分。"""
    dump_a = (
        "SurfaceFlinger TimeStats:\n"
        "uid = 10780\n"
        "layerName = com.example.game/com.example.game.MainActivity\n"
        "packageName = \n"
        "totalFrames = 500\n"
        "uid = 10780\n"
        "layerName = SurfaceView[com.example.game](BLAST)\n"
        "packageName = \n"
        "totalFrames = 100\n"
    )
    dump_b = dump_a.replace("totalFrames = 500", "totalFrames = 600").replace(
        "totalFrames = 100", "totalFrames = 160"
    )
    outputs = iter([dump_a, dump_b])

    def handler(joined: str) -> tuple[str, int]:
        if "--timestats" in joined:
            if "-dump" in joined:
                return next(outputs), 0
            return "", 0  # -clear/-enable/-disable
        if "pm" in joined and "-U" in joined:
            return "package:com.example.game uid:10780\n", 0
        return "", 0

    adb = _RouteAdb(handler)
    clock = _Clock()
    from app.services.fps_sampler import FpsSampler

    s = FpsSampler(interval_ms=500, monotonic=clock, fps_window={"type": "surface_view"})
    ctx = _sf_ctx(adb)
    s._escalate_timestats(ctx)
    clock.advance(1.0)
    assert s.sample(ctx) == []  # 首轮建基准（过滤后仅 SV 块 100）
    clock.advance(1.0)
    records = s.sample(ctx)
    assert len(records) == 1
    # 主窗口层 Δ=100（更活跃）被窗口过滤排除：仅 SV 层 Δ=60 出点
    assert records[0].value == 60.0
    assert records[0].tags["layer"] == "SurfaceView[com.example.game](BLAST)"
    assert records[0].tags["fpsWindow"] == "surface_view"
    assert records[0].tags["method"] == "timestats"


def test_fps_window_surface_view_no_layer_stays_gfxinfo_with_diagnostic() -> None:
    """surface_view 窗口无可匹配图层：保留 gfxinfo 通道 + 一次性诊断（窗口未应用，
    不适用通道保留 source/method tags）。"""

    def handler(joined: str) -> tuple[str, int]:
        if "--latency" in joined or "--list" in joined:
            return "com.example.game/com.example.game.MainActivity#0\n", 0
        return _gfxinfo(next(totals)), 0

    totals = iter([0, 63])
    adb = _RouteAdb(handler)
    clock = _Clock()
    from app.services.fps_sampler import FpsSampler

    s = FpsSampler(interval_ms=500, monotonic=clock, fps_window={"type": "surface_view"})
    ctx = _sf_ctx(adb)
    s.start(ctx)
    assert "fps.window.surface_view.no_layer" in s.drain_diagnostics()
    assert s._mode == "gfxinfo"  # 窗口未应用：保留 gfxinfo 通道
    clock.advance(1.0)
    records = s.sample(ctx)
    assert len(records) == 1
    assert records[0].tags["source"] == "gfxinfo"
    assert records[0].tags["fpsWindow"] == "surface_view"  # 请求窗口如实标注
    assert records[0].value == 63.0


def test_collector_delivers_fps_window_to_sampler() -> None:
    """契约 §21.5（S2 接线）：resolve_fps_window 结果经 create_collector 新参
    送达 FpsSampler（fpsWindow entry → 工厂 fps_window 参数 → 窗口过滤生效）。"""
    from threading import Lock

    from app.services.collector_service import RealCollector
    from app.sse import SseBus

    class _Sess:
        id = "sess-win"
        device_id = "fake-real-001"
        metrics = {"frame.fps.mean": 500}
        fps_window_resolved = "surface_view"
        package_name = "com.example.game"
        process_name = None

    adb = _RouteAdb(lambda joined: ("", 0))
    # 新参显式送达路径
    collector = RealCollector(
        _Sess(), SseBus("sess-win"), adb=adb, fps_window_resolved="surface_view"
    )
    collector._enabled_lock = Lock()
    scheduler = collector._build_scheduler()
    fps_samplers = [s for s in scheduler.samplers if getattr(s, "name", None) == "fps"]
    assert len(fps_samplers) == 1
    assert fps_samplers[0]._window_type == "surface_view"
    # 未传参回退会话已解析属性（router 创建路径不传参）
    collector2 = RealCollector(_Sess(), SseBus("sess-win"), adb=adb)
    collector2._enabled_lock = Lock()
    scheduler2 = collector2._build_scheduler()
    fps_samplers2 = [s for s in scheduler2.samplers if getattr(s, "name", None) == "fps"]
    assert len(fps_samplers2) == 1
    assert fps_samplers2[0]._window_type == "surface_view"


def test_real_session_with_fps_metric_no_not_applied_preaudit(tmp_path, monkeypatch) -> None:
    """real + frame.fps.mean 已启用 + 显式窗口 → 不预广播 not_applied（窗口已接线，
    实际应用由采样层 diagnostic 报告）；未启用 FPS 指标时保留 not_applied 语义
    （既有用例 test_real_m6_paths.test_real_fps_window_diagnostic_not_applied，
    metrics=[] 路径）。"""
    from m4_fixtures import fake_screenrecord_runner, make_real_client

    runner = fake_screenrecord_runner([], {})
    with make_real_client(tmp_path, monkeypatch, recorder_command_runner=runner) as client:
        resp = client.post("/api/sessions", json={
            "deviceId": "fake-real-001",
            "packageName": "com.example.fakegame",
            "metrics": [{"metricId": "frame.fps.mean", "intervalMs": 500}],
            "interactive": False,
            "launchOnCreate": False,
            "fpsWindow": {"type": "surface_view"},
        })
        assert resp.status_code == 201, resp.text
        sid = resp.json()["id"]
        internal = client.app.state.sessions._sessions[sid]
        with internal.bus._lock:
            events = [dict(env) for _seq, env in internal.bus._ring]
        pre_audit = [
            e for e in events
            if e["type"] == "diagnostic" and e.get("code") == "fps_window.not_applied"
        ]
        assert pre_audit == []  # 已接线：不预广播（采样层报告实际应用）
        client.post(f"/api/sessions/{sid}/discard")
