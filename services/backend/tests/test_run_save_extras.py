"""S2 落盘扩展测试（save 面新产物，2026-11-10）：

- power-energy-crosscheck：会话级功耗双链互核 → summary.json 顶层可选
  powerCrosscheck 块（主链 Σ电流积分 vs 降级链 charge_full×|Δcapacity|）；
- version-metadata：save 时应用版本探测 → manifest.appVersion + summary 顶层
  app 身份块（对比引擎 same_device_versions 消费）；
- web-runs-no-offline-html-report：save 写出离线 report.html（perfeval
  write_summary_report 同一消费面），无数据 run 不产出（不伪造）。

如实标注：live 真值复验挂起（SM_F9560 电池 sysfs 被 SELinux 屏蔽，设备池约束，
见缺口 notes）——功耗互核以合成双链样本验收；版本探测/报告以 fake provider
与合成样本验收，真机冒烟在设备可用时补录。
"""

from __future__ import annotations

import json
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from perfeval.collectors.android.samples import SampleRecord  # noqa: E402
from perfeval.core.compare.engine import compare_summaries  # noqa: E402
from perfeval.core.summary import aggregate_summary  # noqa: E402

from app.config import Settings  # noqa: E402
from app.services.session_manager import Session, SessionManager  # noqa: E402
from app.sse import now_ms  # noqa: E402


# ---- fake provider（duck-typing app_version 探测面，同 launch_app 注入哲学）----


class _FakeProvider:
    """带可选 app_version 探测面的 provider 替身（save 面只需该接口）。"""

    mode = "real"

    def __init__(
        self,
        version: dict[str, Any] | None = None,
        probe_raises: bool = False,
    ) -> None:
        self._version = version
        self._probe_raises = probe_raises
        self.calls: list[tuple[str, str]] = []

    def app_version(self, device_id: str, package_name: str) -> dict[str, Any] | None:
        self.calls.append((device_id, package_name))
        if self._probe_raises:
            raise RuntimeError("dumpsys package boom")
        return self._version


class _NoProbeProvider:
    """无 app_version 探测面的 provider 替身（synthetic/simulator 形态）。"""

    mode = "synthetic"


def _manager(tmp_path: Path, provider: Any) -> SessionManager:
    settings = Settings(
        provider="synthetic",
        runs_dir=tmp_path / "runs",
        cases_db_path=tmp_path / "data" / "cases.db",
    )
    return SessionManager(provider, settings)


def _energy_session(
    tmp_path: Path,
    records: list[tuple[str, float, dict[str, Any]]],
    provider: Any | None = None,
) -> tuple[SessionManager, Session]:
    """构造直存 monitoring 会话并注入能量/指标样本（epoch 时戳，不触发归一）。"""
    manager = _manager(tmp_path, provider or _NoProbeProvider())
    created = now_ms()
    session = Session(
        session_id="sess-save-x",
        device_id="fake-real-001",
        package_name="com.example.game",
        metrics=[{"metricId": "power.energy.mah", "intervalMs": 1000}],
        provider_mode="real",
        created_at=created,
        runs_dir=tmp_path / "runs",
    )
    for metric_id, value, tags in records:
        session.append_record(
            SampleRecord(
                metric_id=metric_id,
                value=value,
                ts_ms=created + len(session.records()) * 1000 + 1000,
                tags=tags,
            )
        )
    return manager, session


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _primary(value: float) -> tuple[str, float, dict[str, Any]]:
    return ("power.energy.mah", value, {"source": "current_integral", "unit": "mAh"})


def _degraded(value: float) -> tuple[str, float, dict[str, Any]]:
    return ("power.energy.mah", value, {"source": "level_slope", "unit": "mAh"})


# ---- 功耗会话级互核（power-energy-crosscheck）----


def test_power_crosscheck_dual_chain(tmp_path: Path) -> None:
    """双链样本 → summary 顶层 powerCrosscheck：主链/降级链累计与偏差率。"""
    manager, session = _energy_session(
        tmp_path,
        [
            _primary(0.5), _primary(0.6), _primary(0.7), _primary(0.8),  # 2.6 mAh
            _degraded(0.9), _degraded(0.7), _degraded(0.8),              # 2.4 mAh
        ],
    )
    manager.save(session, "crosscheck_dual")
    run_path = tmp_path / "runs" / "crosscheck_dual"
    summary = _read_json(run_path / "summary.json")
    crosscheck = summary["powerCrosscheck"]
    assert crosscheck["primaryMah"] == 2.6
    assert crosscheck["degradedMah"] == 2.4
    assert crosscheck["deviationPct"] == 8.33  # |2.6−2.4|/2.4
    assert crosscheck["basis"] == "dual_chain"
    assert "charge_full" in crosscheck["note"]  # 口径声明随块透出


