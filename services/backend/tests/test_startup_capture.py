"""S3 startup-ttid-ttfd：run 级启动旁路测量（startup_capture + POST /runs/{id}/startup/capture）。

- 解析（纯函数）：『Displayed』/『Fully drawn』行 → TTID/TTFD（秒缺省与亚秒形态）；
- capture 落盘：fake adb（am start -W 输出 + golden logcat-startup.txt 样本）→
  startup.ndjson（perfdog.startup.v1）追加；
- run_startup 回填：GET /runs/{runId}/startup 读回 ttidMs/ttfdMs；
- 无 Fully drawn 行 → ttfdMs null + note（不伪造）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.services.startup_capture import (  # noqa: E402
    capture_startup,
    parse_displayed_line,
    parse_fully_drawn_line,
    read_startup_ndjson,
)

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "perfeval" / "fixtures" / "android" / "golden"

_AM_START_STDOUT = (
    "Starting: Intent { cmp=com.perfeval.fixture/.MainActivity }\n"
    "Status: ok\n"
    "LaunchState: COLD\n"
    "TotalTime: 842\n"
    "WaitTime: 921\n"
)

_GOLDEN_DISPLAYED_LINE = (
    "06-25 10:30:00.500  12345  12345 I ActivityManager: "
    "Displayed com.perfeval.fixture/.MainActivity: +1s234ms"
)

# 真机 Android 16 同款（ActivityTaskManager tag，亚秒形态）
_TASKMGR_DISPLAYED_LINE = (
    "11-08 15:04:05.123  1234  5678 I ActivityTaskManager: "
    "Displayed com.perfeval.fixture/.MainActivity: +842ms"
)

_FULLY_DRAWN_LINE = (
    "06-25 10:30:02.345  12345  12345 I ActivityTaskManager: "
    "Fully drawn com.perfeval.fixture/.MainActivity: +2s345ms"
)


class _FakeRunner:
    """fake adb：query-activities / am start / logcat -c 按序应答。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, *args: str, serial: str | None = None, timeout: float | None = None) -> Any:
        self.calls.append((args, serial, timeout))
        joined = " ".join(args)
        if "query-activities" in joined:
            stdout = "com.perfeval.fixture/.MainActivity\n"
            return self._result(0, stdout)
        if "am start" in joined:
            assert "-W" in args, "am start 必须带 -W（TotalTime 同源口径）"
            return self._result(0, _AM_START_STDOUT)
        if joined == "logcat -c":
            return self._result(0, "")
        return self._result(0, "")

    @staticmethod
    def _result(code: int, stdout: str) -> Any:
        return type("R", (), {"returncode": code, "stdout": stdout, "stderr": ""})()


class _FakeSource:
    """注入 seam：恒定行序列（模拟 logcat 尾随）。

    时钟用真实 monotonic（capture 缺省 clock），测试以小窗口参数
    （total_deadline_s=2.0 / ttfd_grace_s=0.2）保证亚秒收口——行源为内存序列，
    等待循环在窗口内即时消费完毕。"""

    def __init__(self, *log_lines: str) -> None:
        self.lines = log_lines

    def factory(self) -> Callable[[str], Callable[[], Iterator[str] | None]]:
        def make(serial: str) -> Callable[[], Iterator[str] | None]:
            def source() -> Iterator[str] | None:
                yield from self.lines

            return source

        return make


def _run_dir(client: TestClient, case_name: str) -> Path:
    run_dir = client.runs_dir / case_name
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": "perfeval.manifest.v1",
                "runId": f"run-{case_name}",
                "caseName": case_name,
                "device": "FAKE001",
                "packageName": "com.perfeval.fixture",
            }
        ),
        encoding="utf-8",
    )
    return run_dir


# ---- 纯函数解析 ----


def test_parse_displayed_and_fully_drawn() -> None:
    assert parse_displayed_line("Displayed com.perfeval.fixture/.MainActivity: +1s234ms") == 1234.0
    assert parse_displayed_line("Displayed com.perfeval.fixture/.MainActivity: +842ms") == 842.0
    assert parse_fully_drawn_line("Fully drawn com.perfeval.fixture/.MainActivity: +2s345ms") == 2345.0
    # 多用户形态（真机回归 2026-11-11，小米 2405CPX3DC/Android 16）：
    # 组件与时长之间带 `for user <N>` 尾巴——旧正则不匹配 → ttid 恒 null
    assert parse_displayed_line("Displayed com.android.settings/.MainSettings for user 0: +301ms") == 301.0
    assert parse_displayed_line("Displayed com.pkg/.Act for user 10: +1s088ms") == 1088.0
    assert parse_fully_drawn_line("Fully drawn com.pkg/.Act for user 0: +2s340ms") == 2340.0
    # 非 Displayed/Fully drawn 行与多行续行 → None（不伪造）
    assert parse_displayed_line("ActivityDisplayed nope") is None
    assert parse_fully_drawn_line("Fully drawnx: +1s") is None


