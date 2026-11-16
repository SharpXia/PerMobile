"""realios_sampler/xctrace 窗口链路单测（R24 契约 §26.4/§26.5）：假 runner 驱动。

覆盖面（对应完成判据）：

- **runner 调用序列**：每次 record 前必含 devicectl 预热命令（lockState），
  export --toc/--xpath 两跳在 record 之后、.trace 用后即删（run 目录不落
  .trace 工件）；
- **PID attach 禁用**：全链路 xctrace 命令 ``--attach`` 值恒为进程名（非数字；
  勘察实测 PID attach → exit 21），record 命令模板形状按 §26.4 原文；
- **出点**：value 数值型且 tags 恒带 ``{"basis": "xctrace"}``；窗口锚点 =
  record 退出时刻宿主时钟回推 time-limit，行 ts = 锚点 + 表内 Start Time 偏移
  （fixture activity-monitor-process-live.xml 的真文法行）；
- **进程消失**：解析失败/窗口零数据行 → diagnostic(app.process.not_found)
  （锁存）+ 双指标 value:null 点（§26.5/§6.1）；
- **错误体**：xcrun 缺失 → ``xctrace_unavailable``（503，details.reason=
  xctrace_missing/devicectl_missing）；预热重试耗尽 → prewarm_failed；
  record/export 失败 → diagnostic(xctrace.window.failed)（severity error）。

XML 解析层另以 am-process-live-host-sentinel.xml（宿主真导出，cpu-percent 恒
sentinel）作负例 golden。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "realios"
AM_XML = (FIXTURES / "activity-monitor-process-live.xml").read_text()
HOST_SENTINEL_XML = (FIXTURES / "am-process-live-host-sentinel.xml").read_text()

UDID = "00008130-000955B7C2D1E84A"


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> Any:
    result = type("_R", (), {})()
    result.stdout = stdout
    result.returncode = returncode
    result.stderr = stderr
    return result


class _BusRecorder:
    """SSE 总线记录器（collector emit_* 全量捕获）。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def publish(self, event: str, **payload: Any) -> None:
        self.events.append((event, payload))

    def samples(self) -> list[dict[str, Any]]:
        return [payload for event, payload in self.events if event == "sample"]

    def diagnostics(self) -> list[dict[str, Any]]:
        return [payload for event, payload in self.events if event == "diagnostic"]


class _StubProvider:
    """provider 桩：设备行 + 进程名解析（隔离 provider 命令面；strict 探测面）。"""

    def __init__(
        self,
        state: str = "online",
        process: dict[str, Any] | None = None,
    ) -> None:
        self.state = state
        self.process = process

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        return {
            "id": device_id,
            "platform": "ios",
            "state": self.state,
            "connection": "usb",
        }

    def find_device_strict(self, device_id: str) -> dict[str, Any] | None:
        # strict 探测桩：state 恒权威（None=探测失败抛错由专门桩承担）
        return self.get_device(device_id)

    def resolve_process(
        self, device_id: str, package_name: str
    ) -> dict[str, Any] | None:
        return self.process

    def resolve_process_strict(
        self, device_id: str, package_name: str
    ) -> dict[str, Any] | None:
        return self.process


def _make_session(tmp_path: Path, metrics: list[dict[str, Any]]) -> Any:
    from app.services.session_manager import Session

    return Session(
        session_id="sess-r24",
        device_id=UDID,
        package_name="com.perfeval.demo",
        metrics=metrics,
        provider_mode="real",
        created_at=1,  # 窗口 ts 为未来 epoch ms → to_wall_ms 原样透传
        runs_dir=tmp_path / "runs",
    )


def _make_collector(
    tmp_path: Path,
    bus: _BusRecorder,
    session: Any,
    *,
    provider: _StubProvider | None = None,
    devicectl_runner: Any = None,
    xctrace_runner: Any = None,
    epoch_holder: dict[str, float] | None = None,
    window_s: float = 4.0,
    process_name: str | None = "Runner",
) -> Any:
    from app.services.realios_sampler import RealIOSCollector

    holder = epoch_holder if epoch_holder is not None else {"now": 1_900_000_000.0}
    provider = provider if provider is not None else _StubProvider(
        process={"pid": 15436, "name": process_name} if process_name else None
    )
    # runner 缺省恒注入假实现（None 会落到生产 subprocess，测试不得触真机）
    default_journal: list[tuple[str, list[str]]] = []
    if devicectl_runner is None or xctrace_runner is None:
        default_devicectl, default_xctrace = _window_runners(
            tmp_path, default_journal
        )
        devicectl_runner = devicectl_runner or default_devicectl
        xctrace_runner = xctrace_runner or default_xctrace
    collector = RealIOSCollector(
        session,
        bus,
        provider=provider,
        window_s=window_s,
        devicectl_runner=devicectl_runner,
        xctrace_runner=xctrace_runner,
        work_dir_factory=lambda prefix="": _mkdtemp_recorded(tmp_path, prefix),
        epoch_clock=lambda: holder["now"],
    )
    collector._process_resolver = lambda: (
        provider.process["name"] if provider.process else None
    )
    return collector


