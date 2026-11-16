from __future__ import annotations

import json
from pathlib import Path

import pytest

from perfeval.gui.models.device import DeviceInfo, EnvironmentSnapshot, ToolStatus
from perfeval.gui.models.history import (
    HistoryArtifactKind,
    HistoryHealth,
    HistoryQuery,
    HistorySource,
    HistoryTaskKind,
)
from perfeval.gui.pages.history_page import HistoryPage
from perfeval.gui.pages.home_page import HomePage
from perfeval.gui.services.history_service import HistoryService
from perfeval.gui.services.platform_open import OpenResult, OpenStatus
from perfeval.gui.services.settings import SettingsService
from perfeval.gui.services.workspace import WorkspaceService
from PySide6.QtCore import QItemSelectionModel, QSettings, Qt
from PySide6.QtWidgets import QAbstractItemView, QMessageBox


class RecordingOpener:
    def __init__(self) -> None:
        self.static = []
        self.regular = []

    def open_static(self, descriptor):
        self.static.append(descriptor)
        return OpenResult(OpenStatus.OPENED, "已打开报告", success=True)

    def open_descriptor(self, descriptor):
        self.regular.append(descriptor)
        return OpenResult(OpenStatus.OPENED, "已打开目录", success=True)


def _settings(tmp_path: Path) -> SettingsService:
    backend = QSettings(str(tmp_path / "history.ini"), QSettings.Format.IniFormat)
    backend.setFallbacksEnabled(False)
    return SettingsService(backend)


def _seed_history(workspace: Path) -> None:
    good = workspace / "runs" / "good run"
    (good / "report").mkdir(parents=True)
    (good / "summary.json").write_text(
        json.dumps(
            {
                "schemaVersion": "perfeval.summary.v1",
                "scenarioId": "startup_home",
                "runId": "good-run",
                "status": "completed",
                "app": {"platform": "android"},
            }
        ),
        encoding="utf-8",
    )
    (good / "report" / "report.html").write_text("report", encoding="utf-8")

    broken = workspace / "runs" / "broken"
    broken.mkdir(parents=True)
    (broken / "summary.json").write_text("{not-json", encoding="utf-8")

    compare = workspace / "reports" / "candidate compare"
    compare.mkdir(parents=True)
    (compare / "compare.json").write_text(
        json.dumps(
            {
                "schemaVersion": "perfeval.compare.v1",
                "compareId": "baseline-vs-candidate",
                "status": "fail",
            }
        ),
        encoding="utf-8",
    )
    (compare / "report.html").write_text("compare report", encoding="utf-8")


def _seed_cached_experiment(
    workspace: Path,
    *,
    experiment_name: str = "20260812T080000.000000Z-page-smoothness-abc123",
    title: str = "页面流畅度",
    preset_id: str = "builtin.smoothness",
    scenario_id: str = "quick_smoothness",
) -> Path:
    experiment = (
        workspace
        / ".perfeval"
        / "experiments"
        / "2026"
        / "08"
        / experiment_name
    )
    report = experiment / "data" / scenario_id / "report" / "report.html"
    summary = experiment / "data" / scenario_id / "summary.json"
    report.parent.mkdir(parents=True)
    report.write_text("cached report", encoding="utf-8")
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(
        json.dumps({"scenarioId": scenario_id, "status": "pass"}),
        encoding="utf-8",
    )
    (experiment / "experiment.json").write_text(
        json.dumps(
            {
                "schemaVersion": "perfeval.gui.experiment.v1",
                "experimentId": experiment.name,
                "title": title,
                "presetId": preset_id,
                "taskKind": "run",
                "status": "pass",
                "packageName": "com.perfeval.gamebox",
                "endedAtUtc": "2026-08-12T08:00:00+00:00",
                "reportPath": report.relative_to(experiment).as_posix(),
                "summaryPath": summary.relative_to(experiment).as_posix(),
            }
        ),
        encoding="utf-8",
    )
    return experiment