# ---- capture 落盘（fake adb + golden 行样本）----


def test_capture_writes_startup_ndjson_and_returns_record(client: TestClient) -> None:
    _run_dir(client, "PD_STARTUP_CAP")
    runner = _FakeRunner()
    _source = _FakeSource(_GOLDEN_DISPLAYED_LINE, _FULLY_DRAWN_LINE)
    record = capture_startup(
        client.runs_dir,
        "run-PD_STARTUP_CAP",
        "FAKE001",
        line_source_factory=_source.factory(),
        command_runner=runner,
        total_deadline_s=2.0,
        ttfd_grace_s=0.2,
        now_epoch=lambda: 1793600000.0,
    )
    assert record["state"] == "completed"
    assert record["totalTimeMs"] == 842
    assert record["launchState"] == "cold"
    assert record["ttidMs"] == 1234  # golden logcat-startup.txt 同款 +1s234ms
    assert record["ttfdMs"] == 2345
    assert record["schemaVersion"] == "perfdog.startup.v1"
    assert record["packageName"] == "com.perfeval.fixture"
    assert "reportFullyDrawn" in record["sources"]["ttfd"]

    # am start -W 与 query-activities 都打到注入的 serial（映射口径由调用方保证）
    serials = {call[1] for call in runner.calls}
    assert serials == {"FAKE001"}
    am_start_calls = [call[0] for call in runner.calls if "am" in call[0] and "start" in call[0]]
    assert am_start_calls, "必须执行 am start -W"

    path = client.runs_dir / "PD_STARTUP_CAP" / "startup.ndjson"
    assert path.is_file()
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert lines[-1]["state"] == "completed" and lines[-1]["ttidMs"] == 1234

    # 读回：最近一次 completed 记录
    assert read_startup_ndjson(client.runs_dir / "PD_STARTUP_CAP") == (1234, 2345)


def test_capture_ttid_only_ttfd_null_when_no_fully_drawn(client: TestClient) -> None:
    """无 Fully drawn 行（应用未调用 reportFullyDrawn）→ ttfdMs null + note，不伪造。"""
    _run_dir(client, "PD_STARTUP_NOTTFD")
    _source = _FakeSource(_GOLDEN_DISPLAYED_LINE)
    record = capture_startup(
        client.runs_dir,
        "run-PD_STARTUP_NOTTFD",
        "FAKE001",
        line_source_factory=_source.factory(),
        command_runner=_FakeRunner(),
        total_deadline_s=2.0,
        ttfd_grace_s=0.2,
    )
    assert record["state"] == "completed"
    assert record["ttidMs"] == 1234
    assert record["ttfdMs"] is None
    assert "reportFullyDrawn" in record["note"] and "null" in record["note"]
    assert read_startup_ndjson(client.runs_dir / "PD_STARTUP_NOTTFD") == (1234, None)


def test_capture_activitytaskmanager_subsecond_line(client: TestClient) -> None:
    """ActivityTaskManager tag + 亚秒形态（Android 16 真机同款）同样解析。"""
    _run_dir(client, "PD_STARTUP_TASKMGR")
    _source = _FakeSource(_TASKMGR_DISPLAYED_LINE)
    record = capture_startup(
        client.runs_dir,
        "run-PD_STARTUP_TASKMGR",
        "FAKE001",
        line_source_factory=_source.factory(),
        command_runner=_FakeRunner(),
        total_deadline_s=2.0,
        ttfd_grace_s=0.2,
    )
    assert record["ttidMs"] == 842


def test_capture_package_prefix_filters_stale_lines(client: TestClient) -> None:
    """logcat -c 失败时其它包的 Displayed 行不误采（package 前缀过滤）。"""
    _run_dir(client, "PD_STARTUP_PREFIX")
    stale = (
        "06-25 10:29:00.000  999  999 I ActivityManager: "
        "Displayed com.other.app/.MainActivity: +1ms"
    )
    _source = _FakeSource(stale, _GOLDEN_DISPLAYED_LINE)
    record = capture_startup(
        client.runs_dir,
        "run-PD_STARTUP_PREFIX",
        "FAKE001",
        line_source_factory=_source.factory(),
        command_runner=_FakeRunner(),
        total_deadline_s=2.0,
        ttfd_grace_s=0.2,
    )
    assert record["ttidMs"] == 1234  # 只采本包行