_WORKDIRS: list[Path] = []


def _mkdtemp_recorded(tmp_path: Path, prefix: str) -> str:
    import tempfile

    path = tempfile.mkdtemp(prefix=prefix, dir=str(tmp_path))
    _WORKDIRS.append(Path(path))
    return path


def _window_runners(
    tmp_path: Path,
    journal: list[tuple[str, list[str]]],
    *,
    lockstate_failures: int = 0,
    record_returncode: int = 0,
    record_stderr: str = "",
    table_xml: str = AM_XML,
    xcrun_missing: bool = False,
    fail_export: bool = False,
) -> tuple[Any, Any]:
    """假 devicectl/xctrace runner：模拟命令面行为并记 journal（时序断言用）。"""
    state = {"lockstate_calls": 0}

    def devicectl_runner(command: list[str], timeout: float) -> Any:
        journal.append(("devicectl", command[2:]))
        if command[2:4] == ["device", "info"] and len(command) > 4 and command[4] == "lockState":
            state["lockstate_calls"] += 1
            if state["lockstate_calls"] <= lockstate_failures:
                return _completed("", returncode=1, stderr="device locked")
            return _completed('{"result": {}}')
        return _completed("")

    def xctrace_runner(command: list[str], timeout: float) -> Any:
        journal.append(("xctrace", command[2:]))
        if xcrun_missing:
            raise FileNotFoundError("xcrun")
        args = command[2:]
        if args[0] == "record":
            if record_returncode != 0:
                return _completed(record_stderr, returncode=record_returncode,
                                  stderr=record_stderr)
            output = command[command.index("--output") + 1]
            Path(output).write_bytes(b"PK trace bundle")  # .trace 落盘模拟
            return _completed("")
        if args[0] == "export":
            if fail_export:
                return _completed("", returncode=1, stderr="export boom")
            output = command[command.index("--output") + 1]
            if "--toc" in args:
                Path(output).write_text("<trace-toc/>")
            else:
                Path(output).write_text(table_xml)
            return _completed("")
        return _completed("")

    return devicectl_runner, xctrace_runner


# ---- XML 解析层（fixture golden）----


def test_parse_process_live_xml_golden_fixture() -> None:
    """真文法行 → 双序列（CPU% + footprint MiB）；offset 为表内 ns→ms。"""
    from app.providers.xctrace import parse_process_live_xml

    rows = parse_process_live_xml(AM_XML)
    assert len(rows) == 4
    assert [r["offset_ms"] for r in rows] == [0, 1004, 2010, 3014]
    assert all(r["pid"] == 15436 for r in rows)
    assert all(r["process"] == "Runner" for r in rows)
    assert [r["cpu_pct"] for r in rows] == [12.34, 45.6, 8.9, 33.3]
    # physical footprint：31342592 B = 29.89 MiB（勘察真值形态）/1024 口径
    assert [r["footprint_mb"] for r in rows] == [29.9, 30.0, 30.4, 30.8]


def test_parse_process_live_xml_host_sentinel_negative() -> None:
    """宿主真导出（cpu-percent 恒 sentinel）：cpu_pct=None 不猜，footprint 照读。"""
    from app.providers.xctrace import parse_process_live_xml

    rows = parse_process_live_xml(HOST_SENTINEL_XML)
    assert len(rows) == 1
    assert rows[0]["cpu_pct"] is None  # sentinel → None（宿主录制实测形态）
    assert rows[0]["pid"] == 71981  # ref 引用先行单元格解析（CPU 满载 yes 进程）
    assert rows[0]["process"] == "yes"


def test_parse_process_live_xml_garbage() -> None:
    from app.providers.xctrace import parse_process_live_xml

    assert parse_process_live_xml("not xml") == []
    assert parse_process_live_xml("<trace-query-result><node/></trace-query-result>") == []


def test_parse_process_live_xml_device_cross_row_refs() -> None:
    """真机 trace（iOS 27.x 实测导出，2026-11-16）：单元格 ref **跨行共享**——
    `<start-time ref="1"/>` 引用首行定义的 id。按行内建索引曾使 1676 行只幸存
    6 行自包含行（真机 E2E 实锤），修复=文档级 ref 索引；本 fixture 取真机原文
    前两行（row1 引用 row0 定义的 id）作回归锚。"""
    from app.providers.xctrace import parse_process_live_xml

    device_xml = (FIXTURES / "am-process-live-device-crossref.xml").read_text()
    rows = parse_process_live_xml(device_xml)
    assert len(rows) == 2
    assert rows[0]["process"] == "kernel"
    assert rows[1]["process"] == "nanoweatherprefsd"  # ref 跨行解析成功的证据
    assert rows[1]["pid"] == 15603
    assert all(r["offset_ms"] is not None for r in rows)