def test_power_crosscheck_primary_only_not_fabricated(tmp_path: Path) -> None:
    """单链（无降级链能量记录）→ basis=primary_only，降级值 null 不伪造。"""
    manager, session = _energy_session(tmp_path, [_primary(1.5), _primary(0.9)])
    manager.save(session, "crosscheck_primary")
    summary = _read_json((tmp_path / "runs" / "crosscheck_primary") / "summary.json")
    crosscheck = summary["powerCrosscheck"]
    assert crosscheck["primaryMah"] == 2.4
    assert crosscheck["degradedMah"] is None
    assert crosscheck["deviationPct"] is None
    assert crosscheck["basis"] == "primary_only"


def test_power_crosscheck_degraded_only(tmp_path: Path) -> None:
    """仅降级链（current_now 缺失形态）→ basis=degraded_only，主链 null 不伪造。"""
    manager, session = _energy_session(tmp_path, [_degraded(1.0), _degraded(0.5)])
    manager.save(session, "crosscheck_degraded")
    summary = _read_json((tmp_path / "runs" / "crosscheck_degraded") / "summary.json")
    crosscheck = summary["powerCrosscheck"]
    assert crosscheck["primaryMah"] is None
    assert crosscheck["degradedMah"] == 1.5
    assert crosscheck["basis"] == "degraded_only"


def test_power_crosscheck_absent_without_power_records(tmp_path: Path) -> None:
    """无能量记录 → 不产 powerCrosscheck 块（不伪造零值）。"""
    created = now_ms()
    manager = _manager(tmp_path, _NoProbeProvider())
    session = Session(
        session_id="sess-no-power",
        device_id="fake-real-001",
        package_name="com.example.game",
        metrics=[{"metricId": "cpu.total.pct", "intervalMs": 1000}],
        provider_mode="real",
        created_at=created,
        runs_dir=tmp_path / "runs",
    )
    session.append_record(
        SampleRecord(metric_id="cpu.total.pct", value=12.0, ts_ms=created + 1000)
    )
    manager.save(session, "crosscheck_absent")
    summary = _read_json((tmp_path / "runs" / "crosscheck_absent") / "summary.json")
    assert "powerCrosscheck" not in summary


# ---- 应用版本元数据（version-metadata-missing-trend）----


def test_app_version_manifest_and_summary_context(tmp_path: Path) -> None:
    """探测面可用 → manifest.appVersion 快照 + summary 顶层 app 身份块。"""
    provider = _FakeProvider(version={"versionName": "1.2.3", "versionCode": 123})
    created = now_ms()
    manager = _manager(tmp_path, provider)
    session = Session(
        session_id="sess-version",
        device_id="fake-real-001",
        package_name="com.example.game",
        metrics=[{"metricId": "cpu.total.pct", "intervalMs": 1000}],
        provider_mode="real",
        created_at=created,
        runs_dir=tmp_path / "runs",
    )
    session.append_record(
        SampleRecord(metric_id="cpu.total.pct", value=12.0, ts_ms=created + 1000)
    )
    manager.save(session, "version_ok")
    run_path = tmp_path / "runs" / "version_ok"
    manifest = _read_json(run_path / "manifest.json")
    assert manifest["appVersion"] == {"versionName": "1.2.3", "versionCode": 123}
    # 既有 manifest 字段不受字段集扩展影响
    assert manifest["schemaVersion"] == "perfeval.manifest.v1"
    assert manifest["packageName"] == "com.example.game"
    summary = _read_json(run_path / "summary.json")
    assert summary["app"] == {
        "packageName": "com.example.game",
        "versionName": "1.2.3",
        "versionCode": 123,
    }


def test_app_version_probe_failure_keeps_manifest_null(tmp_path: Path) -> None:
    """版本探测失败（权限拒绝/抛错形态）→ manifest.appVersion 保留 null，不伪造。"""
    provider = _FakeProvider(version=None, probe_raises=True)
    created = now_ms()
    manager = _manager(tmp_path, provider)
    session = Session(
        session_id="sess-version-fail",
        device_id="fake-real-001",
        package_name="com.example.game",
        metrics=[{"metricId": "cpu.total.pct", "intervalMs": 1000}],
        provider_mode="real",
        created_at=created,
        runs_dir=tmp_path / "runs",
    )
    session.append_record(
        SampleRecord(metric_id="cpu.total.pct", value=12.0, ts_ms=created + 1000)
    )
    manager.save(session, "version_fail")
    run_path = tmp_path / "runs" / "version_fail"
    manifest = _read_json(run_path / "manifest.json")
    assert manifest["appVersion"] is None
    assert manifest["packageName"] == "com.example.game"  # 字段集保留
    summary = _read_json(run_path / "summary.json")
    assert "app" not in summary  # 探测失败不伪造身份块