def test_history_service_discovers_complete_partial_and_compare_entries(tmp_path: Path) -> None:
    _seed_history(tmp_path)

    result = HistoryService(tmp_path).scan()

    by_name = {entry.name: entry for entry in result.entries}
    assert set(by_name) == {"startup_home", "broken", "baseline-vs-candidate"}
    assert by_name["startup_home"].health == HistoryHealth.COMPLETE
    assert by_name["startup_home"].status == "completed"
    assert by_name["startup_home"].report_descriptor.exists
    assert by_name["startup_home"].task_kind is HistoryTaskKind.RUN
    assert by_name["startup_home"].source is HistorySource.LEGACY
    assert by_name["startup_home"].has_artifact(HistoryArtifactKind.REPORT)
    assert by_name["baseline-vs-candidate"].task_kind is HistoryTaskKind.COMPARE
    assert by_name["baseline-vs-candidate"].has_report
    assert by_name["baseline-vs-candidate"].status == "fail"
    assert by_name["broken"].health == HistoryHealth.PARTIAL
    assert by_name["broken"].status == "unknown"
    assert "无法解析" in by_name["broken"].warnings[0]


def test_history_service_discovers_persistent_experiment_cache(tmp_path: Path) -> None:
    experiment = _seed_cached_experiment(tmp_path)

    result = HistoryService(tmp_path).scan()

    entry = next(value for value in result.entries if value.name == "页面流畅度")
    assert entry.status == "pass"
    assert entry.health == HistoryHealth.COMPLETE
    assert entry.directory == experiment.resolve()
    assert entry.report_path == (
        experiment / "data" / "quick_smoothness" / "report" / "report.html"
    ).resolve()
    assert entry.summary["scenarioId"] == "quick_smoothness"
    assert entry.preset_id == "builtin.smoothness"
    assert entry.scenario_id == "quick_smoothness"
    assert entry.package_name == "com.perfeval.gamebox"
    assert entry.task_kind is HistoryTaskKind.RUN
    assert entry.source is HistorySource.QUICK_START
    assert entry.has_report


def test_history_page_defaults_to_user_selected_quick_start_scenarios(
    qtbot,
    tmp_path: Path,
) -> None:
    _seed_history(tmp_path)
    _seed_cached_experiment(tmp_path)
    settings = _settings(tmp_path)
    workspace = WorkspaceService(settings, default_workspace=tmp_path)
    page = HistoryPage(
        workspace_service=workspace,
        history_service=HistoryService(tmp_path),
    )
    qtbot.addWidget(page)

    with qtbot.waitSignal(page.scan_finished, timeout=5000):
        page.refresh_button.click()

    assert page.source_combo.currentData() == HistorySource.QUICK_START.value
    assert page.source_combo.accessibleName() == "历史任务来源筛选"
    assert page.table.horizontalHeaderItem(1).text() == "选择的场景"
    assert page.table.rowCount() == 1
    assert page.table.item(0, 1).text() == "页面流畅度"
    assert "当前显示 1 条快速启动任务" in page.validation_banner.messages[0]

    page.source_combo.setCurrentIndex(page.source_combo.findData(""))

    assert page.table.rowCount() == 4
    assert {page.table.item(row, 1).text() for row in range(page.table.rowCount())} == {
        "页面流畅度",
        "startup_home",
        "broken",
        "baseline-vs-candidate",
    }


def test_history_page_load_once_scans_without_reloading_on_visibility_changes(
    qtbot,
    tmp_path: Path,
) -> None:
    _seed_cached_experiment(tmp_path)
    settings = _settings(tmp_path)
    workspace = WorkspaceService(settings, default_workspace=tmp_path)
    page = HistoryPage(
        workspace_service=workspace,
        history_service=HistoryService(tmp_path),
    )
    qtbot.addWidget(page)
    scan_starts: list[bool] = []
    page.scan_started.connect(lambda: scan_starts.append(True))

    with qtbot.waitSignal(page.scan_finished, timeout=5000):
        page.load_once()

    assert page.table.rowCount() == 1
    assert page.table.item(0, 1).text() == "页面流畅度"
    assert len(scan_starts) == 1

    page.hide()
    page.show()
    qtbot.wait(100)
    assert not page.is_scanning
    assert len(scan_starts) == 1


def test_history_service_keeps_standalone_report_as_unknown_task_with_report(
    tmp_path: Path,
) -> None:
    standalone = tmp_path / "reports" / "imported-report"
    standalone.mkdir(parents=True)
    report = standalone / "report.html"
    report.write_text("standalone report", encoding="utf-8")

    result = HistoryService(tmp_path).scan()

    assert len(result.entries) == 1
    entry = result.entries[0]
    assert entry.task_kind is HistoryTaskKind.UNKNOWN
    assert entry.has_report
    assert entry.report_path == report.resolve()


