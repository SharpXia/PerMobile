"""R53 最小切片：网络测速（契约 §24）。

- payload 端点（全模式）：确定性字节流 + Content-Length；
- 设备测速端点：synthetic 501+诊断、校验 400、404；
- real 模式：mock adb（应答表驱动生产代码）断言命令序列（which curl →
  adb reverse → 设备 shell curl → reverse --remove）与各失败阶段
  503 speedtest_unavailable（details.reason 区分）。
真机 I/O（真实 USB 链路吞吐）无设备不可测，如实声明。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from m6_fixtures import (  # noqa: E402
    FakeAdb,
    completed,
    default_probe_responses,
    fake_preflight_result,
)
from app.services.speedtest import (  # noqa: E402
    SPEEDTEST_NOTE,
    curl_command,
    mbps_from_bytes_per_sec,
    parse_speed_download,
    payload_chunks,
)

_PATTERN_BLOCK = bytes(range(256)) * 256


# ---- 应答表（支持 returncode 的 FakeAdb 扩展；失败阶段断言需要 rc!=0）----


class SpeedFakeAdb(FakeAdb):
    """在 FakeAdb 应答表上追加 (matcher, stdout, returncode, stderr) 四元组支持。"""

    def __init__(self, responses: list[tuple[Any, ...]]) -> None:
        super().__init__([])
        self._responses = [
            r if len(r) == 4 else (*r, 0, "") for r in responses
        ]

    def run(self, *args: str, serial: str | None = None, timeout: float | None = None) -> Any:
        self.commands.append([*args, serial])
        self.serials.append(serial)
        joined = " ".join(args)
        for matcher, stdout, rc, stderr in self._responses:
            if matcher(joined):
                return completed(stdout, returncode=rc, stderr=stderr)
        return completed("")


def _speed_responses(
    *,
    curl_output: str = "5242880.0\n",
    curl_rc: int = 0,
    reverse_rc: int = 0,
    with_curl: bool = True,
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = list(default_probe_responses())
    if with_curl:
        rows.append(FakeAdb.when("which curl", "/system/bin/curl\n"))
    rows.append(
        (
            FakeAdb.when("reverse tcp:", "")[0],
            "",
            reverse_rc,
            "adb: failed to set up reverse" if reverse_rc else "",
        )
    )
    rows.append(FakeAdb.when("reverse --remove", ""))
    rows.append((FakeAdb.when("curl -s", curl_output)[0], curl_output, curl_rc, ""))
    return rows


def _make_real_client(
    monkeypatch: pytest.MonkeyPatch, responses: list[tuple[Any, ...]], tmp_path: Path
):
    import app.providers.real as real_module
    from app.config import Settings
    from app.main import create_app

    adb = SpeedFakeAdb(responses)
    provider = real_module.RealProvider(adb=adb)
    monkeypatch.setattr(real_module, "run_preflight", lambda _adb: fake_preflight_result())
    settings = Settings(
        provider="real", port=8737,
        runs_dir=tmp_path / "runs",
        cases_db_path=tmp_path / "data" / "cases.db",
    )
    app = create_app(settings, provider=provider)
    return TestClient(app), adb


# ---- payload 端点（全模式）----


def test_payload_endpoint_deterministic_bytes(client: TestClient) -> None:
    resp = client.get("/api/speedtest/payload?bytes=4096")
    assert resp.status_code == 200
    assert resp.headers["content-length"] == "4096"
    assert len(resp.content) == 4096
    # 确定性 64KiB 块的前 4096 字节（可复算，不做随机性声明）
    assert resp.content == _PATTERN_BLOCK[:4096]


def test_payload_endpoint_default_is_4mib(client: TestClient) -> None:
    resp = client.get("/api/speedtest/payload")
    assert resp.status_code == 200
    assert len(resp.content) == 4 * 1024 * 1024


@pytest.mark.parametrize("bad", [0, 512, 33554433, "abc"])
def test_payload_endpoint_bounds_400(client: TestClient, bad: Any) -> None:
    resp = client.get("/api/speedtest/payload", params={"bytes": bad})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "validation_error"


# ---- 设备测速端点：synthetic / 校验 / 404 ----


def test_speedtest_synthetic_501_with_diagnostic(client: TestClient) -> None:
    resp = client.post("/api/devices/synthetic-001/speedtest", json={})
    assert resp.status_code == 501
    error = resp.json()["error"]
    assert error["code"] == "not_implemented"
    diagnostic = error["details"]["diagnostic"]
    assert diagnostic["code"] == "speedtest.unavailable"
    assert diagnostic["severity"] == "info"


@pytest.mark.parametrize(
    "payload,field",
    [
        ({"bytes": 0}, "bytes"),
        ({"bytes": "1MB"}, "bytes"),
        ({"maxSeconds": 1}, "maxSeconds"),
        ({"maxSeconds": 301}, "maxSeconds"),
    ],
)
def test_speedtest_validation_400(client: TestClient, payload: dict, field: str) -> None:
    resp = client.post("/api/devices/synthetic-001/speedtest", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"]["field"] == field


def test_speedtest_unknown_device_404(client: TestClient) -> None:
    resp = client.post("/api/devices/ghost/speedtest", json={})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "device_not_found"


# ---- real 模式（mock adb 命令序列断言）----


def test_speedtest_real_success_command_sequence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """成功路径：which curl → reverse → 设备 curl → reverse --remove（清理）。"""
    client, adb = _make_real_client(monkeypatch, _speed_responses(), tmp_path)
    resp = client.post(
        "/api/devices/R3CN80ABCDEF/speedtest",
        json={"bytes": 1024 * 1024, "maxSeconds": 30},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    joined_commands = [" ".join(c[:-1]) for c in adb.commands]
    assert any("which curl" in c for c in joined_commands)
    reverse_cmds = [c for c in joined_commands if c.startswith("reverse tcp:")]
    assert len(reverse_cmds) == 1  # 建立 1 次（--remove 单列）
    assert any("reverse --remove" in c for c in joined_commands)
    curl_cmds = [c for c in joined_commands if "curl -s" in c]
    assert len(curl_cmds) == 1
    # 设备 curl 的本地端口 == adb reverse 的本地端口（同一隧道）
    local_port = reverse_cmds[0].split()[1].removeprefix("tcp:")
    assert f"http://127.0.0.1:{local_port}/api/speedtest/payload?bytes=1048576" in curl_cmds[0]
    # 隧道远端 = 后端监听端口（settings.port，contract §24）
    assert f"tcp:8737" in reverse_cmds[0]
    assert "--max-time 30" in curl_cmds[0]

    # 响应 golden 形状（契约 §24）：同源换算 + 恒带口径 note
    assert body["deviceId"] == "R3CN80ABCDEF"
    assert body["method"] == "adb.reverse.shell.curl"
    assert body["bytes"] == 1024 * 1024
    assert isinstance(body["durationMs"], int)
    assert body["bytesPerSec"] == 5242880.0
    assert body["mbps"] == mbps_from_bytes_per_sec(5242880.0)  # = 41.943
    assert body["note"] == SPEEDTEST_NOTE


def test_speedtest_real_reverse_failure_503(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """adb reverse 失败 → 503 speedtest_unavailable（reason=adb_reverse_failed），
    且不执行设备 curl、仍尝试清理 reverse。"""
    client, adb = _make_real_client(monkeypatch, _speed_responses(reverse_rc=1), tmp_path)
    resp = client.post("/api/devices/R3CN80ABCDEF/speedtest", json={})
    assert resp.status_code == 503
    error = resp.json()["error"]
    assert error["code"] == "speedtest_unavailable"
    assert error["details"]["reason"] == "adb_reverse_failed"
    joined_commands = [" ".join(c[:-1]) for c in adb.commands]
    assert not any("curl -s" in c for c in joined_commands)


def test_speedtest_real_curl_missing_503(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """设备无 curl → 503（reason=curl_missing），不建立 reverse。"""
    client, adb = _make_real_client(monkeypatch, _speed_responses(with_curl=False), tmp_path)
    resp = client.post("/api/devices/R3CN80ABCDEF/speedtest", json={})
    assert resp.status_code == 503
    error = resp.json()["error"]
    assert error["code"] == "speedtest_unavailable"
    assert error["details"]["reason"] == "curl_missing"
    joined_commands = [" ".join(c[:-1]) for c in adb.commands]
    assert not any(c.startswith("reverse") for c in joined_commands)


@pytest.mark.parametrize(
    "curl_output,curl_rc,reason",
    [
        ("", 1, "curl_failed"),                # curl 退出失败（超时/链路断）
        ("curl: (7) Failed to connect", 7, "curl_failed"),
        ("0.0\n", 0, "curl_zero_transfer"),  # rc=0 但 0 字节传输（不返回 0 填充结果）
    ],
)
def test_speedtest_real_curl_failure_503(
    monkeypatch: pytest.MonkeyPatch, curl_output: str, curl_rc: int, reason: str,
    tmp_path: Path,
) -> None:
    client, _ = _make_real_client(
        monkeypatch, _speed_responses(curl_output=curl_output, curl_rc=curl_rc), tmp_path
    )
    resp = client.post("/api/devices/R3CN80ABCDEF/speedtest", json={})
    assert resp.status_code == 503, resp.text
    error = resp.json()["error"]
    assert error["code"] == "speedtest_unavailable"
    assert error["details"]["reason"] == reason


# ---- 纯函数层 ----


def test_parse_speed_download() -> None:
    assert parse_speed_download("5242880.0\n") == 5242880.0
    assert parse_speed_download("  1234.5 \r\n") == 1234.5
    assert parse_speed_download("") is None
    assert parse_speed_download("\n") is None
    assert parse_speed_download("curl: (7) Failed") is None
    assert parse_speed_download("-1.0") is None  # 负速率不合法，不猜


def test_mbps_conversion() -> None:
    assert mbps_from_bytes_per_sec(1_000_000.0) == 8.0  # 十进制 Mbit/s
    assert mbps_from_bytes_per_sec(0.0) == 0.0
    assert mbps_from_bytes_per_sec(125_000.0) == 1.0


def test_curl_command_quoting() -> None:
    cmd = curl_command("http://127.0.0.1:41234/api/speedtest/payload?bytes=42", max_time_s=60)
    assert cmd[:5] == ["curl", "-s", "-o", "/dev/null", "-w"]
    assert cmd[5] == "'%{speed_download}'"  # 单引号在设备 shell 剥离
    assert "--max-time" in cmd and "60" in cmd
    assert cmd[-1] == "'http://127.0.0.1:41234/api/speedtest/payload?bytes=42'"


def test_payload_chunks_total() -> None:
    assert sum(len(c) for c in payload_chunks(1000)) == 1000
    assert sum(len(c) for c in payload_chunks(64 * 1024 * 3 + 5)) == 64 * 1024 * 3 + 5
    with pytest.raises(ValueError):
        list(payload_chunks(0))