# ---- 命令构造（§26.4 原文形态 + PID attach 禁用）----


def test_build_record_command_shape() -> None:
    from app.providers.xctrace import build_record_command

    # 2026-11-16 真机实测修订：attach 枚举不可用（exit 19）→ --all-processes
    command = build_record_command(UDID, 4.0, "/tmp/w.trace")
    assert command == [
        "record",
        "--template", "Activity Monitor",
        "--device", UDID,
        "--all-processes",
        "--time-limit", "4s",
        "--no-prompt",
        "--output", "/tmp/w.trace",
    ]
    from app.providers.xctrace import build_export_command

    # --toc/--xpath 互斥两模式（--help 实测）；xpath 形态固定指向目标表
    assert build_export_command("/t.trace", toc=True) == [
        "export", "--input", "/t.trace", "--toc",
    ]
    assert build_export_command("/t.trace") == [
        "export", "--input", "/t.trace",
        "--xpath",
        '/trace-toc/run[@number="1"]/data/table'
        '[@schema="activity-monitor-process-live"]',
    ]


# ---- 窗口链路（假 runner：序列/预热/清理）----


def test_window_chain_sequence_and_prewarm_before_record(tmp_path: Path) -> None:
    """runner 调用序列：每次 record 前必含 devicectl 预热（lockState）命令。"""
    from app.providers.xctrace import run_sample_window

    journal: list[tuple[str, list[str]]] = []
    devicectl_runner, xctrace_runner = _window_runners(tmp_path, journal)
    holder = {"now": 1_900_000_000.0}
    result = run_sample_window(
        UDID, "Runner", 4.0,
        work_dir=str(tmp_path / "win"),
        devicectl_runner=devicectl_runner,
        xctrace_runner=xctrace_runner,
        epoch_clock=lambda: holder["now"],
    )
    kinds = [kind for kind, _ in journal]
    record_indexes = [i for i, c in enumerate(journal) if c[1] and c[1][0] == "record"]
    assert record_indexes, "record 命令必须发出"
    for index in record_indexes:
        prewarm = [
            c for c in journal[:index]
            if c[0] == "devicectl" and "lockState" in c[1]
        ]
        assert prewarm, "每次 record 前必有 devicectl lockState 预热命令"
    # record 之后 export --toc → export --xpath（§26.4 链路）
    first_record = record_indexes[0]
    assert journal[first_record + 1][1][0] == "export"
    assert "--toc" in journal[first_record + 1][1]
    assert journal[first_record + 2][1][0] == "export"
    assert "--xpath" in journal[first_record + 2][1]
    # record 命令形状（§26.4，2026-11-16 修订：--all-processes + 解析后过滤）
    record_cmd = journal[first_record][1]
    assert record_cmd == [
        "record", "--template", "Activity Monitor", "--device", UDID,
        "--all-processes", "--time-limit", "4s", "--no-prompt",
        "--output", str(tmp_path / "win" / "window.trace"),
    ]
    assert "--attach" not in record_cmd, "attach 枚举真机不可用，不得回退"
    # 窗口锚点 = record 退出时刻回推 time-limit（§26.4）
    assert result["windowStartEpochMs"] == int(holder["now"] * 1000) - 4000
    assert result["recordExitCode"] == 0
    # 目标进程过滤：fixture 含 Runner+SpringBoard 双进程行，只留 Runner
    assert len(result["rows"]) == 4
    assert {row["process"] for row in result["rows"]} == {"Runner"}
    # .trace 用后即删（§26.4：run 目录不落 .trace 工件）
    assert not (tmp_path / "win" / "window.trace").exists()


def test_window_chain_prewarm_retry_then_success(tmp_path: Path) -> None:
    """锁屏瞬时失败 → lockState 重试（§26.4）；成功后 record 照常。"""
    from app.providers.xctrace import run_sample_window

    journal: list[tuple[str, list[str]]] = []
    devicectl_runner, xctrace_runner = _window_runners(
        tmp_path, journal, lockstate_failures=2
    )
    result = run_sample_window(
        UDID, "Runner", 4.0,
        work_dir=str(tmp_path / "win"),
        devicectl_runner=devicectl_runner,
        xctrace_runner=xctrace_runner,
        epoch_clock=lambda: 1_900_000_000.0,
        prewarm_attempts=3,
    )
    lockstate_calls = [
        c for c in journal if c[0] == "devicectl" and "lockState" in c[1]
    ]
    assert len(lockstate_calls) == 3  # 失败 2 次 + 成功 1 次
    assert len(result["rows"]) == 4


