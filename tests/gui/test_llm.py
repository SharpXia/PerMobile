from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QSignalSpy

from perfeval.gui.models.llm import (
    InterpretationDepth,
    InterpretationSource,
    InterpretationState,
    LlmApiFormat,
    LlmProfile,
)
from perfeval.gui.services.llm_artifacts import (
    MAX_INPUT_BYTES,
    build_interpretation_record,
    build_prompt,
    inject_report_link,
    read_snapshot,
    render_interpretation_html,
    source_from_history,
    write_interpretation,
)
from perfeval.gui.services.llm_jobs import InterpretationJobManager
from perfeval.gui.services.llm_profiles import CredentialStore, LlmProfileService
from perfeval.gui.services.llm_protocols import (
    LlmProtocolError,
    build_request,
    parse_response,
    safe_error_message,
    validate_base_url,
)
from perfeval.gui.services.settings import SettingsService
from perfeval.gui.services.workspace import WorkspaceService
from perfeval.gui.pages.settings_page import SettingsPage
from perfeval.gui.services.history_service import HistoryService
from perfeval.gui.widgets.llm_interpretation_panel import LlmInterpretationPanel


class FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


def _settings(tmp_path: Path) -> SettingsService:
    backend = QSettings(str(tmp_path / "llm-settings.ini"), QSettings.Format.IniFormat)
    backend.setFallbacksEnabled(False)
    return SettingsService(backend)


def _profile(api_format: LlmApiFormat, base_url: str = "https://example.com/v1") -> LlmProfile:
    return LlmProfile("profile-1", "测试模型", api_format, base_url, "model-1")


