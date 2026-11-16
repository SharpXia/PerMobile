"""simulator 会话全链单测（M-iOS 契约 §23）：假 simctl/ps 驱动生产链路。

与 test_real_m6_paths.py 同哲学：传输层（simctl/ps 子进程）替身，会话状态机/
采集调度/落盘/录屏收口/日志流过滤全部走生产代码。live smoke（booted 模拟器
实测）独立标注在 test_simulator_live_smoke.py。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.providers.simulator import SimulatorProvider  # noqa: E402
from app.services.simulator_sampler import SimulatorCollector  # noqa: E402

SIMULATOR_UDID = "88B9E268-8B16-46BA-9E47-E32495BCE547"
TARGET_APP = "com.apple.Preferences"

DEVICES_JSON = f"""{{
  "devices": {{
    "com.apple.CoreSimulator.SimRuntime.iOS-27-0": [
      {{"udid": "{SIMULATOR_UDID}", "isAvailable": true, "state": "Booted",
        "name": "iPhone 18 Pro"}}
    ]
  }}
}}"""

FIXTURE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + b"\x00" * 16


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> Any:
    result = type("_R", (), {})()
    result.stdout = stdout
    result.returncode = returncode
    result.stderr = stderr
    return result


class _FakePopen:
    """simctl recordVideo 子进程替身：SIGINT 后写 4KB 产物（>1KB 不判废）。"""

    def __init__(self, command: list[str]) -> None:
        self.command = command
        self.signalled = False

    def poll(self) -> int | None:
        return None

    def send_signal(self, sig: int) -> None:
        self.signalled = True

    def wait(self, timeout: float | None = None) -> int:
        Path(self.command[-1]).write_bytes(b"\x00" * 4096)
        return 0


@contextmanager
def make_simulator_client(
    tmp_path: Path, monkeypatch: Any, *, log_lines: list[str] | None = None
) -> Iterator[TestClient]:
    """simulator 模式测试客户端：假 simctl runner + 替身采样/录屏/日志源。"""
    log_lines = log_lines if log_lines is not None else []
    settings = Settings(
        provider="simulator",
        runs_dir=tmp_path / "runs",
        cases_db_path=tmp_path / "data" / "cases.db",
    )

    def fake_simctl(command: list[str], timeout: float) -> Any:
        args = command[2:]  # 剥 ["xcrun", "simctl"]
        if args[:3] == ["list", "devices", "--json"]:
            return _completed(DEVICES_JSON)
        if args[:1] == ["listapps"]:
            return _completed(
                '{\n    "com.apple.Preferences" =     {\n'
                '        ApplicationType = System;\n'
                '        CFBundleDisplayName = Settings;\n'
                '        CFBundleIdentifier = "com.apple.Preferences";\n    };\n}\n'
            )
        if len(args) >= 3 and args[:3] == ["spawn", SIMULATOR_UDID, "launchctl"]:
            return _completed(
                f"81296\t0\tUIKitApplication:{TARGET_APP}[70d3][rb-legacy]\n"
            )
        if len(args) >= 3 and args[:3] == ["spawn", SIMULATOR_UDID, "log"]:
            return _completed(
                "Timestamp               Ty Process[PID:TID]\n"
                + "\n".join(log_lines)
                + ("\n" if log_lines else "")
            )
        if args[:1] == ["launch"]:
            return _completed(f"{TARGET_APP}: 81296\n")
        if args[:1] == ["bootstatus"]:
            return _completed("Device already booted\n")
        return _completed("")

    provider = SimulatorProvider(host_cores=8, simctl_runner=fake_simctl)

    # 采样替身：pid 恒在、ps 读数固定（宿主口径两指标确定性出点）
    monkeypatch.setattr(
        SimulatorCollector, "_target_pid_via_simctl", lambda self: 81296
    )
    monkeypatch.setattr(
        SimulatorCollector, "_sample_ps", lambda self, pid: (12.5, 453.2)
    )
    # 日志流替身：SimctlLogSource 的 simctl log show 轮询走模块级 _run（同为
    # simctl 命令形态），一并替换为 fixture 行（ps/采集已被类级替身接管）
    import app.services.simulator_sampler as sampler_module

    def fake_module_run(command: list[str], timeout: float) -> Any:
        args = command[2:] if command[:2] == ["xcrun", "simctl"] else command
        if len(args) >= 3 and args[:3] == ["spawn", SIMULATOR_UDID, "log"]:
            return _completed(
                "Timestamp               Ty Process[PID:TID]\n"
                + "\n".join(log_lines)
                + ("\n" if log_lines else "")
            )
        return _completed("")

    monkeypatch.setattr(sampler_module, "_run", fake_module_run)

    def fake_popen(command: list[str]) -> Any:
        return _FakePopen(command)

    app = create_app(settings, provider=provider)
    # 录屏器工厂注入（SimulatorScreenRecorder + fake popen；避免真 simctl 子进程）
    from app.services.simulator_sampler import SimulatorScreenRecorder

    def recorder_factory(session: Any, run_path: Path) -> Any:
        return SimulatorScreenRecorder(
            session=session,
            artifact_dir=run_path / "artifacts",
            case_name=run_path.name,
            popen=fake_popen,
        )

    app.state.sessions._screen_recorder_factory = recorder_factory
    with TestClient(app) as client:
        client.runs_dir = settings.runs_dir  # type: ignore[attr-defined]
        yield client


def _make_session(client: TestClient, **overrides: Any) -> dict:
    body: dict[str, Any] = {
        "deviceId": SIMULATOR_UDID,
        "packageName": TARGET_APP,
        "metrics": [
            {"metricId": "cpu.proc.host.pct", "intervalMs": 50},
            {"metricId": "memory.rss.mb", "intervalMs": 50},
        ],
    }
    body.update(overrides)
    resp = client.post("/api/sessions", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def wait_until(predicate: Any, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met before timeout")


# ---- 会话与采样（宿主口径）----


def test_simulator_session_host_sampling_flow(tmp_path, monkeypatch) -> None:
    with make_simulator_client(tmp_path, monkeypatch) as client:
        session = _make_session(client)
        assert session["state"] == "monitoring"
        assert [m["metricId"] for m in session["metrics"]] == [
            "cpu.proc.host.pct", "memory.rss.mb",
        ]
        wait_until(lambda: client.get(
            f"/api/sessions/{session['id']}/metrics"
        ).json()["series"][0].get("points"))
        series = client.get(f"/api/sessions/{session['id']}/metrics").json()["series"]
        cpu = next(s for s in series if s["metricId"] == "cpu.proc.host.pct")
        mem = next(s for s in series if s["metricId"] == "memory.rss.mb")
        assert all(p["value"] == 12.5 for p in cpu["points"])
        assert all(p["value"] == 453.2 for p in mem["points"])
        # save → samples.ndjson 落盘（数值点；null 仅 SSE——本流无 null）
        saved = client.post(f"/api/sessions/{session['id']}/save", json={}).json()
        run_dir = Path(saved["runPath"])
        samples = [
            json.loads(line)
            for line in (run_dir / "samples.ndjson").read_text().splitlines()
            if line
        ]
        assert samples, "宿主口径采样点必须落盘"
        assert {s["metricId"] for s in samples} == {
            "cpu.proc.host.pct", "memory.rss.mb",
        }


def test_simulator_process_missing_emits_null_and_diagnostic(tmp_path, monkeypatch) -> None:
    """§6.1 同语义：App 进程不存在 → diagnostic + null 点（仅 SSE，不落盘）。"""
    with make_simulator_client(tmp_path, monkeypatch) as client:
        # pid 替身改道为「进程不存在」
        monkeypatch.setattr(SimulatorCollector, "_target_pid_via_simctl", lambda self: None)
        session = _make_session(client)
        events: list[dict] = []

        def consume() -> None:
            try:
                with client.stream(
                    "GET", f"/api/sessions/{session['id']}/events",
                    headers={"Last-Event-ID": "0"},
                ) as resp:
                    for line in resp.iter_lines():
                        if line is None:
                            continue
                        text = line.rstrip("\r")
                        if text.startswith("data:"):
                            events.append(json.loads(text.removeprefix("data:").strip()))
                        if len(events) > 12:
                            break
            except Exception:  # noqa: BLE001 —— 收尾竞态：discard 后 client 关闭中断流
                pass

        thread = threading.Thread(target=consume, daemon=True)
        thread.start()
        wait_until(
            lambda: any(e.get("type") == "diagnostic"
                        and e.get("code") == "app.process.not_found" for e in events)
        )
        nulls = [
            e for e in events
            if e.get("type") == "sample" and e.get("value") is None
        ]
        assert nulls and all(n["metricId"] in ("cpu.proc.host.pct", "memory.rss.mb") for n in nulls)
        client.post(f"/api/sessions/{session['id']}/discard")


# ---- 录屏（simctl recordVideo 生命周期 + 判废 + 锚点）----


def test_simulator_record_screen_flow(tmp_path, monkeypatch) -> None:
    with make_simulator_client(tmp_path, monkeypatch) as client:
        session = _make_session(client, recordScreen=True, metrics=[])
        sid = session["id"]
        client.post(f"/api/sessions/{sid}/record/start")
        client.post(f"/api/sessions/{sid}/record/stop")
        saved = client.post(f"/api/sessions/{sid}/save", json={}).json()
        run_dir = Path(saved["runPath"])
        # §16.1 既有映射：screen.mp4 → video.mp4；4KB > 1KB 不判废
        assert (run_dir / "artifacts" / "video.mp4").read_bytes() == b"\x00" * 4096
        manifest = json.loads((run_dir / "manifest.json").read_text())
        anchors = manifest["context"]["clockAnchors"]
        # video 锚点（§23.3）：simctl 方法声明 + host_epoch 时钟源
        assert anchors["video"]["method"].startswith("simctl.io.recordVideo")
        assert anchors["video"]["clockSource"] == "host_epoch"
        assert anchors["video"]["segments"] == 1
        # 采样时钟锚点（§23.3）：host_epoch（模拟器与宿主共享内核时钟）
        assert anchors["samplers"]["source"] == "host_epoch"
        assert isinstance(anchors["samplers"]["startEpochMs"], int)
        artifacts = client.get(f"/api/runs/{saved['runId']}/artifacts").json()["artifacts"]
        assert any(a["name"] == "video.mp4" and a["kind"] == "video" for a in artifacts)


def test_simulator_record_invalidated_under_1kb(tmp_path, monkeypatch) -> None:
    """<1KB 判废（§16.1 同口径）：产物删除 + diagnostic + video 锚点撤回。"""
    with make_simulator_client(tmp_path, monkeypatch) as client:
        session = _make_session(client, recordScreen=True, metrics=[])
        sid = session["id"]

        # 替身 Popen 写 512B（判废路径）
        class _TinyPopen(_FakePopen):
            def wait(self, timeout: float | None = None) -> int:
                Path(self.command[-1]).write_bytes(b"\x00" * 512)
                return 0

        from app.services.simulator_sampler import SimulatorScreenRecorder

        original_start = SimulatorScreenRecorder.start

        def start_with_tiny(self: Any) -> None:
            self._popen = lambda command: _TinyPopen(command)
            original_start(self)

        monkeypatch.setattr(SimulatorScreenRecorder, "start", start_with_tiny)
        client.post(f"/api/sessions/{sid}/record/start")
        client.post(f"/api/sessions/{sid}/record/stop")
        saved = client.post(f"/api/sessions/{sid}/save", json={}).json()
        run_dir = Path(saved["runPath"])
        assert not (run_dir / "artifacts" / "video.mp4").exists()
        manifest = json.loads((run_dir / "manifest.json").read_text())
        anchors = manifest["context"]["clockAnchors"] or {}
        assert "video" not in anchors  # 判废后 video 锚点撤回（samplers 锚点仍在）
        assert anchors.get("samplers", {}).get("source") == "host_epoch"


# ---- 截图（simctl screenshot，§23.3）----


def test_simulator_screenshot_endpoint(tmp_path, monkeypatch) -> None:
    import app.services.simulator_sampler as sampler_module

    with make_simulator_client(tmp_path, monkeypatch) as client:
        session = _make_session(client, metrics=[])
        client.post(f"/api/sessions/{session['id']}/record/start")

        def fake_capture(device_id: str, **kwargs: Any) -> bytes:
            assert device_id == SIMULATOR_UDID
            return FIXTURE_PNG

        monkeypatch.setattr(
            sampler_module, "capture_simulator_screenshot_bytes", fake_capture
        )
        resp = client.post(f"/api/sessions/{session['id']}/screenshot")
        assert resp.status_code == 201, resp.text
        artifact = resp.json()["artifact"]
        assert artifact["kind"] == "screenshot"
        assert artifact["bytes"] == len(FIXTURE_PNG)
        client.post(f"/api/sessions/{session['id']}/record/stop")
        # 定时截图（simulator 模式路由 capture 注入面）
        resp = client.post(f"/api/sessions/{session['id']}/timed-screenshot", json={})
        assert resp.status_code == 409  # stopped 不可启动（状态门）


# ---- 日志流（§23.4：simctl log show 轮询行源）----


def test_simulator_log_stream_filters_by_level(tmp_path, monkeypatch) -> None:
    lines = [
        # 级别字母映射：Df→debug、Er→error、If→info；无级别行丢弃（TID 为 hex，
        # 与 log show --style compact 实测形态一致）
        "2026-11-04 00:02:42.767 Df Preferences[81296:14a6a43] debug message alpha",
        "2026-11-04 00:02:42.900 Er SpringBoard[80459:14a6b01] error message beta",
        "2026-11-04 00:02:43.100 If chronod[80616:14a6b11] info message gamma",
        "continuation line without level",
        "2026-11-04 00:02:43.200 Wf SpringBoard[80459:14a6b21] warn message delta",
    ]
    with make_simulator_client(tmp_path, monkeypatch, log_lines=lines) as client:
        session = _make_session(
            client, metrics=[], logLevel="warn"  # 选中 warn：W/E 放行，D/I 拦
        )
        events: list[dict] = []

        def consume() -> None:
            try:
                with client.stream(
                    "GET", f"/api/sessions/{session['id']}/events",
                    headers={"Last-Event-ID": "0"},
                ) as resp:
                    for line in resp.iter_lines():
                        if line is None:
                            continue
                        text = line.rstrip("\r")
                        if text.startswith("data:"):
                            events.append(json.loads(text.removeprefix("data:").strip()))
                        if any(e.get("type") == "log" for e in events) and len(events) > 6:
                            break
            except Exception:  # noqa: BLE001 —— 收尾竞态：discard 后 client 关闭中断流
                pass

        thread = threading.Thread(target=consume, daemon=True)
        thread.start()
        wait_until(lambda: any(e.get("type") == "log" for e in events), timeout_s=10)
        logs = [e for e in events if e.get("type") == "log"]
        levels = {log["level"] for log in logs}
        assert levels <= {"warn", "error"}  # debug/info 被级别过滤拦下
        assert any("error message beta" in log["line"] for log in logs)
        assert all(log["source"] for log in logs)  # source = 进程名（§21.7 信封同形）
        client.post(f"/api/sessions/{session['id']}/discard")


# ---- 会话门禁（offline 设备 / launchOnCreate / sim-boot）----


def test_simulator_session_offline_device_rejected(tmp_path, monkeypatch) -> None:
    with make_simulator_client(tmp_path, monkeypatch) as client:
        body = {
            "deviceId": "FFFF-UNKNOWN",  # 不存在
            "packageName": TARGET_APP,
            "metrics": [],
        }
        resp = client.post("/api/sessions", json=body)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "device_not_found"


def test_simulator_launch_on_create(tmp_path, monkeypatch) -> None:
    """launchOnCreate 接线（§6.1）：simctl launch 成功 → phase completed。"""
    with make_simulator_client(tmp_path, monkeypatch) as client:
        session = _make_session(client, metrics=[], launchOnCreate=True)
        events: list[dict] = []

        def consume() -> None:
            try:
                with client.stream(
                    "GET", f"/api/sessions/{session['id']}/events",
                    headers={"Last-Event-ID": "0"},
                ) as resp:
                    for line in resp.iter_lines():
                        if line is None:
                            continue
                        text = line.rstrip("\r")
                        if text.startswith("data:"):
                            events.append(json.loads(text.removeprefix("data:").strip()))
                        if any(
                            e.get("type") == "phase" and e.get("phase") == "app_prepare"
                            and e.get("state") == "completed" for e in events
                        ):
                            break
            except Exception:  # noqa: BLE001 —— 收尾竞态：discard 后 client 关闭中断流
                pass

        thread = threading.Thread(target=consume, daemon=True)
        thread.start()
        wait_until(
            lambda: any(
                e.get("type") == "phase" and e.get("phase") == "app_prepare"
                and e.get("state") == "completed" for e in events
            ),
            timeout_s=10,
        )
        phases = [e for e in events if e.get("type") == "phase" and e.get("phase") == "app_prepare"]
        assert phases[0]["state"] == "started"
        assert any(p["state"] == "completed" and p["detail"] == TARGET_APP for p in phases)
        client.post(f"/api/sessions/{session['id']}/discard")


def test_sim_boot_endpoint_modes(tmp_path, monkeypatch) -> None:
    """§23.1 sim-boot：simulator 模式可执行（幂等）；synthetic 模式 501 + 诊断。"""
    with make_simulator_client(tmp_path, monkeypatch) as client:
        resp = client.post(f"/api/devices/{SIMULATOR_UDID}/sim-boot")
        assert resp.status_code == 200
        assert resp.json() == {
            "deviceId": SIMULATOR_UDID, "action": "boot", "state": "online",
        }
        resp = client.post("/api/devices/NOPE/sim-boot")
        assert resp.status_code == 404

    # synthetic 模式：501 not_implemented + details.diagnostic
    synthetic_settings = Settings(
        provider="synthetic",
        runs_dir=tmp_path / "runs2",
        cases_db_path=tmp_path / "data2" / "cases.db",
    )
    from app.providers.synthetic import SyntheticProvider

    app = create_app(synthetic_settings, provider=SyntheticProvider())
    with TestClient(app) as client2:
        resp = client2.post("/api/devices/synthetic-001/sim-boot")
        assert resp.status_code == 501
        body = resp.json()
        assert body["error"]["code"] == "not_implemented"
        assert body["error"]["details"]["diagnostic"]["code"] == "sim-boot.unavailable"


# ---- 2026-11-04 体检修复回归（sim-1 覆盖窗口回补 / sim-2 平台标注 / sim-3 时钟基）----


def test_simulator_save_backfills_full_window_and_labels_ios(tmp_path, monkeypatch) -> None:
    """sim-1/sim-2/sim-3 修复端到端（simulator 全链，PD_20261104_004744 同形态）：

    - sim-1：save 后 samples.ndjson 覆盖全程（monitoring 期样本经内存缓冲回补，
      与 timeline/summary 同窗口）——此前 recording 在线追加只落录制窗样本，
      统计侧按 samples.ndjson 计数与 summary 口径静默分叉（8/37）；
    - sim-2：timeline 事件 source.platform == "ios"（注册表 simulator 指标平台，
      此前硬编码 "android"）；
    - sim-3：tsMonotonicNs = (tsMs - createdAt) × 1e6 的 run 内相对纳秒，单调
      递增（此前 epoch ms × 1e6 直填单调钟字段，与 02-log-schema.md:146 不符）。
    """
    with make_simulator_client(tmp_path, monkeypatch) as client:
        session = _make_session(client)
        sid = session["id"]
        created_at = session["createdAt"]
        # monitoring 期先出点（sim-1 回补的来源）
        wait_until(lambda: client.get(f"/api/sessions/{sid}/metrics").json()["series"][0].get("points"))
        client.post(f"/api/sessions/{sid}/record/start")
        wait_until(lambda: len(client.get(f"/api/sessions/{sid}/metrics").json()["series"][0]["points"]) >= 4)
        client.post(f"/api/sessions/{sid}/record/stop")
        saved = client.post(f"/api/sessions/{sid}/save", json={}).json()
        run_dir = Path(saved["runPath"])

        samples = [
            json.loads(line)
            for line in (run_dir / "samples.ndjson").read_text().splitlines()
            if line
        ]
        timeline = [
            json.loads(line)
            for line in (run_dir / "timeline.ndjson").read_text().splitlines()
            if line
        ]
        assert samples, "采样点必须落盘"

        # sim-1：samples.ndjson 与 timeline 覆盖窗口一致（修复前 samples 严格少于
        # timeline——monitoring 期样本只在内存缓冲）
        assert len(samples) == len(timeline)
        assert {row["tsMs"] for row in samples} == {
            int(event["tsMonotonicNs"] // 1_000_000) + created_at for event in timeline
        }

        # sim-2：simulator 会话事件标 ios 平台
        assert {event["source"]["platform"] for event in timeline} == {"ios"}

        # sim-3：tsMonotonicNs 为 run 内相对纳秒（原点 = 会话创建时刻），单调递增
        for row, event in zip(samples, timeline):
            assert event["tsMonotonicNs"] == (row["tsMs"] - created_at) * 1_000_000
        offsets = [event["tsMonotonicNs"] for event in timeline]
        assert offsets == sorted(offsets)
        assert offsets[0] >= 0