def test_window_chain_prewarm_exhausted_error_body(tmp_path: Path) -> None:
    """预热重试耗尽 → 503 xctrace_unavailable（details.reason=prewarm_failed）。"""
    from app.errors import ApiError
    from app.providers.xctrace import run_sample_window

    journal: list[tuple[str, list[str]]] = []
    devicectl_runner, xctrace_runner = _window_runners(
        tmp_path, journal, lockstate_failures=99
    )
    with pytest.raises(ApiError) as excinfo:
        run_sample_window(
            UDID, "Runner", 4.0,
            work_dir=str(tmp_path / "win"),
            devicectl_runner=devicectl_runner,
            xctrace_runner=xctrace_runner,
            epoch_clock=lambda: 1_900_000_000.0,
            prewarm_attempts=3,
        )
    body = excinfo.value.to_body()["error"]
    assert body["code"] == "xctrace_unavailable"
    assert excinfo.value.status == 503
    assert body["details"]["reason"] == "prewarm_failed"
    assert body["details"]["attempts"] == 3
    # 失败窗口不出 record（预热先行拦截）
    assert not [c for c in journal if c[0] == "xctrace"]


def test_window_chain_xcrun_missing_error_bodies(tmp_path: Path) -> None:
    """xcrun 本体缺失两态（§3）：devicectl_missing / xctrace_missing，均 503。"""
    from app.errors import ApiError
    from app.providers.xctrace import run_sample_window

    def missing_devicectl(command: list[str], timeout: float) -> Any:
        raise FileNotFoundError("xcrun")

    with pytest.raises(ApiError) as excinfo:
        run_sample_window(
            UDID, "Runner", 4.0,
            work_dir=str(tmp_path / "win"),
            devicectl_runner=missing_devicectl,
            xctrace_runner=lambda c, t: _completed(""),
        )
    assert excinfo.value.to_body()["error"]["details"]["reason"] == "devicectl_missing"

    journal: list[tuple[str, list[str]]] = []
    devicectl_runner, xctrace_runner = _window_runners(
        tmp_path, journal, xcrun_missing=True
    )
    with pytest.raises(ApiError) as excinfo:
        run_sample_window(
            UDID, "Runner", 4.0,
            work_dir=str(tmp_path / "win"),
            devicectl_runner=devicectl_runner,
            xctrace_runner=xctrace_runner,
        )
    assert excinfo.value.to_body()["error"]["details"]["reason"] == "xctrace_missing"
    assert excinfo.value.status == 503


def test_window_chain_record_pid_reject_exit21(tmp_path: Path) -> None:
    """record 非零退出（勘察 PID attach 实测 exit 21 形态）→ record_failed+exit code。"""
    from app.providers.xctrace import XctraceWindowError, run_sample_window

    journal: list[tuple[str, list[str]]] = []
    devicectl_runner, xctrace_runner = _window_runners(
        tmp_path, journal,
        record_returncode=21,
        record_stderr="Cannot find process for provided pid: 15436",
    )
    with pytest.raises(XctraceWindowError) as excinfo:
        run_sample_window(
            UDID, "Runner", 4.0,
            work_dir=str(tmp_path / "win"),
            devicectl_runner=devicectl_runner,
            xctrace_runner=xctrace_runner,
        )
    assert excinfo.value.reason == "record_failed"
    assert excinfo.value.exit_code == 21
    # 失败后不 export、trace 已清理
    assert not [c for c in journal if c[0] == "xctrace" and c[1] and c[1][0] == "export"]
    assert not (tmp_path / "win" / "window.trace").exists()


def test_window_chain_export_failure(tmp_path: Path) -> None:
    from app.providers.xctrace import XctraceWindowError, run_sample_window

    journal: list[tuple[str, list[str]]] = []
    devicectl_runner, xctrace_runner = _window_runners(
        tmp_path, journal, fail_export=True
    )
    with pytest.raises(XctraceWindowError) as excinfo:
        run_sample_window(
            UDID, "Runner", 4.0,
            work_dir=str(tmp_path / "win"),
            devicectl_runner=devicectl_runner,
            xctrace_runner=xctrace_runner,
        )
    assert excinfo.value.reason == "export_failed"
    assert not (tmp_path / "win" / "window.trace").exists()


# ---- RealIOSCollector（出点/锚点/诊断/null 点）----


def _metric_session(tmp_path: Path) -> Any:
    return _make_session(tmp_path, [
        {"metricId": "cpu.proc.xctrace.pct", "intervalMs": 1000},
        {"metricId": "memory.footprint.mb", "intervalMs": 2000},
    ])