def test_app_version_provider_without_probe_face(tmp_path: Path) -> None:
    """provider 无探测面（synthetic/simulator）→ manifest.appVersion 置 null。"""
    created = now_ms()
    manager = _manager(tmp_path, _NoProbeProvider())
    session = Session(
        session_id="sess-no-probe",
        device_id="synthetic-001",
        package_name="com.example.game",
        metrics=[{"metricId": "cpu.total.pct", "intervalMs": 1000}],
        provider_mode="synthetic",
        created_at=created,
        runs_dir=tmp_path / "runs",
    )
    session.append_record(
        SampleRecord(metric_id="cpu.total.pct", value=12.0, ts_ms=created + 1000)
    )
    manager.save(session, "version_noprobe")
    manifest = _read_json((tmp_path / "runs" / "version_noprobe") / "manifest.json")
    assert manifest["appVersion"] is None


def test_same_device_versions_recognizes_version_change() -> None:
    """same_device_versions 冒烟：两个不同版本 run 识别版本变化；同版本被拦截。

    对比引擎身份字段（compare engine._identity_from_summary 读 summary["app"]
    的 versionName/versionCode）随 summary 顶层 app 块在 Web run 可用——perfeval
    对比面零改动。
    """
    run_id_a = "run-version-a"
    run_id_b = "run-version-b"
    observations = [{"value": 12.0, "unit": "%"}]

    def _summary(run_id: str, version: dict[str, Any]) -> dict[str, Any]:
        return aggregate_summary(
            run_id,
            metric_inputs={"cpu.total.pct": observations},
            context={"app": {"packageName": "com.example.game", **version}},
        )

    baseline = _summary(run_id_a, {"versionName": "1.0.0", "versionCode": 100})
    candidate = _summary(run_id_b, {"versionName": "1.1.0", "versionCode": 110})
    changed = compare_summaries(baseline, candidate, mode="same_device_versions")
    assert changed["status"] != "invalid"
    assert "app_version_not_changed" not in changed.get("errors", [])

    same = compare_summaries(
        baseline,
        _summary(run_id_b, {"versionName": "1.0.0", "versionCode": 100}),
        mode="same_device_versions",
    )
    assert same["status"] == "invalid"
    assert "app_version_not_changed" in same["errors"]


# ---- Web run 离线 HTML 报告（web-runs-no-offline-html-report）----


class _TextParser(HTMLParser):
    """抽取 HTML 文本（指标行非空断言用）。"""

    def __init__(self) -> None:
        super().__init__()
        self.chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.chunks.append(text)


def test_report_html_generated_for_run_with_data(tmp_path: Path) -> None:
    """有数据 run → report.html 可解析、指标行非空；既有 5 类落盘文件不改动。"""
    created = now_ms()
    manager = _manager(tmp_path, _NoProbeProvider())
    session = Session(
        session_id="sess-report",
        device_id="fake-real-001",
        package_name="com.example.game",
        metrics=[{"metricId": "cpu.total.pct", "intervalMs": 1000}],
        provider_mode="real",
        created_at=created,
        runs_dir=tmp_path / "runs",
    )
    for offset in range(3):
        session.append_record(
            SampleRecord(metric_id="cpu.total.pct", value=10.0 + offset, ts_ms=created + (offset + 1) * 1000)
        )
    manager.save(session, "report_ok")
    run_path = tmp_path / "runs" / "report_ok"
    # 既有 5 类落盘文件形状不变（report.html 仅叠加，runs 只读约束回归）
    assert {p.name for p in run_path.iterdir()} == {
        "samples.ndjson", "timeline.ndjson", "summary.json", "manifest.json",
        "report.html", "artifacts",
    }
    html_text = (run_path / "report.html").read_text(encoding="utf-8")
    parser = _TextParser()
    parser.feed(html_text)
    text = "\n".join(parser.chunks)
    assert "cpu.total.pct" in text  # 指标行非空（指标身份出现在报告中）


def test_report_html_not_generated_for_run_without_data(tmp_path: Path) -> None:
    """无数据 run → 不产 report.html（同 summary「不伪造」原则；空数据不产块）。"""
    created = now_ms()
    manager = _manager(tmp_path, _NoProbeProvider())
    session = Session(
        session_id="sess-no-report",
        device_id="fake-real-001",
        package_name="com.example.game",
        metrics=[{"metricId": "cpu.total.pct", "intervalMs": 1000}],
        provider_mode="real",
        created_at=created,
        runs_dir=tmp_path / "runs",
    )
    session.append_null_record("cpu.total.pct")  # value:null 点不算数值样本
    manager.save(session, "report_empty")
    run_path = tmp_path / "runs" / "report_empty"
    assert not (run_path / "report.html").exists()
    assert not (run_path / "summary.json").exists()
    assert (run_path / "manifest.json").exists()
