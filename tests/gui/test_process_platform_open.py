from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QDesktopServices

from perfeval.gui.models.result import ResultDescriptor, ResultType, ResultViewer
from perfeval.gui.services.platform_open import OpenStatus, PlatformOpenService


def _descriptor(path: Path, *, viewer: ResultViewer, result_type: ResultType) -> ResultDescriptor:
    return ResultDescriptor(
        type=result_type,
        label=path.name,
        location=str(path),
        exists=path.exists(),
        viewer=viewer,
    )


def test_process_platform_open_uses_desktop_services_for_local_file_and_directory(
    qtbot, monkeypatch, tmp_path: Path
) -> None:
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url) or True)
    report = tmp_path / "报告.html"
    report.write_text("report", encoding="utf-8")
    service = PlatformOpenService(cli_program="perfeval", allowed_roots=(tmp_path,))

    file_result = service.open_static(
        _descriptor(report, viewer=ResultViewer.REPORT, result_type=ResultType.REPORT)
    )
    directory_result = service.open_descriptor(
        _descriptor(tmp_path, viewer=ResultViewer.DIRECTORY, result_type=ResultType.DIRECTORY)
    )

    assert file_result.status == OpenStatus.OPENED
    assert directory_result.status == OpenStatus.OPENED
    assert [url.toLocalFile() for url in opened] == [str(report), str(tmp_path)]


def test_process_platform_open_rejects_missing_external_and_nonlocal_url(tmp_path: Path) -> None:
    service = PlatformOpenService(cli_program="perfeval", allowed_roots=(tmp_path,))
    outside = tmp_path.parent / "outside.trace"
    outside.write_text("trace", encoding="utf-8")

    external = service.open_descriptor(
        _descriptor(outside, viewer=ResultViewer.SYSTEM, result_type=ResultType.ARTIFACT)
    )
    missing = service.open_descriptor(
        _descriptor(tmp_path / "missing", viewer=ResultViewer.SYSTEM, result_type=ResultType.ARTIFACT)
    )
    url = service.open_descriptor(
        ResultDescriptor(ResultType.URL, "external", "https://ui.perfetto.dev", True)
    )

    assert external.status == OpenStatus.REJECTED
    assert missing.status == OpenStatus.FAILED
    assert url.status == OpenStatus.REJECTED


def test_process_platform_open_requires_explicit_report_and_perfetto_confirmation(
    qtbot, tmp_path: Path
) -> None:
    report = tmp_path / "report.html"
    report.write_text("report", encoding="utf-8")
    trace = tmp_path / "capture.perfetto-trace"
    trace.write_bytes(b"trace")
    service = PlatformOpenService(
        cli_program=sys.executable,
        cli_prefix_arguments=("-c", "import sys; raise SystemExit(0)"),
        working_directory=tmp_path,
        allowed_roots=(tmp_path,),
        serve_seconds=30,
    )
    report_descriptor = _descriptor(
        report, viewer=ResultViewer.REPORT, result_type=ResultType.REPORT
    )
    trace_descriptor = _descriptor(
        trace, viewer=ResultViewer.PERFETTO, result_type=ResultType.ARTIFACT
    )

    assert service.open_descriptor(report_descriptor).status == OpenStatus.CONFIRM_LOCAL_SERVER
    assert service.open_descriptor(trace_descriptor).status == OpenStatus.CONFIRM_EXTERNAL_SITE
    assert service.active_viewer_count == 0

    with qtbot.waitSignal(service.viewer_finished, timeout=5000):
        started = service.open_descriptor(report_descriptor, confirm_local_server=True)
    assert started.status == OpenStatus.VIEWER_STARTED
    assert started.success

    with qtbot.waitSignal(service.viewer_finished, timeout=5000):
        started = service.open_descriptor(trace_descriptor, confirm_external_site=True)
    assert started.status == OpenStatus.VIEWER_STARTED
    assert service.active_viewer_count == 0

