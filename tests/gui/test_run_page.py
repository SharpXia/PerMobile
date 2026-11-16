from __future__ import annotations

import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtWidgets import QPushButton

from perfeval.gui.models.device import DeviceInfo, EnvironmentSnapshot
from perfeval.gui.models.result import ResultDescriptor, ResultType, ResultViewer, TaskOutcome, TaskResult
from perfeval.gui.models.task import TaskState, TaskType
from perfeval.gui.pages.run_page import RunPage
from perfeval.gui.services.command_builder import CommandBuilder
from perfeval.gui.services.settings import SettingsService
from perfeval.gui.services.workspace import WorkspaceService
from perfeval.gui.widgets.validation_banner import ValidationSeverity


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class FakeDeviceService(QObject):
    refresh_started = Signal()
    snapshot_ready = Signal(object)
    refresh_failed = Signal(str)
    refresh_finished = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.refresh_count = 0

    def refresh(self) -> bool:
        self.refresh_count += 1
        self.refresh_started.emit()
        return True


class RecordingController(QObject):
    state_changed = Signal(object)
    validation_failed = Signal(object)
    started = Signal(object)
    log_chunk = Signal(object)
    manual_requested = Signal(object)
    elapsed_changed = Signal(float)
    launch_failed = Signal(object)
    finished = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.state = TaskState.IDLE
        self.started_tasks = []
        self.cancel_count = 0
        self.continue_count = 0

    @property
    def is_active(self) -> bool:
        return self.state in {
            TaskState.STARTING,
            TaskState.RUNNING,
            TaskState.WAITING_FOR_USER,
            TaskState.CANCELLING,
        }

    def start(self, task) -> bool:
        self.started_tasks.append(task)
        self.state = TaskState.RUNNING
        self.state_changed.emit(self.state)
        return True

    def cancel(self) -> bool:
        if not self.is_active:
            return False
        self.cancel_count += 1
        self.state = TaskState.CANCELLING
        self.state_changed.emit(self.state)
        return True

    def continue_manual(self) -> bool:
        self.continue_count += 1
        return self.state == TaskState.WAITING_FOR_USER

    def reset(self) -> None:
        self.state = TaskState.IDLE
        self.state_changed.emit(self.state)

    def publish_result(self, result: TaskResult) -> None:
        self.state = TaskState.COMPLETED
        self.state_changed.emit(self.state)
        self.finished.emit(result)


def _settings(tmp_path: Path) -> SettingsService:
    backend = QSettings(str(tmp_path / "run-page.ini"), QSettings.Format.IniFormat)
    backend.setFallbacksEnabled(False)
    return SettingsService(backend)


def _page(qtbot, tmp_path: Path) -> tuple[RunPage, RecordingController, FakeDeviceService]:
    settings = _settings(tmp_path)
    workspace = WorkspaceService(settings, default_workspace=tmp_path)
    controller = RecordingController()
    devices = FakeDeviceService()
    page = RunPage(
        settings=settings,
        workspace_service=workspace,
        device_service=devices,
        controller=controller,
        builder_factory=lambda root: CommandBuilder(
            root,
            cli_program="perfeval",
            display_platform="posix",
        ),
    )
    qtbot.addWidget(page)
    return page, controller, devices