def test_collector_window_batch_points_tags_and_anchor(tmp_path: Path) -> None:
    """窗口批出点：value 数值型、tags.basis=xctrace、ts=窗口锚点+表内偏移。"""
    bus = _BusRecorder()
    session = _metric_session(tmp_path)
    journal: list[tuple[str, list[str]]] = []
    devicectl_runner, xctrace_runner = _window_runners(tmp_path, journal)
    holder = {"now": 1_900_000_000.0}
    collector = _make_collector(
        tmp_path, bus, session,
        devicectl_runner=devicectl_runner,
        xctrace_runner=xctrace_runner,
        epoch_holder=holder,
    )
    collector._on_start()
    collector._run_window()

    samples = bus.samples()
    assert len(samples) == 8  # 4 行 × 双指标（窗口批，~1s 粒度）
    anchor = int(holder["now"] * 1000) - 4000
    cpu_ts = [s["tsMs"] for s in samples if s["metricId"] == "cpu.proc.xctrace.pct"]
    mem_ts = [s["tsMs"] for s in samples if s["metricId"] == "memory.footprint.mb"]
    assert cpu_ts == [anchor + 0, anchor + 1004, anchor + 2010, anchor + 3014]
    assert mem_ts == cpu_ts  # 同行双指标同 ts
    assert all(isinstance(s["value"], (int, float)) for s in samples)
    assert all(s["tags"] == {"basis": "xctrace"} for s in samples)
    cpu_values = [s["value"] for s in samples if s["metricId"] == "cpu.proc.xctrace.pct"]
    assert cpu_values == [12.34, 45.6, 8.9, 33.3]
    mem_values = [s["value"] for s in samples if s["metricId"] == "memory.footprint.mb"]
    assert mem_values == [29.9, 30.0, 30.4, 30.8]
    # 会话缓冲同源（落盘数据面）
    assert len(session._records) == 8
    # 时钟锚点（§26.4）：host_epoch + 窗口批语义声明
    assert session.sampler_clock_anchor["source"] == "host_epoch"
    assert "xctrace window batch" in session.sampler_clock_anchor["method"]
    # 窗口结束工作目录清理（.trace 不落 run 目录）
    assert all(not d.exists() for d in _WORKDIRS)


def test_collector_process_missing_null_points_and_latch(tmp_path: Path) -> None:
    """进程消失：app.process.not_found 诊断锁存 + 双指标 value:null（§26.5/§6.1）。"""
    bus = _BusRecorder()
    session = _metric_session(tmp_path)
    journal: list[tuple[str, list[str]]] = []
    devicectl_runner, xctrace_runner = _window_runners(tmp_path, journal)
    collector = _make_collector(
        tmp_path, bus, session,
        provider=_StubProvider(process=None),  # 解析失败 = 目标进程不存在
        devicectl_runner=devicectl_runner,
        xctrace_runner=xctrace_runner,
    )
    collector._run_window()
    diagnostics = bus.diagnostics()
    assert len(diagnostics) == 1
    assert diagnostics[0]["code"] == "app.process.not_found"
    assert diagnostics[0]["severity"] == "warn"
    nulls = [s for s in bus.samples() if s["value"] is None]
    assert len(nulls) == 2  # 双指标各 1 null 点
    assert all(s["tags"] == {"basis": "xctrace"} for s in nulls)
    # 全程无 xctrace 命令（进程名都拿不到，不进录制）
    assert not [c for c in journal if c[0] == "xctrace"]
    # 第二窗：诊断不重复（锁存），null 点按窗再出（§26.5 该窗语义）
    collector._run_window()
    diagnostics = [
        d for d in bus.diagnostics() if d["code"] == "app.process.not_found"
    ]
    assert len(diagnostics) == 1
    assert len([s for s in bus.samples() if s["value"] is None]) == 4


def test_collector_empty_rows_treated_as_process_vanished(tmp_path: Path) -> None:
    """record 成功但零数据行（录制中进程消失窗口）→ app.process.not_found + null。"""
    bus = _BusRecorder()
    session = _metric_session(tmp_path)
    journal: list[tuple[str, list[str]]] = []
    devicectl_runner, xctrace_runner = _window_runners(
        tmp_path, journal,
        table_xml=(
            '<?xml version="1.0"?><trace-query-result><node>'
            "<schema"
            ' name="activity-monitor-process-live">'
            "<col><mnemonic>start</mnemonic></col></schema>"
            "<data></data></node></trace-query-result>"
        ),
    )
    collector = _make_collector(
        tmp_path, bus, session,
        devicectl_runner=devicectl_runner,
        xctrace_runner=xctrace_runner,
    )
    collector._run_window()
    assert any(
        d["code"] == "app.process.not_found" for d in bus.diagnostics()
    )
    assert len([s for s in bus.samples() if s["value"] is None]) == 2
    assert not [s for s in bus.samples() if s["value"] is not None]