def test_history_service_skips_external_directory_and_file_symlinks(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    (workspace / "runs").mkdir(parents=True)
    external.mkdir()
    (external / "summary.json").write_text('{"status":"pass"}', encoding="utf-8")
    try:
        (workspace / "runs" / "outside-dir").symlink_to(external, target_is_directory=True)
        linked_file = workspace / "runs" / "linked-file"
        linked_file.mkdir()
        (linked_file / "summary.json").symlink_to(external / "summary.json")
    except OSError as exc:
        pytest.skip(f"symlink unsupported: {exc}")

    result = HistoryService(workspace).scan()

    assert result.entries == ()


def test_history_service_scan_is_bounded(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    for index in range(8):
        directory = runs / f"run-{index}"
        directory.mkdir(parents=True)
        (directory / "summary.json").write_text(
            json.dumps({"runId": f"run-{index}", "status": "completed"}),
            encoding="utf-8",
        )

    result = HistoryService(tmp_path, max_nodes=20, max_entries=3).scan()

    assert len(result.entries) == 3
    assert result.truncated
    assert "安全上限" in result.warnings[-1]


def test_history_page_filters_views_summary_and_forwards_safe_open_requests(
    qtbot,
    tmp_path: Path,
) -> None:
    _seed_history(tmp_path)
    settings = _settings(tmp_path)
    workspace = WorkspaceService(settings, default_workspace=tmp_path)
    opener = RecordingOpener()
    page = HistoryPage(
        workspace_service=workspace,
        history_service=HistoryService(tmp_path),
        opener=opener,
    )
    qtbot.addWidget(page)
    page.source_combo.setCurrentIndex(page.source_combo.findData(""))

    with qtbot.waitSignal(page.scan_finished, timeout=5000) as blocker:
        page.refresh_button.click()
        assert page.is_scanning
        assert not page.refresh_button.isEnabled()
        assert page.loading_overlay.is_active
        assert page.loading_overlay.isVisibleTo(page.table)
    result = blocker.args[0]
    assert not page.is_scanning
    assert page.refresh_button.isEnabled()
    assert not page.loading_overlay.is_active

    assert len(result.entries) == 3
    assert page.table.rowCount() == 3
    assert page.status_combo.accessibleName() == "历史任务状态筛选"
    assert page.task_kind_combo.accessibleName() == "历史任务类型筛选"
    assert page.source_combo.accessibleName() == "历史任务来源筛选"
    assert page.artifact_combo.accessibleName() == "历史报告可用性筛选"
    assert page.health_combo.accessibleName() == "历史结果完整性筛选"
    assert page.delete_entry_button.objectName() == "dangerButton"
    assert page.clear_history_button.objectName() == "destructiveTextButton"
    assert (
        page.filters_layout.itemAtPosition(2, 3).widget()
        is page.delete_entry_button
    )
    assert page.scroll.objectName() == "historyPageScroll"
    assert page.scroll.widgetResizable()
    assert page.table.minimumHeight() == page.table.maximumHeight()
    assert page.table.height() == page.table.minimumHeight()
    assert page.table.selectionMode() == QAbstractItemView.SelectionMode.ExtendedSelection
    assert page.list_actions_layout.indexOf(page.clear_history_button) >= 0
    assert page.toast.objectName() == "pageToast"
    assert page.content_layout.indexOf(page.toast) == -1
    assert page.detail_section.isHidden()
    target_row = next(
        row
        for row in range(page.table.rowCount())
        if page.table.item(row, 1).text() == "startup_home"
    )
    page.table.selectRow(target_row)
    assert not page.detail_section.isHidden()
    assert page.report_section is page.detail_section
    assert "startup_home" in page.report_title_label.text()
    assert "解读未生成" in page.report_meta_label.text()
    assert page.report_availability_label.text() == "原始报告可用"
    assert page.open_report_button.text() == "查看原始报告"
    assert page.open_report_button.isEnabled()

    page.resize(980, 720)
    page.move(120, 90)
    page.show()
    qtbot.waitExposed(page)
    qtbot.waitUntil(lambda: page.scroll.verticalScrollBar().maximum() > 0)
    assert page.scroll.horizontalScrollBar().maximum() == 0
    page.open_report_button.click()
    assert opener.static[0].label == "报告"
    assert opener.regular == []

    page.artifact_combo.setCurrentIndex(page.artifact_combo.findData("report"))
    assert page.table.rowCount() == 2
    assert {
        page.table.item(row, 1).text() for row in range(page.table.rowCount())
    } == {"startup_home", "baseline-vs-candidate"}

    page.task_kind_combo.setCurrentIndex(page.task_kind_combo.findData("run"))
    assert page.table.rowCount() == 1
    assert page.table.item(0, 1).text() == "startup_home"

    page.task_kind_combo.setCurrentIndex(0)
    page.artifact_combo.setCurrentIndex(page.artifact_combo.findData("no_report"))
    assert page.table.rowCount() == 1
    assert page.table.item(0, 1).text() == "broken"

    page.artifact_combo.setCurrentIndex(0)
    page.health_combo.setCurrentIndex(page.health_combo.findData("partial"))
    assert page.table.rowCount() == 1
    assert page.table.item(0, 1).text() == "broken"

    new_workspace = tmp_path / "new workspace"
    new_workspace.mkdir()
    workspace.set_workspace(new_workspace)
    assert page.entries == ()
    assert "workspace 已变化" in page.validation_banner.messages[0]


def test_history_page_double_click_opens_existing_report_only(
    qtbot,
    tmp_path: Path,
) -> None:
    _seed_history(tmp_path)
    settings = _settings(tmp_path)
    workspace = WorkspaceService(settings, default_workspace=tmp_path)
    opener = RecordingOpener()
    page = HistoryPage(
        workspace_service=workspace,
        history_service=HistoryService(tmp_path),
        opener=opener,
    )
    qtbot.addWidget(page)
    page.source_combo.setCurrentIndex(page.source_combo.findData(""))

    with qtbot.waitSignal(page.scan_finished, timeout=5000):
        page.refresh_button.click()

    report_row = next(
        row
        for row in range(page.table.rowCount())
        if page.table.item(row, 1).text() == "startup_home"
    )
    missing_row = next(
        row
        for row in range(page.table.rowCount())
        if page.table.item(row, 1).text() == "broken"
    )

    page.table.itemDoubleClicked.emit(page.table.item(report_row, 1))
    assert len(opener.static) == 1
    assert opener.static[0].label == "报告"
    page.table.itemDoubleClicked.emit(page.table.item(missing_row, 1))
    assert len(opener.static) == 1


def test_history_page_deletes_selected_result_after_confirmation(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    experiment = _seed_cached_experiment(tmp_path)
    settings = _settings(tmp_path)
    workspace = WorkspaceService(settings, default_workspace=tmp_path)
    page = HistoryPage(
        workspace_service=workspace,
        history_service=HistoryService(tmp_path),
    )
    qtbot.addWidget(page)
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )

    with qtbot.waitSignal(page.scan_finished, timeout=5000):
        page.refresh_button.click()
    page.table.selectRow(0)

    assert "页面流畅度" in page.report_title_label.text()
    assert page.delete_entry_button.isEnabled()

    with qtbot.waitSignal(page.scan_finished, timeout=5000):
        page.delete_entry_button.click()

    assert not experiment.exists()
    assert page.table.rowCount() == 0
    assert "已删除“页面流畅度”" in page.validation_banner.messages[0]


def test_history_page_multi_select_deletes_all_selected_result_directories(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    first = _seed_cached_experiment(tmp_path)
    second = _seed_cached_experiment(
        tmp_path,
        experiment_name="20260812T090000.000000Z-startup-def456",
        title="启动速度",
        preset_id="builtin.startup",
        scenario_id="quick_startup",
    )
    for experiment in (first, second):
        logs = experiment / "logs"
        logs.mkdir()
        (logs / "task.stderr.log").write_text("test log", encoding="utf-8")
    settings = _settings(tmp_path)
    workspace = WorkspaceService(settings, default_workspace=tmp_path)
    page = HistoryPage(
        workspace_service=workspace,
        history_service=HistoryService(tmp_path),
    )
    qtbot.addWidget(page)
    prompts: list[str] = []

    def confirm(_parent, _title, message, *_args, **_kwargs):
        prompts.append(message)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "warning", confirm)

    with qtbot.waitSignal(page.scan_finished, timeout=5000):
        page.refresh_button.click()

    selection_model = page.table.selectionModel()
    assert selection_model is not None
    flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
    for row in range(page.table.rowCount()):
        selection_model.select(page.table.model().index(row, 0), flags)
    page.table.setCurrentCell(
        0,
        0,
        QItemSelectionModel.SelectionFlag.NoUpdate,
    )

    assert len(page._selected_entries()) == 2
    assert page.delete_entry_button.text() == "删除所选结果（2）"
    assert "已选择 2 条" in page.list_status_label.text()
    assert page.delete_entry_button.isEnabled()

    with qtbot.waitSignal(page.scan_finished, timeout=5000):
        page.delete_entry_button.click()

    assert "所选 2 条" in prompts[0]
    assert "报告、日志" in prompts[0]
    assert not first.exists()
    assert not second.exists()
    assert page.table.rowCount() == 0
    assert "已删除所选 2 条历史结果" in page.validation_banner.messages[0]


def test_history_page_clears_current_source_and_preserves_legacy_results(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    _seed_history(tmp_path)
    first = _seed_cached_experiment(tmp_path)
    second = _seed_cached_experiment(
        tmp_path,
        experiment_name="20260812T090000.000000Z-startup-def456",
        title="启动速度",
        preset_id="builtin.startup",
        scenario_id="quick_startup",
    )
    settings = _settings(tmp_path)
    workspace = WorkspaceService(settings, default_workspace=tmp_path)
    page = HistoryPage(
        workspace_service=workspace,
        history_service=HistoryService(tmp_path),
    )
    qtbot.addWidget(page)
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )

    with qtbot.waitSignal(page.scan_finished, timeout=5000):
        page.refresh_button.click()

    assert page.table.rowCount() == 2
    assert page.clear_history_button.text() == "清空快速启动历史"

    with qtbot.waitSignal(page.scan_finished, timeout=5000):
        page.clear_history_button.click()

    assert not first.exists()
    assert not second.exists()
    assert page.table.rowCount() == 0
    page.source_combo.setCurrentIndex(page.source_combo.findData(""))
    assert page.table.rowCount() == 3
    assert "已清空 2 条快速启动任务" in page.validation_banner.messages[0]


def test_history_page_queries_related_scenario_and_can_return_to_all_results(
    qtbot,
    tmp_path: Path,
) -> None:
    _seed_history(tmp_path)
    _seed_cached_experiment(tmp_path)
    settings = _settings(tmp_path)
    workspace = WorkspaceService(settings, default_workspace=tmp_path)
    page = HistoryPage(
        workspace_service=workspace,
        history_service=HistoryService(tmp_path),
    )
    qtbot.addWidget(page)

    query = HistoryQuery(
        label="页面流畅度",
        preset_id="builtin.smoothness",
        scenario_id="quick_smoothness",
        task_kind=HistoryTaskKind.RUN,
    )
    with qtbot.waitSignal(page.scan_finished, timeout=5000):
        page.show_related_results(query)

    assert page.related_query == query
    assert page.related_filter_bar.isVisibleTo(page)
    assert page.related_filter_label.text() == "当前查询：页面流畅度"
    assert page.table.rowCount() == 1
    assert page.table.item(0, 1).text() == "页面流畅度"
    assert "找到 1 条" in page.validation_banner.messages[0]

    page.clear_related_button.click()

    assert page.related_query is None
    assert not page.related_filter_bar.isVisibleTo(page)
    assert page.table.rowCount() == 1


def test_home_page_accepts_lightweight_environment_and_recent_history_summaries(
    qtbot,
    tmp_path: Path,
) -> None:
    _seed_history(tmp_path)
    entries = HistoryService(tmp_path).scan().entries
    page = HomePage(str(tmp_path))
    qtbot.addWidget(page)
    snapshot = EnvironmentSnapshot(
        tools=(
            ToolStatus("cli", "PerfEval CLI", True),
            ToolStatus("adb", "adb", True),
        ),
        devices=(DeviceInfo("device-1", "device"), DeviceInfo("offline-1", "offline")),
        android_status="ready",
    )

    page.set_environment_snapshot(snapshot)
    page.set_recent_entries(entries)

    assert "在线设备：1" in page.environment_summary.text()
    assert "｜" in page.recent_label.text()
