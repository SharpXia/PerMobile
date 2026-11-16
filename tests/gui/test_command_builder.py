from __future__ import annotations

from pathlib import Path

import pytest

from perfeval.gui.models.task import ManualCapability, TaskType
from perfeval.gui.services.command_builder import (
    CandidateInput,
    CommandBuilder,
    CommandValidationError,
    format_display_command,
)


def _workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace = tmp_path / "工作 空间"
    workspace.mkdir()
    scenario = workspace / "场景 文件.yaml"
    scenario.write_text("scenarioId: demo\n", encoding="utf-8")
    apk = workspace / "应用 包.apk"
    apk.write_bytes(b"apk")
    return workspace, scenario, apk


def test_command_builder_uses_program_and_arguments_with_unicode_paths(tmp_path: Path) -> None:
    workspace, scenario, apk = _workspace(tmp_path)
    builder = CommandBuilder(workspace, cli_program="perfeval-cli")

    task = builder.build_run_device(
        scenario=scenario,
        output="运行 结果",
        package_name="com.example.app",
        apk=apk,
        clear_data=False,
        reinstall=False,
        interactive=True,
    )

    assert task.task_type == TaskType.RUN
    assert task.program == "perfeval-cli"
    assert task.arguments[:2] == ("run", str(scenario))
    assert "--gui-events" in task.arguments
    assert "--no-clear-data" in task.arguments
    assert "--no-reinstall" in task.arguments
    assert task.manual_capability == ManualCapability.EVENT_AND_TEXT
    assert task.expected_output_root == workspace / "运行 结果"
    assert "场景 文件.yaml" in task.display_command
    assert all(value not in {"sh", "-c", "cmd", "/c"} for value in task.arguments)


def test_command_builder_rejects_external_output_without_authorization(tmp_path: Path) -> None:
    workspace, scenario, _apk = _workspace(tmp_path)
    builder = CommandBuilder(workspace, cli_program="perfeval")

    with pytest.raises(CommandValidationError, match="已授权"):
        builder.build_run_offline(
            scenario=scenario,
            android_artifacts=workspace,
            output=tmp_path / "outside",
        )


def test_command_builder_emits_sampling_and_screen_record_switches(tmp_path: Path) -> None:
    """TASK-025 契约：高级开关逐项映射为 run 子命令的 CLI 产物。"""
    workspace, scenario, _apk = _workspace(tmp_path)
    builder = CommandBuilder(workspace, cli_program="perfeval")

    task = builder.build_run_device(
        scenario=scenario,
        sampling_off=True,
        sampling_interval_ms=250,
        screen_record=True,
    )

    arguments = task.arguments
    assert "--sampling-off" in arguments
    assert arguments[arguments.index("--sampling-interval-ms") + 1] == "250"
    assert "--screen-record" in arguments
    # 开关不影响其余产物结构
    assert "--gui-events" in arguments


def test_command_builder_default_advanced_switches_keep_command_unchanged(tmp_path: Path) -> None:
    """回归：不动高级开关时，产物与既有命令完全一致（不出现新增旗标）。"""
    workspace, scenario, _apk = _workspace(tmp_path)
    builder = CommandBuilder(workspace, cli_program="perfeval")

    default_task = builder.build_run_device(scenario=scenario)
    legacy_call = builder.build_run_device(
        scenario=scenario,
        repeat=None,
        package_name=None,
        serial=None,
        apk=None,
        activity=None,
        clear_data=None,
        reinstall=None,
        perfetto=False,
        post_launch_wait=None,
        interactive=False,
    )

    assert default_task.arguments == legacy_call.arguments
    for flag in ("--sampling-off", "--sampling-interval-ms", "--screen-record"):
        assert flag not in default_task.arguments


@pytest.mark.parametrize("invalid", [0, -100, True, 3_600_001])
def test_command_builder_rejects_invalid_sampling_interval(tmp_path: Path, invalid) -> None:
    workspace, scenario, _apk = _workspace(tmp_path)
    builder = CommandBuilder(workspace, cli_program="perfeval")

    with pytest.raises(CommandValidationError, match="sampling_interval_ms"):
        builder.build_run_device(scenario=scenario, sampling_interval_ms=invalid)


def test_command_builder_ab_preserves_candidate_order_and_rejects_slug_collisions(tmp_path: Path) -> None:
    workspace, scenario, apk = _workspace(tmp_path)
    second = workspace / "candidate 2.apk"
    second.write_bytes(b"apk")
    builder = CommandBuilder(workspace, cli_program="perfeval")

    task = builder.build_ab(
        scenario=scenario,
        baseline_apk=apk,
        candidates=(CandidateInput("v2", apk), CandidateInput("v3", second)),
    )
    candidate_values = [
        task.arguments[index + 1]
        for index, value in enumerate(task.arguments)
        if value == "--candidate-apk"
    ]
    assert candidate_values == [f"v2={apk}", f"v3={second}"]

    with pytest.raises(CommandValidationError, match="清洗后的输出名冲突"):
        builder.build_ab(
            scenario=scenario,
            baseline_apk=apk,
            candidates=(CandidateInput("版本 一", apk), CandidateInput("版本 二", second)),
        )


@pytest.mark.parametrize("reserved", ["CON", "con.txt", "LPT1"])
def test_command_builder_rejects_windows_reserved_case_ids(tmp_path: Path, reserved: str) -> None:
    workspace, _scenario, _apk = _workspace(tmp_path)
    builder = CommandBuilder(workspace, cli_program="perfeval")

    with pytest.raises(CommandValidationError, match="Windows 保留"):
        builder.build_diagnose(package_name="com.example", case_id=reserved)


def test_display_command_is_copy_only_and_platform_specific() -> None:
    arguments = ["run", "C:/工作 空间/scenario.yaml", "--package-name", "a&b"]
    posix = format_display_command("perfeval", arguments, platform="darwin")
    windows = format_display_command("perfeval.exe", arguments, platform="win32")

    assert "'C:/工作 空间/scenario.yaml'" in posix
    assert '"C:/工作 空间/scenario.yaml"' in windows
    assert arguments[1] == "C:/工作 空间/scenario.yaml"