def test_collector_record_failure_diagnostic(tmp_path: Path) -> None:
    """record 失败 → xctrace.window.failed（error，details.reason/exitCode），无点。"""
    bus = _BusRecorder()
    session = _metric_session(tmp_path)
    journal: list[tuple[str, list[str]]] = []
    devicectl_runner, xctrace_runner = _window_runners(
        tmp_path, journal, record_returncode=19,
        record_stderr="Cannot find process matching name: Runner",
    )
    collector = _make_collector(
        tmp_path, bus, session,
        devicectl_runner=devicectl_runner,
        xctrace_runner=xctrace_runner,
    )
    collector._run_window()
    diagnostics = bus.diagnostics()
    assert len(diagnostics) == 1
    assert diagnostics[0]["code"] == "xctrace.window.failed"
    assert diagnostics[0]["severity"] == "error"
    assert diagnostics[0]["detail"]["reason"] == "record_failed"
    assert diagnostics[0]["detail"]["exitCode"] == 19
    assert bus.samples() == []  # 该窗不出点（[R§8.6] 不造数）


def test_collector_prewarm_failure_diagnostic(tmp_path: Path) -> None:
    """预热耗尽 → xctrace_unavailable 诊断（error，reason=prewarm_failed）。"""
    bus = _BusRecorder()
    session = _metric_session(tmp_path)
    journal: list[tuple[str, list[str]]] = []
    devicectl_runner, xctrace_runner = _window_runners(
        tmp_path, journal, lockstate_failures=99
    )
    collector = _make_collector(
        tmp_path, bus, session,
        devicectl_runner=devicectl_runner,
        xctrace_runner=xctrace_runner,
    )
    collector._run_window()
    diagnostics = bus.diagnostics()
    assert len(diagnostics) == 1
    assert diagnostics[0]["code"] == "xctrace_unavailable"
    assert diagnostics[0]["severity"] == "error"
    assert diagnostics[0]["detail"]["reason"] == "prewarm_failed"
    assert bus.samples() == []


def test_collector_tunnel_offline_no_points(tmp_path: Path) -> None:
    """会话中途失联（tunnelState 离线）→ device.tunnel.offline，该窗不出点。"""
    bus = _BusRecorder()
    session = _metric_session(tmp_path)
    journal: list[tuple[str, list[str]]] = []
    devicectl_runner, xctrace_runner = _window_runners(tmp_path, journal)
    collector = _make_collector(
        tmp_path, bus, session,
        provider=_StubProvider(state="offline", process={"pid": 1, "name": "Runner"}),
        devicectl_runner=devicectl_runner,
        xctrace_runner=xctrace_runner,
    )
    collector._run_window()
    diagnostics = bus.diagnostics()
    assert len(diagnostics) == 1
    assert diagnostics[0]["code"] == "device.tunnel.offline"
    assert diagnostics[0]["severity"] == "error"
    assert bus.samples() == []
    # 离线窗不起录制链路
    assert not [c for c in journal if c[0] == "xctrace"]


def test_collector_skips_window_without_target_metrics(tmp_path: Path) -> None:
    """会话未勾双指标 → 不起 xctrace 链路（无命令面承诺）。"""
    bus = _BusRecorder()
    session = _make_session(tmp_path, [
        {"metricId": "cpu.total.pct", "intervalMs": 1000},  # Android 行，真机 ios 不勾
    ])
    journal: list[tuple[str, list[str]]] = []
    devicectl_runner, xctrace_runner = _window_runners(tmp_path, journal)
    collector = _make_collector(
        tmp_path, bus, session,
        devicectl_runner=devicectl_runner,
        xctrace_runner=xctrace_runner,
    )
    collector._run_window()
    assert journal == []
    assert bus.samples() == []


def test_collector_explicit_process_name_bypasses_resolution(tmp_path: Path) -> None:
    """session.process_name 显式提供 → 直通录制并作过滤目标（解析桩不可用也照录）。"""
    from app.services.realios_sampler import RealIOSCollector

    bus = _BusRecorder()
    session = _metric_session(tmp_path)
    session.process_name = "SpringBoard"  # fixture 双进程行——显式名决定过滤目标
    journal: list[tuple[str, list[str]]] = []
    devicectl_runner, xctrace_runner = _window_runners(tmp_path, journal)
    provider = _StubProvider(process=None)  # 解析桩不可用——显式名必须直通
    collector = RealIOSCollector(
        session, bus, provider=provider, window_s=4.0,
        devicectl_runner=devicectl_runner, xctrace_runner=xctrace_runner,
        work_dir_factory=lambda prefix="": _mkdtemp_recorded(tmp_path, prefix),
    )
    collector._process_resolver = collector._resolve_attach_name  # 走生产解析路径
    collector._run_window()
    record_cmds = [c for c in journal if c[0] == "xctrace" and c[1][0] == "record"]
    assert record_cmds, "显式进程名会话必须照常录制（解析桩不可用也被旁路）"
    # 过滤按显式名执行：fixture 内 SpringBoard 行有真值 → 出点在场
    assert bus.samples(), "显式名过滤后应有该进程的出点"