def test_device_run_form_builds_complete_command_and_starts_controller(qtbot, tmp_path: Path) -> None:
    page, controller, devices = _page(qtbot, tmp_path)
    scenario = tmp_path / "场景.yaml"
    apk = tmp_path / "build with spaces.apk"
    scenario.write_text("scenario", encoding="utf-8")
    apk.write_text("apk", encoding="utf-8")
    form = page.device_form
    form.scenario_picker.set_path(str(scenario))
    form.output_edit.setText("runs/device output")
    form.repeat_spin.setValue(3)
    form.serial_combo.setEditText("emulator-5554")
    form.apk_picker.set_path(str(apk))
    form.package_edit.setText("com.example.app")
    form.activity_edit.setText(".MainActivity")
    form.advanced_group.setChecked(True)
    form.clear_data_check.setChecked(False)
    form.reinstall_check.setChecked(False)
    form.perfetto_check.setChecked(True)
    form.post_launch_wait_spin.setValue(3.5)
    form.interactive_check.setChecked(True)

    task = page.preview_command()

    assert task is not None
    assert task.task_type == TaskType.RUN
    assert "--android-device-run" in task.arguments
    assert task.arguments[task.arguments.index("--serial") + 1] == "emulator-5554"
    assert task.arguments[task.arguments.index("--apk") + 1] == str(apk)
    assert "--no-clear-data" in task.arguments
    assert "--no-reinstall" in task.arguments
    assert "--perfetto" in task.arguments
    assert "--interactive" in task.arguments
    assert "--gui-events" in task.arguments
    assert page.preview_edit.toPlainText() == task.display_command
    assert page.copy_preview_button.isEnabled()

    page.device_form.refresh_devices_button.click()
    page.start_button.click()

    assert controller.started_tasks[-1].arguments == task.arguments
    assert page.cancel_button.isEnabled()
    assert not page.start_button.isEnabled()
    assert devices.refresh_count == 1

    page.cancel_button.click()
    assert controller.cancel_count == 1


def test_offline_form_hides_device_only_inputs_and_builds_artifact_command(qtbot, tmp_path: Path) -> None:
    page, _controller, _devices = _page(qtbot, tmp_path)
    scenario = tmp_path / "offline.yaml"
    artifacts = tmp_path / "artifacts"
    scenario.write_text("scenario", encoding="utf-8")
    artifacts.mkdir()
    page.mode_tabs.setCurrentIndex(1)
    form = page.offline_form
    form.scenario_picker.set_path(str(scenario))
    form.output_edit.setText("runs/offline")
    form.repeat_spin.setValue(2)
    form.artifacts_picker.set_path(str(artifacts))
    form.package_edit.setText("com.example.offline")

    task = page.preview_command()

    assert task is not None
    assert task.task_type == TaskType.OFFLINE
    assert task.arguments[task.arguments.index("--android-artifacts") + 1] == str(artifacts)
    assert "--android-device-run" not in task.arguments
    assert "--serial" not in task.arguments
    assert "--apk" not in task.arguments
    assert "--interactive" not in task.arguments


def test_invalid_visible_input_blocks_start_and_preserves_form_values(qtbot, tmp_path: Path) -> None:
    page, controller, _devices = _page(qtbot, tmp_path)
    missing = tmp_path / "missing scenario.yaml"
    page.device_form.scenario_picker.set_path(str(missing))
    page.device_form.output_edit.setText("runs")

    page.start_button.click()

    assert controller.started_tasks == []
    assert page.device_form.scenario_picker.path == str(missing)
    assert page.validation_banner.severity == ValidationSeverity.ERROR
    assert any("scenario" in message and "不存在" in message for message in page.validation_banner.messages)


def test_device_snapshot_populates_only_online_serials(qtbot, tmp_path: Path) -> None:
    page, _controller, devices = _page(qtbot, tmp_path)
    snapshot = EnvironmentSnapshot(
        tools=(),
        devices=(
            DeviceInfo("online-1", "device", model="Pixel"),
            DeviceInfo("offline-1", "offline", model="Old Phone"),
            DeviceInfo("locked-1", "unauthorized"),
        ),
        android_status="ready",
    )

    devices.snapshot_ready.emit(snapshot)

    assert page.device_form.serial_combo.count() == 1
    assert page.device_form.serial() == "online-1"


def test_result_entry_is_forwarded_without_page_opening_paths_directly(qtbot, tmp_path: Path) -> None:
    page, controller, _devices = _page(qtbot, tmp_path)
    report = tmp_path / "report.html"
    report.write_text("report", encoding="utf-8")
    descriptor = ResultDescriptor(
        type=ResultType.REPORT,
        label="报告",
        location=str(report),
        exists=True,
        viewer=ResultViewer.REPORT,
        related_task_id="run-1",
    )
    result = TaskResult(
        task_id="run-1",
        task_type=TaskType.OFFLINE,
        outcome=TaskOutcome.COMPLETED,
        exit_code=0,
        results=(descriptor,),
    )
    controller.publish_result(result)
    open_button = next(
        button
        for button in page.task_summary.findChildren(QPushButton)
        if button.text() == "打开"
    )

    with qtbot.waitSignal(page.open_result_requested) as blocker:
        open_button.click()

    assert blocker.args[0] == descriptor


