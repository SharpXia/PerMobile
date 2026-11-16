from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from perfeval.gui.app import _smoke_exit_delay_ms
from perfeval.gui.services.packaging_paths import (
    PackagedRuntimeError,
    check_packaged_runtime,
    cli_version_matches,
    parse_cli_version,
    resolve_cli_launch,
    runtime_paths,
)


def test_process_packaging_paths_resolve_macos_bundle_sidecar_and_resources(tmp_path: Path) -> None:
    contents = tmp_path / "PerfEval Lite.app" / "Contents"
    executable = contents / "MacOS" / "PerfEval-Lite"
    sidecar = contents / "MacOS" / "perfeval-cli"
    resources = contents / "Resources"
    executable.parent.mkdir(parents=True)
    resources.mkdir(parents=True)
    executable.write_bytes(b"gui")
    sidecar.write_bytes(b"cli")
    (resources / "version.json").write_text('{"version":"0.1.0"}', encoding="utf-8")

    paths = runtime_paths(executable=executable, platform="darwin", frozen=True)

    assert paths.application_root == contents
    assert paths.resource_root == resources
    assert paths.sidecar == sidecar
    assert paths.version_metadata == resources / "version.json"
    assert resolve_cli_launch(paths=paths).program == str(sidecar)


def test_process_packaging_paths_resolve_windows_onedir_layout(tmp_path: Path) -> None:
    executable = tmp_path / "PerfEval-Lite.exe"
    executable.write_bytes(b"gui")
    resources = tmp_path / "resources"
    resources.mkdir()

    paths = runtime_paths(executable=executable, platform="win32", frozen=True)

    assert paths.application_root == tmp_path
    assert paths.resource_root == resources
    assert paths.sidecar == tmp_path / "perfeval-cli.exe"


def test_process_packaging_frozen_runtime_never_falls_back_to_python_module(tmp_path: Path) -> None:
    paths = runtime_paths(
        executable=tmp_path / "PerfEval-Lite",
        platform="linux",
        frozen=True,
    )

    with pytest.raises(PackagedRuntimeError, match="sidecar"):
        resolve_cli_launch(paths=paths)


def test_process_packaging_runtime_check_blocks_version_mismatch(tmp_path: Path) -> None:
    executable = tmp_path / "PerfEval-Lite"
    executable.write_bytes(b"gui")
    sidecar = tmp_path / "perfeval-cli"
    sidecar.write_text("#!/bin/sh\necho 'perfeval 9.9.9'\n", encoding="utf-8")
    sidecar.chmod(0o755)
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "version.json").write_text(
        json.dumps({"schemaVersion": "perfeval.version.v1", "version": "0.1.0"}),
        encoding="utf-8",
    )
    paths = runtime_paths(executable=executable, platform="linux", frozen=True)

    result = check_packaged_runtime(paths=paths, expected_version="0.1.0")

    assert not result.ok
    assert result.cli_version == "9.9.9"
    assert "版本不匹配" in result.message


def test_process_packaging_runtime_check_accepts_matching_sidecar(tmp_path: Path) -> None:
    executable = tmp_path / "PerfEval-Lite"
    executable.write_bytes(b"gui")
    sidecar = tmp_path / "perfeval-cli"
    sidecar.write_text("#!/bin/sh\necho 'perfeval 0.1.0'\n", encoding="utf-8")
    sidecar.chmod(0o755)
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "version.json").write_text(
        json.dumps({"schemaVersion": "perfeval.version.v1", "version": "0.1.0"}),
        encoding="utf-8",
    )
    paths = runtime_paths(executable=executable, platform="linux", frozen=True)

    result = check_packaged_runtime(paths=paths, expected_version="0.1.0")

    assert result.ok
    assert result.cli_version == "0.1.0"


def test_process_packaging_version_parser_is_strict() -> None:
    assert parse_cli_version("perfeval 0.1.0\n") == "0.1.0"
    assert cli_version_matches("perfeval 0.1.0", expected="0.1.0")
    assert parse_cli_version("version=0.1.0") is None
    assert not cli_version_matches("perfeval 0.2.0", expected="0.1.0")


def test_process_packaging_smoke_exit_environment_is_bounded(monkeypatch) -> None:
    monkeypatch.setenv("PERFEVAL_GUI_SMOKE_EXIT_MS", "750")
    assert _smoke_exit_delay_ms() == 750
    monkeypatch.setenv("PERFEVAL_GUI_SMOKE_EXIT_MS", "999999")
    assert _smoke_exit_delay_ms() == 30_000
    monkeypatch.setenv("PERFEVAL_GUI_SMOKE_EXIT_MS", "invalid")
    assert _smoke_exit_delay_ms() is None