def test_collector_window_scheduling(tmp_path: Path) -> None:
    """调度：首窗即刻；窗口间隔 = window_s + gap（滚动窗节拍）。"""
    bus = _BusRecorder()
    session = _metric_session(tmp_path)
    collector = _make_collector(tmp_path, bus, session, window_s=4.0)
    collector._window_s = 4.0
    collector._window_gap_s = 1.0
    now = 100.0
    collector._emit_due(now)  # 首窗即刻（_next_window_at 初始 0.0）
    assert collector._next_window_at == pytest.approx(105.0)
    # 窗口内不再触发（窗口批串行语义）
    before = len(bus.samples())
    collector._emit_due(now + 2.0)
    assert len(bus.samples()) == before


def test_collector_window_failure_backoff(tmp_path: Path) -> None:
    """失败窗退避（2026-11-16 真机 E2E 实测）：record 失败仍按固定 5s 连发会把
    CoreDevice 隧道打挂（14 连发 "Timed out waiting for device to boot" 实锤）
    ——失败窗 gap 翻倍封顶 60s 给隧道恢复窗，成功一窗即复位原节奏。"""
    from app.services.realios_sampler import RealIOSCollector, _FAILURE_BACKOFF_CAP_S

    bus = _BusRecorder()
    session = _metric_session(tmp_path)
    journal: list[tuple[str, list[str]]] = []
    fail_devicectl, fail_xctrace = _window_runners(
        tmp_path,
        journal,
        record_returncode=19,
        record_stderr="Cannot find process matching name: AgentTool",
    )
    collector = RealIOSCollector(
        session,
        bus,
        provider=_StubProvider(process={"pid": 1, "name": "AgentTool"}),
        window_s=4.0,
        devicectl_runner=fail_devicectl,
        xctrace_runner=fail_xctrace,
        work_dir_factory=lambda prefix="": _mkdtemp_recorded(tmp_path, prefix),
    )
    collector._window_s = 4.0
    collector._window_gap_s = 1.0

    # 连续失败：gap 2→4→8→16→32→60(封顶)，next = 窗起 + window_s + gap
    cursor = 100.0
    for gap in (2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0):
        collector._emit_due(cursor)
        assert collector._next_window_at == pytest.approx(cursor + 4.0 + gap)
        cursor = collector._next_window_at
    assert collector._window_fail_streak == 7
    assert 4.0 + _FAILURE_BACKOFF_CAP_S == 64.0  # 封顶口径自检

    # 成功一窗 → 复位：原节奏 next = 窗起 + window_s + 原 gap
    ok_devicectl, ok_xctrace = _window_runners(tmp_path, journal)
    collector._devicectl_runner = ok_devicectl
    collector._xctrace_runner = ok_xctrace
    collector._emit_due(cursor)
    assert collector._next_window_at == pytest.approx(cursor + 4.0 + 1.0)
    assert collector._window_fail_streak == 0
    assert bus.samples(), "成功窗必须出点"


def test_collector_process_missing_keeps_normal_cadence(tmp_path: Path) -> None:
    """进程缺失是语义态而非设备级失败：不进退避，维持原节奏（null 点照发）。"""
    bus = _BusRecorder()
    session = _metric_session(tmp_path)
    collector = _make_collector(
        tmp_path, bus, session, window_s=4.0, process_name=None
    )
    collector._window_s = 4.0
    collector._window_gap_s = 1.0
    collector._emit_due(100.0)
    assert collector._next_window_at == pytest.approx(105.0)
    collector._emit_due(105.0)
    assert collector._next_window_at == pytest.approx(110.0)
    assert collector._window_fail_streak == 0


# ---- 审读修订回归（2026-11-16）：锚点采样点 / 探测失败无数据语义 ----