def test_offline_page_executes_real_cli_and_locates_report(qtbot, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    workspace = WorkspaceService(settings, default_workspace=tmp_path)
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(
        """schemaVersion: perfeval.scenario.v1
project: PerMobile
scenarioId: gui_offline
apps:
  - id: target
    platform: android
    packageName: com.example.app
run:
  repeats: 1
  mode: same_device_versions
""",
        encoding="utf-8",
    )
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    golden = PROJECT_ROOT / "perfeval" / "fixtures" / "android" / "golden"
    for name in ("gfxinfo.txt", "meminfo.txt", "logcat-startup.txt"):
        shutil.copy2(golden / name, artifacts / ("logcat.txt" if name == "logcat-startup.txt" else name))
    page = RunPage(
        settings=settings,
        workspace_service=workspace,
        builder_factory=lambda root: CommandBuilder(
            root,
            cli_program=sys.executable,
            cli_prefix_arguments=("-m", "perfeval.cli.main"),
            environment={"PYTHONPATH": str(PROJECT_ROOT)},
        ),
    )
    qtbot.addWidget(page)
    page.mode_tabs.setCurrentIndex(1)
    page.offline_form.scenario_picker.set_path(str(scenario))
    page.offline_form.artifacts_picker.set_path(str(artifacts))
    page.offline_form.output_edit.setText("runs")
    page.offline_form.package_edit.setText("com.example.app")

    with qtbot.waitSignal(page.controller.finished, timeout=10000) as blocker:
        page.start_button.click()

    result = blocker.args[0]
    assert result.outcome == TaskOutcome.COMPLETED
    assert any(
        descriptor.type == ResultType.REPORT and descriptor.exists
        for descriptor in result.results
    )
    assert (tmp_path / "runs" / "gui_offline" / "report" / "report.html").is_file()


def test_device_advanced_switches_default_to_follow_scenario(qtbot, tmp_path: Path) -> None:
    """TASK-025 回归：默认不动高级开关时命令产物与既有行为一致。"""
    page, _controller, _devices = _page(qtbot, tmp_path)
    scenario = tmp_path / "场景.yaml"
    scenario.write_text("scenario", encoding="utf-8")
    form = page.device_form
    form.scenario_picker.set_path(str(scenario))
    form.output_edit.setText("runs")
    form.advanced_group.setChecked(True)

    task = page.preview_command()

    assert task is not None
    for flag in ("--sampling-off", "--sampling-interval-ms", "--screen-record"):
        assert flag not in task.arguments


def test_device_advanced_switches_emit_sampling_and_screen_record_flags(
    qtbot,
    tmp_path: Path,
) -> None:
    page, _controller, _devices = _page(qtbot, tmp_path)
    scenario = tmp_path / "场景.yaml"
    scenario.write_text("scenario", encoding="utf-8")
    form = page.device_form
    form.scenario_picker.set_path(str(scenario))
    form.output_edit.setText("runs")
    form.advanced_group.setChecked(True)

    form.sampling_off_check.setChecked(True)
    form.sampling_interval_spin.setValue(500)
    form.screen_record_check.setChecked(True)
    task = page.preview_command()

    assert task is not None
    assert "--sampling-off" in task.arguments
    assert task.arguments[task.arguments.index("--sampling-interval-ms") + 1] == "500"
    assert "--screen-record" in task.arguments


def test_device_sampling_interval_zero_means_follow_scenario(qtbot, tmp_path: Path) -> None:
    page, _controller, _devices = _page(qtbot, tmp_path)
    scenario = tmp_path / "场景.yaml"
    scenario.write_text("scenario", encoding="utf-8")
    form = page.device_form
    form.scenario_picker.set_path(str(scenario))
    form.output_edit.setText("runs")
    form.advanced_group.setChecked(True)
    form.sampling_interval_spin.setValue(0)

    task = page.preview_command()

    assert task is not None
    assert "--sampling-interval-ms" not in task.arguments
