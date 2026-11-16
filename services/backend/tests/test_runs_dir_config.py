"""runs 目录默认落点解析（2026-11-06 体检 runs-dir-retention）。

缺陷背景：打包冻结环境下 ``_default_runs_dir`` 以 ``Path(__file__).parents[3]``
推导 repo_root/runs——``__file__`` 在冻结态解析进 PyInstaller 解包目录
（onefile=_MEIxxxx 退出即清理的临时目录），saved 会话写进随进程退出的路径而
静默丢失（实测现象：2026-11-04 会话 PD_20261104_145547 原始数据丢失）。
修复：PD_RUNS_DIR 显式覆盖恒胜出；冻结态改用用户级稳定数据目录；开发态口径
（仓库 runs/）不变。
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

from app import config
from app.config import Settings, _default_runs_dir, _frozen_runs_dir


# ---- 开发态默认：仓库 runs/（既有口径不变）----


def test_default_runs_dir_repo_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.delenv("PD_RUNS_DIR", raising=False)
    expected = Path(config.__file__).resolve().parents[3] / "runs"
    assert _default_runs_dir() == expected


# ---- PD_RUNS_DIR 显式覆盖（三态恒胜出）----


def test_env_override_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PD_RUNS_DIR", str(tmp_path / "custom-runs"))
    assert _default_runs_dir() == tmp_path / "custom-runs"


def test_env_override_wins_in_frozen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("PD_RUNS_DIR", str(tmp_path / "explicit-runs"))
    # 显式覆盖高于冻结态默认（不因冻结态平台分支被改写）
    assert _default_runs_dir() == tmp_path / "explicit-runs"


# ---- 冻结态：用户级稳定数据目录，绝不再落进解包/临时目录 ----


def test_frozen_darwin_user_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    expected = tmp_path / "Library" / "Application Support" / "PerfEval" / "runs"
    assert _frozen_runs_dir() == expected


def test_frozen_windows_appdata_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    expected = tmp_path / "AppData" / "Roaming" / "PerfEval" / "runs"
    assert _frozen_runs_dir() == expected


def test_frozen_windows_appdata_missing_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    expected = tmp_path / "AppData" / "Roaming" / "PerfEval" / "runs"
    assert _frozen_runs_dir() == expected


def test_frozen_linux_xdg_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    expected = tmp_path / "xdg-data" / "PerfEval" / "runs"
    assert _frozen_runs_dir() == expected


def test_frozen_linux_home_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    expected = tmp_path / ".local" / "share" / "PerfEval" / "runs"
    assert _frozen_runs_dir() == expected


@pytest.mark.parametrize("platform_name", ["darwin", "win32", "linux"])
def test_frozen_default_never_under_meipass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform_name: str
) -> None:
    """冻结态默认目录不得落在 ``sys._MEIPASS``（临时解包目录）之下。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", platform_name)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_MEI12345"), raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("PD_RUNS_DIR", raising=False)
    resolved = _default_runs_dir().resolve()
    meipass = (tmp_path / "_MEI12345").resolve()
    assert meipass not in resolved.parents


# ---- Settings 装配（默认工厂经 dataclass default_factory 生效）----


def test_settings_uses_default_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.delenv("PD_RUNS_DIR", raising=False)
    settings = Settings(provider="synthetic")
    assert settings.runs_dir == Path(config.__file__).resolve().parents[3] / "runs"


def test_settings_explicit_runs_dir_preserved() -> None:
    explicit = Path("/tmp/pd-explicit-runs")
    settings = Settings(provider="synthetic", runs_dir=explicit)
    assert settings.runs_dir == explicit


# ---- run.py build_settings：--runs-dir 缺省走 Settings 默认（单一事实源）----


def _load_run_module():
    run_path = Path(__file__).resolve().parents[1] / "run.py"
    spec = importlib.util.spec_from_file_location("pd_run_module", run_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _args(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "port": 8737,
        "host": "127.0.0.1",
        "provider": "synthetic",
        "transport": "auto",
        "runs_dir": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_build_settings_default_uses_config_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.delenv("PD_RUNS_DIR", raising=False)
    module = _load_run_module()
    settings = module.build_settings(_args())
    assert settings.runs_dir == Settings(provider="synthetic").runs_dir


def test_build_settings_explicit_runs_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    module = _load_run_module()
    custom = tmp_path / "my-runs"
    settings = module.build_settings(_args(runs_dir=str(custom)))
    assert settings.runs_dir == custom
