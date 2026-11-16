from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings

from perfeval.gui.models.result import ResultType, TaskOutcome
from perfeval.gui.models.task import ManualCapability, TaskSpec, TaskType
from perfeval.gui.pages.ab_page import AbPage
from perfeval.gui.pages.diagnose_page import DiagnosePage
from perfeval.gui.pages.environment_page import EnvironmentPage
from perfeval.gui.pages.run_page import RunPage
from perfeval.gui.services.command_builder import CommandBuilder
from perfeval.gui.services.settings import SettingsService
from perfeval.gui.services.workspace import WorkspaceService


FAKE_CLI = Path(__file__).with_name("fake_cli.py")


class FakeWorkflowBuilder(CommandBuilder):
    """Keep page validation/UI real while replacing only the process command."""

    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace, cli_program=sys.executable)

    def build_run_device(self, **values: object) -> TaskSpec:
        return self._fake_task(TaskType.RUN, "stream", values["output"])

    def build_run_offline(self, **values: object) -> TaskSpec:
        return self._fake_task(TaskType.OFFLINE, "exit", values["output"])

    def build_ab(self, **values: object) -> TaskSpec:
        return self._fake_task(TaskType.AB, "stream", values["output"])

    def build_diagnose(self, **values: object) -> TaskSpec:
        return self._fake_task(
            TaskType.DIAGNOSE,
            "manual",
            values["output"],
            manual=True,
        )

    def _fake_task(
        self,
        task_type: TaskType,
        scenario: str,
        output: object,
        *,
        manual: bool = False,
    ) -> TaskSpec:
        root = Path(str(output)).expanduser()
        if not root.is_absolute():
            root = self.workspace / root
        root = root.resolve(strict=False)
        arguments = [
            str(FAKE_CLI),
            scenario,
            "--output-root",
            str(root),
            "--delay",
            "0",
        ]
        return TaskSpec.create(
            task_type=task_type,
            program=sys.executable,
            arguments=arguments,
            working_directory=self.workspace,
            display_command=" ".join([sys.executable, *arguments]),
            expected_output_root=root,
            manual_capability=(
                ManualCapability.EVENT_AND_TEXT if manual else ManualCapability.NONE
            ),
        )


def _settings(tmp_path: Path, name: str) -> SettingsService:
    backend = QSettings(str(tmp_path / f"{name}.ini"), QSettings.Format.IniFormat)
    backend.setFallbacksEnabled(False)
    return SettingsService(backend)


def _input_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    scenario = tmp_path / "场景 文件.yaml"
    scenario.write_text("scenarioId: validation\n", encoding="utf-8")
    baseline = tmp_path / "baseline app.apk"
    candidate = tmp_path / "候选 app.apk"
    baseline.write_bytes(b"apk")
    candidate.write_bytes(b"apk")
    return scenario, baseline, candidate


@pytest.mark.parametrize("mode", ["device", "offline"])
def test_validation_run_page_fake_process_end_to_end(qtbot, tmp_path: Path, mode: str) -> None:
    settings = _settings(tmp_path, f"run-{mode}")
    workspace = WorkspaceService(settings, default_workspace=tmp_path)
    builder = FakeWorkflowBuilder(tmp_path)
    scenario, apk, _candidate = _input_files(tmp_path)
    page = RunPage(
        settings=settings,
        workspace_service=workspace,
        builder_factory=lambda _root: builder,
    )
    qtbot.addWidget(page)
    page.show()

    if mode == "device":
        page.mode_tabs.setCurrentIndex(0)
        page.device_form.scenario_picker.set_path(str(scenario))
        page.device_form.apk_picker.set_path(str(apk))
        page.device_form.package_edit.setText("com.example.app")
        page.device_form.output_edit.setText("runs/设备 输出")
    else:
        artifacts = tmp_path / "artifact 目录"
        artifacts.mkdir()
        page.mode_tabs.setCurrentIndex(1)
        page.offline_form.scenario_picker.set_path(str(scenario))
        page.offline_form.artifacts_picker.set_path(str(artifacts))
        page.offline_form.package_edit.setText("com.example.app")
        page.offline_form.output_edit.setText("runs/离线 输出")

    with qtbot.waitSignal(page.controller.finished, timeout=5000) as blocker:
        page.start_button.click()

    result = blocker.args[0]
    assert result.outcome == TaskOutcome.COMPLETED
    assert any(item.type == ResultType.REPORT and item.exists for item in result.results)
    assert page.task_summary.result is result
    if mode == "device":
        assert "first chunk" in page.task_console.output.toPlainText()