def test_window_anchor_sampled_at_record_exit_before_export(tmp_path: Path) -> None:
    """审读#1：窗口锚点在 record 退出即采样（§26.4）——export 两跳的秒级耗时不
    推后窗口时戳（此前链路末尾取钟会把全部点时戳系统性推后 export 耗时）。"""
    from app.providers.xctrace import run_sample_window

    journal: list[tuple[str, list[str]]] = []
    devicectl_runner, base_xctrace_runner = _window_runners(tmp_path, journal)
    holder = {"now": 1_900_000_000.0}
    record_exit_now: list[float] = []

    def timed_xctrace_runner(command: list[str], timeout: float) -> Any:
        args = command[2:]
        if args[0] == "record":
            result = base_xctrace_runner(command, timeout)
            record_exit_now.append(holder["now"])  # record 退出时刻的宿主钟
            return result
        assert args[0] == "export"
        holder["now"] += 4.0  # 每跳 export 耗时 4s（README 实测「秒级完成」量级）
        return base_xctrace_runner(command, timeout)

    result = run_sample_window(
        UDID, "Runner", 4.0,
        work_dir=str(tmp_path / "win"),
        devicectl_runner=devicectl_runner,
        xctrace_runner=timed_xctrace_runner,
        epoch_clock=lambda: holder["now"],
    )
    assert record_exit_now == [1_900_000_000.0]  # record 退出时 export 尚未耗时
    # 锚点 = record 退出时刻回推 time-limit（而非链路末尾钟 −time-limit）
    assert result["windowStartEpochMs"] == 1_899_999_996_000
    assert holder["now"] == 1_900_000_008.0  # 两跳 export 各 +4s 已流逝
    # 旧缺陷回归断言：链路末尾取钟会得到 1_900_000_004_000
    assert result["windowStartEpochMs"] != int(holder["now"] * 1000) - 4000


def test_collector_online_probe_failure_no_data_semantics(tmp_path: Path) -> None:
    """审读#3：在线探测命令失败（重试耗尽）→ xctrace_unavailable（reason=
    devicectl_probe_failed）诊断，无点无 null——不误报 device.tunnel.offline。"""
    from app.errors import ApiError
    from app.providers.xctrace import XCTRACE_UNAVAILABLE
    from app.services.realios_sampler import RealIOSCollector

    class _ProbeFailProvider:
        def find_device_strict(self, device_id: str) -> dict[str, Any] | None:
            raise ApiError(
                XCTRACE_UNAVAILABLE,
                "devicectl list-devices 探测重试耗尽（3 次）",
                details={
                    "reason": "devicectl_probe_failed",
                    "probe": "list-devices",
                    "attempts": 3,
                },
            )

    bus = _BusRecorder()
    session = _metric_session(tmp_path)
    collector = RealIOSCollector(
        session, bus, provider=_ProbeFailProvider(), window_s=4.0,
        devicectl_runner=lambda c, t: _completed(""),
        xctrace_runner=lambda c, t: _completed(""),
    )
    collector._run_window()
    diagnostics = bus.diagnostics()
    assert len(diagnostics) == 1
    assert diagnostics[0]["code"] == "xctrace_unavailable"
    assert diagnostics[0]["severity"] == "error"
    assert diagnostics[0]["detail"]["reason"] == "devicectl_probe_failed"
    # 无数据语义：无 null 点、无 tunnel.offline 误报、不进录制
    assert bus.samples() == []
    assert not any(d["code"] == "device.tunnel.offline" for d in diagnostics)


def test_collector_resolve_probe_failure_no_data_semantics(tmp_path: Path) -> None:
    """审读#3：apps/processes 探测失败（重试耗尽）→ 无数据诊断 + 该窗不出点
    （无 null 点）——不误报 app.process.not_found（探测失败≠进程不存在）。"""
    from app.errors import ApiError
    from app.providers.xctrace import XCTRACE_UNAVAILABLE
    from app.services.realios_sampler import RealIOSCollector

    class _ResolveFailProvider:
        def find_device_strict(self, device_id: str) -> dict[str, Any] | None:
            return {
                "id": device_id, "platform": "ios",
                "state": "online", "connection": "usb",
            }

        def resolve_process_strict(
            self, device_id: str, package_name: str
        ) -> dict[str, Any] | None:
            raise ApiError(
                XCTRACE_UNAVAILABLE,
                "devicectl info-apps 探测重试耗尽（3 次）",
                details={
                    "reason": "devicectl_probe_failed",
                    "probe": "info-apps",
                    "attempts": 3,
                },
            )

    bus = _BusRecorder()
    session = _metric_session(tmp_path)
    journal: list[tuple[str, list[str]]] = []
    devicectl_runner, xctrace_runner = _window_runners(tmp_path, journal)
    collector = RealIOSCollector(
        session, bus, provider=_ResolveFailProvider(), window_s=4.0,
        devicectl_runner=devicectl_runner, xctrace_runner=xctrace_runner,
        work_dir_factory=lambda prefix="": _mkdtemp_recorded(tmp_path, prefix),
    )
    collector._process_resolver = collector._resolve_attach_name  # 生产 strict 路径
    collector._run_window()
    diagnostics = bus.diagnostics()
    assert len(diagnostics) == 1
    assert diagnostics[0]["code"] == "xctrace_unavailable"
    assert diagnostics[0]["detail"]["reason"] == "devicectl_probe_failed"
    assert diagnostics[0]["detail"]["probe"] == "info-apps"
    # 无数据语义：无 null 点、无 not_found 误报、不进录制
    assert bus.samples() == []
    assert not any(d["code"] == "app.process.not_found" for d in diagnostics)
    assert not [c for c in journal if c[0] == "xctrace"]