def test_capture_no_displayed_line_ttid_null(client: TestClient) -> None:
    """am start 成功但未观测到 Displayed 行 → ttidMs/ttfdMs null + note（落盘）。"""
    _run_dir(client, "PD_STARTUP_NOLINE")
    _source = _FakeSource("06-25 10:30:00.500  1  1 D Other: noise")
    record = capture_startup(
        client.runs_dir,
        "run-PD_STARTUP_NOLINE",
        "FAKE001",
        line_source_factory=_source.factory(),
        command_runner=_FakeRunner(),
        total_deadline_s=2.0,
        ttfd_grace_s=0.2,
    )
    assert record["state"] == "completed"
    assert record["ttidMs"] is None and record["ttfdMs"] is None
    assert record["totalTimeMs"] == 842
    assert "TTID=null" in record["note"]


def test_capture_am_start_failed_returns_failed_record_without_file(
    client: TestClient, monkeypatch
) -> None:
    """am start 失败 → state=failed 记录（不落盘，无测量产物不伪造）。"""
    from unittest.mock import Mock

    from app.services import startup_capture

    class _FailingRunner(_FakeRunner):
        def __call__(self, *args: str, serial=None, timeout=None):  # noqa: ANN001
            if "am start" in " ".join(args):
                return type("R", (), {"returncode": 1, "stdout": "Error: Activity not found\n", "stderr": ""})()
            return super().__call__(*args, serial=serial, timeout=timeout)

    _run_dir(client, "PD_STARTUP_FAIL")
    adb_path = client.runs_dir / "应用目录 with spaces" / "adb.exe"
    monkeypatch.setenv("PERFEVAL_ADB_PATH", str(adb_path))
    source = Mock()
    line_source_factory = Mock(return_value=lambda: source)
    monkeypatch.setattr(startup_capture, "adb_logcat_time_source", line_source_factory)
    runner = _FailingRunner()

    def fake_run(adb, *args, **kwargs):
        assert adb.adb_path == str(adb_path)
        return runner(*args, **kwargs)

    monkeypatch.setattr(startup_capture.AdbClient, "run", fake_run)
    record = capture_startup(
        client.runs_dir,
        "run-PD_STARTUP_FAIL",
        "FAKE001",
        total_deadline_s=2.0,
        ttfd_grace_s=0.2,
    )
    assert record["state"] == "failed"
    assert record["error"] and record["ttidMs"] is None
    assert not (client.runs_dir / "PD_STARTUP_FAIL" / "startup.ndjson").exists()
    line_source_factory.assert_called_once_with("FAKE001", adb_path=str(adb_path))
    source.close.assert_called_once_with()


# ---- 端点：POST capture + GET startup 回填 ----


def test_capture_endpoint_real_provider_writes_and_startup_backfills(client: TestClient) -> None:
    """端点链路：POST capture（synthetic → 501）切换 real provider 形态后回填。

    client 为 synthetic provider——501 not_implemented 分支直测；落盘回填链路
    经 capture_startup 直调（上组用例）+ GET /startup 断言（无捕获 → null + note）。
    """
    resp = client.post(
        "/api/runs/run-PD_STARTUP_EP/startup/capture",
        json={"deviceSerial": "FAKE001"},
    )
    assert resp.status_code == 501, resp.text
    assert resp.json()["error"]["code"] == "not_implemented"

    # capture 产物 → GET /startup 回填（synthetic provider 不拦截 GET）
    _run_dir(client, "PD_STARTUP_EP")
    _source = _FakeSource(_GOLDEN_DISPLAYED_LINE, _FULLY_DRAWN_LINE)
    capture_startup(
        client.runs_dir,
        "run-PD_STARTUP_EP",
        "FAKE001",
        line_source_factory=_source.factory(),
        command_runner=_FakeRunner(),
        total_deadline_s=2.0,
        ttfd_grace_s=0.2,
    )
    resp2 = client.get("/api/runs/run-PD_STARTUP_EP/startup")
    assert resp2.status_code == 200
    body = resp2.json()
    # 该 fixture 无 timeline 启动事件 → totalTimeMs null；TTID/TTFD 来自 startup.ndjson
    assert body["totalTimeMs"] is None
    assert body["ttidMs"] == 1234 and body["ttfdMs"] == 2345


def test_capture_endpoint_device_serial_validation(client: TestClient) -> None:
    resp = client.post("/api/runs/run-X/startup/capture", json={})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "validation_error"


def test_capture_endpoint_missing_run_404(client: TestClient) -> None:
    """synthetic provider 501 先行——real 形态下 run 不存在 → 404 由 resolve_run_dir 防护
    （capture_startup 直调断言）。"""
    from app.errors import ApiError

    import pytest

    with pytest.raises(ApiError) as exc_info:
        _source = _FakeSource()
        capture_startup(
            client.runs_dir,
            "run-NOPE",
            "FAKE001",
            line_source_factory=_source.factory(),
            command_runner=_FakeRunner(),
            total_deadline_s=2.0,
            ttfd_grace_s=0.2,
        )
    assert exc_info.value.code == "report_not_found"