def test_validation_ab_page_fake_process_end_to_end(qtbot, tmp_path: Path) -> None:
    scenario, baseline, candidate = _input_files(tmp_path)
    page = AbPage(
        workspace=tmp_path,
        command_builder=FakeWorkflowBuilder(tmp_path),
        open_result=lambda _descriptor: True,
    )
    qtbot.addWidget(page)
    page.show()
    page.scenario_picker.set_path(str(scenario))
    page.output_picker.set_path("runs/AB 空格输出")
    page.baseline_apk_picker.set_path(str(baseline))
    page.candidate_editor.rows[0].id_edit.setText("候选-v2")
    page.candidate_editor.rows[0].apk_picker.set_path(str(candidate))

    with qtbot.waitSignal(page.controller.finished, timeout=5000) as blocker:
        page.start_button.click()

    result = blocker.args[0]
    assert result.task_type == TaskType.AB
    assert result.outcome == TaskOutcome.COMPLETED
    assert page.summary.result is result
    assert any(item.type == ResultType.REPORT and item.exists for item in result.results)
    assert "first chunk" in page.console.output.toPlainText()


def test_validation_diagnose_manual_fake_process_end_to_end(qtbot, tmp_path: Path) -> None:
    page = DiagnosePage(
        workspace=tmp_path,
        command_builder=FakeWorkflowBuilder(tmp_path),
    )
    qtbot.addWidget(page)
    page.show()
    page.package_edit.setText("com.example.app")
    page.output_picker.set_path("runs/诊断 空格输出")
    page.interactive_check.setChecked(True)

    with qtbot.waitSignal(page.controller.manual_requested, timeout=5000) as manual_blocker:
        page.start_button.click()

    assert manual_blocker.args[0].source == "event"
    assert page.task_console.manual_frame.isVisible()
    assert "请完成手动步骤" in page.task_console.manual_prompt_label.text()

    with qtbot.waitSignal(page.controller.finished, timeout=5000) as finished_blocker:
        page.task_console.manual_continue_button.click()

    result = finished_blocker.args[0]
    assert result.task_type == TaskType.DIAGNOSE
    assert result.outcome == TaskOutcome.COMPLETED
    assert page.task_summary.result is result


def test_validation_environment_page_real_qprocess_fake_tools(qtbot, tmp_path: Path) -> None:
    wrapper = tmp_path / "fake perfeval adb"
    wrapper.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import runpy, sys",
                f"sys.argv[0] = {str(FAKE_CLI)!r}",
                f"runpy.run_path({str(FAKE_CLI)!r}, run_name='__main__')",
                "",
            ]
        ),
        encoding="utf-8",
    )
    wrapper.chmod(wrapper.stat().st_mode | 0o111)
    settings = _settings(tmp_path, "environment-e2e")
    workspace = WorkspaceService(settings, default_workspace=tmp_path)
    page = EnvironmentPage(settings=settings, workspace_service=workspace)
    qtbot.addWidget(page)
    page.show()
    page.cli_picker.set_path(str(wrapper))
    page.adb_picker.set_path(str(wrapper))

    with qtbot.waitSignal(page.device_service.snapshot_ready, timeout=5000) as blocker:
        page.refresh_button.click()

    snapshot = blocker.args[0]
    assert snapshot.cli_version == "perfeval 0.1.0-fake"
    assert snapshot.android_status == "ready"
    assert [(device.serial, device.state) for device in snapshot.devices] == [
        ("fake-online-1", "device")
    ]
    assert page.devices_table.item(0, 2).text() == "在线"
    assert settings.external_tool_path("cli") == str(wrapper)
    assert settings.external_tool_path("adb") == str(wrapper)
    assert os.access(wrapper, os.X_OK)