def test_llm_profiles_persist_metadata_but_not_api_key(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    backend = FakeKeyring()
    service = LlmProfileService(settings, credentials=CredentialStore(backend))
    profile = _profile(LlmApiFormat.OPENAI_CHAT)

    assert service.save_profile(profile, api_key="top-secret", make_default=True)
    service.interpretation_depth = InterpretationDepth.DEEP

    restored = LlmProfileService(settings, credentials=CredentialStore(backend))
    assert restored.profiles == (profile,)
    assert restored.default_profile == profile
    assert restored.api_key(profile.profile_id) == "top-secret"
    assert restored.interpretation_depth is InterpretationDepth.DEEP
    assert "top-secret" not in (tmp_path / "llm-settings.ini").read_text(encoding="utf-8")

    restored.delete_profile(profile.profile_id)
    assert restored.profiles == ()
    assert restored.api_key(profile.profile_id) is None


@pytest.mark.parametrize(
    ("api_format", "suffix", "auth_header"),
    [
        (LlmApiFormat.OPENAI_CHAT, "/chat/completions", "Authorization"),
        (LlmApiFormat.ANTHROPIC_MESSAGES, "/messages", "x-api-key"),
        (
            LlmApiFormat.GEMINI_GENERATE_CONTENT,
            "/models/model-1:generateContent",
            "x-goog-api-key",
        ),
    ],
)
def test_llm_protocols_build_expected_requests(api_format, suffix, auth_header) -> None:
    request = build_request(
        _profile(api_format),
        "secret",
        system_prompt="system",
        user_prompt="user",
    )

    assert request.url.endswith(suffix)
    assert auth_header in request.headers
    assert b"secret" not in request.body
    body = json.loads(request.body)
    assert body["model"] == "model-1" if api_format is not LlmApiFormat.GEMINI_GENERATE_CONTENT else True
    if api_format is LlmApiFormat.GEMINI_GENERATE_CONTENT:
        assert body["generationConfig"]["maxOutputTokens"] == 3000
    else:
        assert body["max_tokens"] == 3000


def test_llm_protocols_parse_all_supported_responses() -> None:
    openai = parse_response(
        LlmApiFormat.OPENAI_CHAT,
        {"choices": [{"message": {"content": "openai"}}], "usage": {"total_tokens": 3}},
    )
    anthropic = parse_response(
        LlmApiFormat.ANTHROPIC_MESSAGES,
        {"content": [{"type": "text", "text": "anthropic"}]},
    )
    gemini = parse_response(
        LlmApiFormat.GEMINI_GENERATE_CONTENT,
        {"candidates": [{"content": {"parts": [{"text": "gemini"}]}}]},
    )

    assert (openai.text, anthropic.text, gemini.text) == ("openai", "anthropic", "gemini")
    assert openai.usage["total_tokens"] == 3


def test_llm_url_validation_accepts_http_and_https() -> None:
    assert validate_base_url("http://127.0.0.1:8080/v1") == "http://127.0.0.1:8080/v1"
    assert validate_base_url("http://api.example.com/v1/") == "http://api.example.com/v1"
    assert validate_base_url("https://api.example.com/v1/") == "https://api.example.com/v1"
    with pytest.raises(LlmProtocolError):
        validate_base_url("https://user:pass@example.com/v1")
    with pytest.raises(LlmProtocolError):
        validate_base_url("https://example.com/v1?api_key=secret")


def test_llm_error_message_redacts_credentials() -> None:
    message = safe_error_message(
        b'{"error":{"message":"Authorization: Bearer top-secret; api_key=top-secret"}}',
        status_code=401,
        secrets=("top-secret",),
    )

    assert "top-secret" not in message
    assert "[REDACTED]" in message


def test_llm_prompt_is_bounded_and_never_reads_binary_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "run"
    root.mkdir()
    summary = root / "summary.json"
    summary.write_text(json.dumps({"metrics": {"large": "x" * 500_000}}), encoding="utf-8")
    apk = root / "app.apk"
    apk.write_bytes(b"binary-secret-that-must-not-be-uploaded")
    source = InterpretationSource(
        "run-1",
        "大报告",
        root,
        data_paths=(summary,),
        artifact_paths=(summary, apk),
    )

    bundle = build_prompt(source)

    assert bundle.input_bytes <= MAX_INPUT_BYTES
    assert bundle.truncated
    assert "binary-secret-that-must-not-be-uploaded" not in bundle.user_prompt
    assert '"suffix":".apk"' in bundle.user_prompt


def test_llm_prompt_depth_controls_detail_and_bounded_log_excerpts(tmp_path: Path) -> None:
    root = tmp_path / "run"
    root.mkdir()
    summary = root / "summary.json"
    summary.write_text(json.dumps({"status": "warn", "metrics": {"jank": 0.18}}), encoding="utf-8")
    logcat = root / "logcat.txt"
    logcat.write_text(
        "BEGIN FATAL EXCEPTION Authorization: Bearer log-secret\n"
        + "x" * 120_000
        + "\nTAIL ANR marker\n",
        encoding="utf-8",
    )
    source = InterpretationSource(
        "run-depth",
        "深度解读",
        root,
        data_paths=(summary,),
        log_paths=(logcat,),
    )

    brief = build_prompt(source, InterpretationDepth.BRIEF)
    deep = build_prompt(source, InterpretationDepth.DEEP)

    assert brief.depth is InterpretationDepth.BRIEF
    assert deep.depth is InterpretationDepth.DEEP
    assert deep.max_output_tokens > brief.max_output_tokens
    assert deep.input_bytes > brief.input_bytes
    assert deep.log_count == brief.log_count == 1
    assert deep.logs_truncated and brief.logs_truncated
    assert "根因只能作为待验证假设" in deep.user_prompt
    assert "BEGIN FATAL EXCEPTION" in deep.user_prompt
    assert "TAIL ANR marker" in deep.user_prompt
    assert "log-secret" not in deep.user_prompt
    assert "[REDACTED]" in deep.user_prompt
    assert deep.input_bytes <= MAX_INPUT_BYTES


def test_llm_prompt_calibrates_tiny_frame_samples_and_caps_severity(tmp_path: Path) -> None:
    root = tmp_path / "run"
    root.mkdir()
    summary = root / "summary.json"
    summary.write_text(
        json.dumps(
            {
                "metrics": {
                    "frame.total_count": {"value": 9, "min": 9, "max": 11, "n": 3},
                    "frame.janky_count": {"value": 5},
                    "frame.jank_rate": {"value": 0.5455},
                    "frame.duration.p50.ms": {"value": 13},
                    "frame.duration.p95.ms": {"value": 65, "max": 69},
                    "stability.anr.count": {"value": 0},
                    "stability.crash.count": {"value": 0},
                }
            }
        ),
        encoding="utf-8",
    )
    source = InterpretationSource("run-small", "小样本流畅度", root, data_paths=(summary,))

    bundle = build_prompt(source, InterpretationDepth.DEEP)

    assert bundle.assessment_calibration["frameEvidence"]["quality"] == "very_low"
    assert bundle.assessment_calibration["frameEvidence"]["severityCeiling"] == "medium"
    assert "预算失败只表示命中当前测试配置的阈值" in bundle.system_prompt
    assert "少于 20 时，P90/P95 往往接近最慢一两帧" in bundle.user_prompt
    assert '"severityCeiling":"medium"' in bundle.user_prompt

    response = json.dumps(
        {
            "executiveSummary": "渲染性能严重恶化。",
            "findings": [
                {
                    "severity": "critical",
                    "title": "卡顿率极高",
                    "evidence": "frame.jank_rate=54.55%",
                    "analysis": "超过预算",
                    "recommendation": "复测",
                },
                {
                    "severity": "high",
                    "title": "内存偏高",
                    "evidence": "memory.pss.peak_mb=443",
                    "analysis": "需要关注",
                    "recommendation": "分析内存",
                },
            ],
            "limitations": [],
        },
        ensure_ascii=False,
    )
    record = build_interpretation_record(
        source,
        _profile(LlmApiFormat.OPENAI_CHAT),
        bundle,
        response,
        {},
    )

    assert record["result"]["findings"][0]["severity"] == "medium"
    assert record["result"]["findings"][1]["severity"] == "high"
    assert record["assessmentCalibration"]["adjustedFindingCount"] == 1
    assert any("不能据此判定实际体验存在严重卡顿" in item for item in record["result"]["limitations"])

    html_report = render_interpretation_html(source, record)
    assert "证据质量校准" in html_report
    assert "每轮仅采集 9–11 帧" in html_report
    assert "自动下调 1 个流畅度问题" in html_report


def test_llm_artifacts_render_escape_and_link_both_reports(tmp_path: Path) -> None:
    root = tmp_path / "run"
    report = root / "report" / "report.html"
    summary = root / "summary.json"
    report.parent.mkdir(parents=True)
    report.write_text("<!doctype html><html><body><h1>原始报告</h1></body></html>", encoding="utf-8")
    summary.write_text(json.dumps({"status": "warn"}), encoding="utf-8")
    (root / "experiment.json").write_text(
        json.dumps({"schemaVersion": "perfeval.gui.experiment.v1", "experimentId": "run-1"}),
        encoding="utf-8",
    )
    source = InterpretationSource(
        "run-1",
        "报告 <script>alert(1)</script>",
        root,
        report_paths=(report,),
        data_paths=(summary,),
    )
    profile = _profile(LlmApiFormat.OPENAI_CHAT)
    bundle = build_prompt(source, InterpretationDepth.DEEP)
    response = json.dumps(
        {
            "executiveSummary": "发现 <b>风险</b>",
            "findings": [
                {
                    "severity": "high",
                    "title": "启动较慢",
                    "evidence": "startup.cold.ms=1800",
                    "analysis": "超过参考范围",
                    "recommendation": "检查主线程初始化",
                }
            ],
            "positives": ["无崩溃"],
            "nextSteps": ["复测"],
            "limitations": [],
        },
        ensure_ascii=False,
    )
    record = build_interpretation_record(source, profile, bundle, response, {})
    record["generatedAtUtc"] = "2026-08-18T06:04:12.607009+00:00"

    descriptor = write_interpretation(source, profile, record)
    inject_report_link(report, source.interpretation_html_path)

    interpretation_html = source.interpretation_html_path.read_text(encoding="utf-8")
    report_html = report.read_text(encoding="utf-8")
    assert descriptor.exists
    assert "发现 &lt;b&gt;风险&lt;/b&gt;" in interpretation_html
    assert "<script>alert(1)</script>" not in interpretation_html
    assert "返回原始报告" in interpretation_html
    assert report_html.count("perfeval-llm-link:start") == 1
    assert "查看 LLM 解读报告" in report_html
    snapshot = read_snapshot(root)
    assert snapshot.state is InterpretationState.COMPLETED
    assert snapshot.depth is InterpretationDepth.DEEP
    assert "解读档位" in interpretation_html
    assert "深度" in interpretation_html
    assert "生成时间（北京时间）" in interpretation_html
    assert "2026-08-18 14:04:12" in interpretation_html
    assert "2026-08-18T06:04:12.607009+00:00" not in interpretation_html
    manifest = json.loads((root / "experiment.json").read_text(encoding="utf-8"))
    assert manifest["llmInterpretation"]["htmlPath"] == "llm/interpretation.html"
    assert manifest["llmInterpretation"]["depth"] == InterpretationDepth.DEEP.value


def test_llm_artifacts_reject_symlink_output_directory(tmp_path: Path) -> None:
    root = tmp_path / "run"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        (root / "llm").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前平台不允许创建符号链接")
    summary = root / "summary.json"
    summary.write_text("{}", encoding="utf-8")
    source = InterpretationSource("run", "不安全目录", root, data_paths=(summary,))
    profile = _profile(LlmApiFormat.OPENAI_CHAT)
    bundle = build_prompt(source)
    record = build_interpretation_record(
        source,
        profile,
        bundle,
        json.dumps({"executiveSummary": "ok"}),
        {},
    )

    with pytest.raises(ValueError, match="符号链接"):
        write_interpretation(source, profile, record)
    assert not (outside / "interpretation.json").exists()

    settings = _settings(tmp_path)
    profiles = LlmProfileService(settings, credentials=CredentialStore(FakeKeyring()))
    profiles.save_profile(profile, api_key="secret", make_default=True)
    manager = InterpretationJobManager(profiles)
    with pytest.raises(ValueError, match="符号链接"):
        manager.start(source)
    assert manager.active_count == 0


class _OpenAiHandler(BaseHTTPRequestHandler):
    request_bodies: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 - stdlib API
        size = int(self.headers.get("Content-Length", "0"))
        self.__class__.request_bodies.append(json.loads(self.rfile.read(size)))
        content = json.dumps(
            {
                "executiveSummary": "连接正常，报告可解读。",
                "findings": [],
                "positives": [],
                "nextSteps": ["继续复核原始数据"],
                "limitations": [],
            },
            ensure_ascii=False,
        )
        payload = json.dumps(
            {
                "choices": [{"message": {"content": content}}],
                "usage": {"total_tokens": 42},
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        pass


class _RetryHandler(_OpenAiHandler):
    calls = 0

    def do_POST(self) -> None:  # noqa: N802 - stdlib API
        self.__class__.calls += 1
        if self.__class__.calls == 1:
            size = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(size)
            payload = b'{"error":{"message":"busy"}}'
            self.send_response(503)
            self.send_header("Retry-After", "0")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        super().do_POST()


class _EchoSecretHandler(_OpenAiHandler):
    def do_POST(self) -> None:  # noqa: N802 - stdlib API
        size = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(size)
        content = json.dumps(
            {
                "executiveSummary": "服务错误回显 leaky-api-key-987",
                "limitations": [],
            },
            ensure_ascii=False,
        )
        payload = json.dumps(
            {
                "choices": [{"message": {"content": content}}],
                "usage": {"provider_note": "leaky-api-key-987"},
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _FailHandler(_OpenAiHandler):
    def do_POST(self) -> None:  # noqa: N802 - stdlib API
        size = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(size)
        payload = b'{"error":{"message":"invalid credentials"}}'
        self.send_response(401)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def test_llm_job_manager_runs_in_background_and_persists_result(qtbot, tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        settings = _settings(tmp_path)
        profiles = LlmProfileService(settings, credentials=CredentialStore(FakeKeyring()))
        profile = _profile(
            LlmApiFormat.OPENAI_CHAT,
            f"http://127.0.0.1:{server.server_address[1]}/v1",
        )
        profiles.save_profile(profile, api_key="secret", make_default=True)
        manager = InterpretationJobManager(profiles, timeout_ms=5_000)
        root = tmp_path / "result"
        root.mkdir()
        summary = root / "summary.json"
        report = root / "report.html"
        summary.write_text(json.dumps({"status": "warn", "metrics": {"a": 1}}), encoding="utf-8")
        report.write_text("<html><body>report</body></html>", encoding="utf-8")
        source = InterpretationSource(
            "result-1",
            "后台解读",
            root,
            report_paths=(report,),
            data_paths=(summary,),
        )

        with qtbot.waitSignal(manager.job_completed, timeout=8_000):
            manager.start(
                source,
                profile.profile_id,
                depth=InterpretationDepth.DEEP,
            )

        assert manager.active_count == 0
        assert source.interpretation_json_path.is_file()
        assert source.interpretation_html_path.is_file()
        snapshot = read_snapshot(root)
        assert snapshot.state is InterpretationState.COMPLETED
        assert snapshot.depth is InterpretationDepth.DEEP
        assert _OpenAiHandler.request_bodies[-1]["model"] == "model-1"
        assert _OpenAiHandler.request_bodies[-1]["max_tokens"] == 6000
        record = json.loads(source.interpretation_json_path.read_text(encoding="utf-8"))
        assert record["interpretationDepth"]["id"] == InterpretationDepth.DEEP.value
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_llm_job_manager_retries_one_transient_failure(qtbot, tmp_path: Path) -> None:
    _RetryHandler.calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RetryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        settings = _settings(tmp_path)
        profiles = LlmProfileService(settings, credentials=CredentialStore(FakeKeyring()))
        profile = _profile(
            LlmApiFormat.OPENAI_CHAT,
            f"http://127.0.0.1:{server.server_address[1]}/v1",
        )
        profiles.save_profile(profile, api_key="secret", make_default=True)
        manager = InterpretationJobManager(profiles, timeout_ms=5_000)
        root = tmp_path / "retry"
        root.mkdir()
        summary = root / "summary.json"
        summary.write_text(json.dumps({"status": "warn"}), encoding="utf-8")
        source = InterpretationSource("retry", "重试", root, data_paths=(summary,))

        with qtbot.waitSignal(manager.job_completed, timeout=8_000):
            manager.start(source)

        assert _RetryHandler.calls == 2
        assert read_snapshot(root).state is InterpretationState.COMPLETED
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_llm_job_manager_redacts_key_echoed_by_provider(qtbot, tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EchoSecretHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        settings = _settings(tmp_path)
        profiles = LlmProfileService(settings, credentials=CredentialStore(FakeKeyring()))
        profile = _profile(
            LlmApiFormat.OPENAI_CHAT,
            f"http://127.0.0.1:{server.server_address[1]}/v1",
        )
        profiles.save_profile(profile, api_key="leaky-api-key-987", make_default=True)
        manager = InterpretationJobManager(profiles, timeout_ms=5_000)
        root = tmp_path / "redacted"
        root.mkdir()
        summary = root / "summary.json"
        summary.write_text(json.dumps({"status": "warn"}), encoding="utf-8")
        source = InterpretationSource("redacted", "响应脱敏", root, data_paths=(summary,))

        with qtbot.waitSignal(manager.job_completed, timeout=5_000):
            manager.start(source)

        for path in (
            source.interpretation_json_path,
            source.interpretation_html_path,
            source.status_path,
        ):
            assert "leaky-api-key-987" not in path.read_text(encoding="utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_failed_regeneration_preserves_previous_success(qtbot, tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FailHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        root = tmp_path / "preserve"
        root.mkdir()
        summary = root / "summary.json"
        summary.write_text(json.dumps({"status": "pass"}), encoding="utf-8")
        source = InterpretationSource("preserve", "保留旧结果", root, data_paths=(summary,))
        old_profile = _profile(LlmApiFormat.OPENAI_CHAT)
        bundle = build_prompt(source)
        old_record = build_interpretation_record(
            source,
            old_profile,
            bundle,
            json.dumps({"executiveSummary": "旧解读仍应保留"}, ensure_ascii=False),
            {},
        )
        write_interpretation(source, old_profile, old_record)
        old_html = source.interpretation_html_path.read_text(encoding="utf-8")

        settings = _settings(tmp_path)
        profiles = LlmProfileService(settings, credentials=CredentialStore(FakeKeyring()))
        failing_profile = _profile(
            LlmApiFormat.OPENAI_CHAT,
            f"http://127.0.0.1:{server.server_address[1]}/v1",
        )
        profiles.save_profile(failing_profile, api_key="bad-key", make_default=True)
        manager = InterpretationJobManager(profiles, timeout_ms=5_000)

        with qtbot.waitSignal(manager.job_failed, timeout=5_000):
            manager.start(source)

        assert source.interpretation_html_path.read_text(encoding="utf-8") == old_html
        assert read_snapshot(root).state is InterpretationState.FAILED
        assert read_snapshot(root).html_path == source.interpretation_html_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_llm_job_can_be_cancelled_while_queued(qtbot, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    profiles = LlmProfileService(settings, credentials=CredentialStore(FakeKeyring()))
    profile = _profile(LlmApiFormat.OPENAI_CHAT, "http://127.0.0.1:9/v1")
    profiles.save_profile(profile, api_key="secret", make_default=True)
    manager = InterpretationJobManager(profiles)
    root = tmp_path / "cancelled"
    root.mkdir()
    summary = root / "summary.json"
    summary.write_text(json.dumps({"status": "pass"}), encoding="utf-8")
    source = InterpretationSource("cancel-1", "取消测试", root, data_paths=(summary,))

    job_id = manager.start(source)
    assert manager.cancel(job_id)
    qtbot.waitUntil(lambda: manager.active_count == 0)

    assert read_snapshot(root).state is InterpretationState.CANCELLED
    assert not source.interpretation_html_path.exists()


def test_llm_settings_ui_saves_profiles_and_changes_default(qtbot, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    workspace = WorkspaceService(settings, default_workspace=tmp_path)
    profiles = LlmProfileService(settings, credentials=CredentialStore(FakeKeyring()))
    manager = InterpretationJobManager(profiles)
    page = SettingsPage(
        settings,
        workspace,
        llm_profiles=profiles,
        interpretation_manager=manager,
    )
    qtbot.addWidget(page)

    page.llm_name_edit.setText("OpenAI 网关")
    page.llm_model_edit.setText("model-a")
    page.llm_api_key_edit.setText("key-a")
    page.save_llm_profile_button.click()

    assert len(profiles.profiles) == 1
    assert profiles.default_profile is not None
    assert profiles.default_profile.name == "OpenAI 网关"

    page.new_llm_profile_button.click()
    page.llm_name_edit.setText("Claude")
    page.llm_format_combo.setCurrentIndex(
        page.llm_format_combo.findData(LlmApiFormat.ANTHROPIC_MESSAGES.value)
    )
    page.llm_url_edit.setText("https://api.anthropic.com/v1")
    page.llm_model_edit.setText("claude-model")
    page.llm_api_key_edit.setText("key-b")
    page.save_llm_profile_button.click()
    second_id = page._editing_llm_profile_id
    page.default_llm_profile_button.click()

    assert len(profiles.profiles) == 2
    assert profiles.default_profile_id == second_id
    assert "key-a" not in (tmp_path / "llm-settings.ini").read_text(encoding="utf-8")
    assert "key-b" not in (tmp_path / "llm-settings.ini").read_text(encoding="utf-8")


def test_llm_panel_routes_missing_configuration_to_settings(qtbot, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    profiles = LlmProfileService(settings, credentials=CredentialStore(FakeKeyring()))
    manager = InterpretationJobManager(profiles)
    panel = LlmInterpretationPanel(profiles, manager)
    qtbot.addWidget(panel)
    root = tmp_path / "result"
    root.mkdir()
    summary = root / "summary.json"
    summary.write_text(json.dumps({"status": "pass"}), encoding="utf-8")
    panel.set_source(InterpretationSource("result", "结果", root, data_paths=(summary,)))
    spy = QSignalSpy(panel.settings_requested)
    panel.depth_combo.setCurrentIndex(
        panel.depth_combo.findData(InterpretationDepth.DEEP.value)
    )

    panel.generate_button.click()

    assert spy.count() == 1
    assert "设置页" in panel.message_label.text()
    assert "AI 生成，仅供参考" in panel.disclosure_label.text()
    assert "日志片段" in panel.disclosure_label.text()
    assert profiles.interpretation_depth is InterpretationDepth.DEEP
    assert panel.depth_combo.count() == 3
    assert panel.message_label.isHidden()
    assert panel.layout().indexOf(panel.message_label) == -1
    assert panel.title_row.indexOf(panel.title_label) == 0
    assert panel.title_row.indexOf(panel.state_label) == 1
    assert panel.title_row.indexOf(panel.depth_label) == 3
    assert panel.title_row.indexOf(panel.depth_combo) == 4
    assert panel.depth_label.alignment() & Qt.AlignmentFlag.AlignRight
    assert panel.controls_row.indexOf(panel.profile_combo) == 0
    assert panel.controls_row.indexOf(panel.generate_button) == 1
    assert panel.controls_row.indexOf(panel.cancel_button) == 2
    assert panel.controls_row.indexOf(panel.view_button) == 3
    assert panel.controls_row.indexOf(panel.settings_button) == 4
    buttons = (
        panel.generate_button,
        panel.cancel_button,
        panel.view_button,
        panel.settings_button,
    )
    assert len({button.height() for button in buttons}) == 1
    assert all(button.minimumHeight() == button.maximumHeight() for button in buttons)


def test_history_service_discovers_llm_interpretation_without_affecting_health(tmp_path: Path) -> None:
    root = tmp_path / "runs" / "run-1"
    root.mkdir(parents=True)
    (root / "summary.json").write_text(
        json.dumps({"runId": "run-1", "status": "completed"}),
        encoding="utf-8",
    )
    (root / "report.html").write_text("<html><body>report</body></html>", encoding="utf-8")
    llm = root / "llm"
    llm.mkdir()
    (llm / "interpretation.html").write_text("<html><body>llm</body></html>", encoding="utf-8")
    (llm / "interpretation.json").write_text("{}", encoding="utf-8")
    (llm / "status.json").write_text(
        json.dumps(
            {
                "schemaVersion": "perfeval.llm.status.v1",
                "state": "completed",
                "processId": 1,
                "htmlPath": "interpretation.html",
            }
        ),
        encoding="utf-8",
    )

    entry = HistoryService(tmp_path).scan().entries[0]

    assert entry.health.value == "complete"
    assert entry.interpretation_state is InterpretationState.COMPLETED
    assert entry.interpretation_descriptor is not None
    assert entry.interpretation_descriptor.exists


def test_history_interpretation_source_discovers_related_log_files(tmp_path: Path) -> None:
    root = tmp_path / "runs" / "run-logs"
    root.mkdir(parents=True)
    (root / "summary.json").write_text(
        json.dumps({"runId": "run-logs", "status": "warn"}),
        encoding="utf-8",
    )
    logs = root / "logs"
    logs.mkdir()
    stderr = logs / "task.stderr.log"
    stderr.write_text("collector warning", encoding="utf-8")
    logcat = root / "repeats" / "r001" / "artifacts" / "android" / "logcat.txt"
    logcat.parent.mkdir(parents=True)
    logcat.write_text("FATAL EXCEPTION", encoding="utf-8")
    (root / "repeats" / "r001" / "artifacts" / "android" / "meminfo.txt").write_text(
        "not a log input",
        encoding="utf-8",
    )

    entry = HistoryService(tmp_path).scan().entries[0]
    source = source_from_history(entry)

    assert {path.name for path in source.log_paths} == {"task.stderr.log", "logcat.txt"}
