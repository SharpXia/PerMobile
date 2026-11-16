from __future__ import annotations

import importlib.util
import os
import time
import zipfile
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = ROOT / "packaging" / "pyinstaller" / "build.py"
SPEC = importlib.util.spec_from_file_location("perfeval_packaging_build", BUILD_SCRIPT)
assert SPEC is not None and SPEC.loader is not None
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)


def test_source_date_epoch_defaults_to_current_build_time(monkeypatch) -> None:
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    before = int(time.time())

    value = build._source_date_epoch()

    assert before <= value <= int(time.time())


def test_source_date_epoch_honors_explicit_reproducible_value(monkeypatch) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")

    assert build._source_date_epoch() == 1700000000


def test_zip_uses_local_wall_clock_time(tmp_path: Path) -> None:
    source = tmp_path / "PerfEval Lite.app"
    source.mkdir()
    (source / "payload.txt").write_text("payload", encoding="utf-8")
    archive = tmp_path / "artifact.zip"
    archive.write_bytes(b"stale archive")
    linked_archive = tmp_path / "stale-hard-link.zip"
    os.link(archive, linked_archive)
    epoch = 1786438800

    build._write_deterministic_zip(source, archive, source_epoch=epoch)

    expected = datetime.fromtimestamp(epoch).timetuple()[:6]
    assert linked_archive.read_bytes() == b"stale archive"
    assert int(archive.stat().st_mtime) == epoch
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.getinfo("PerfEval Lite.app/").date_time == expected
        assert bundle.getinfo("PerfEval Lite.app/payload.txt").date_time == expected


def test_normalize_tree_mtime_applies_real_epoch_to_bundle(tmp_path: Path) -> None:
    root = tmp_path / "PerfEval Lite.app"
    payload = root / "Contents/MacOS/PerfEval-Lite"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"gui")
    epoch = 1786438800

    build._normalize_tree_mtime(root, source_epoch=epoch)

    assert int(root.stat().st_mtime) == epoch
    assert int(payload.stat().st_mtime) == epoch
    assert os.path.getmtime(payload) == epoch


def test_codesign_identity_selection_prefers_stable_mac_identities() -> None:
    output = """
      1) AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA "iPhone Distribution: Example"
      2) BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB "3rd Party Mac Developer Application: Example"
      3) CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC "Apple Development: Example"
      4) DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD "Developer ID Application: Example"
    """

    assert build._select_codesign_identity(output) == "D" * 40


def test_codesign_identity_selection_falls_back_to_apple_development() -> None:
    output = """
      1) AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA "iPhone Distribution: Example"
      2) CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC "Apple Development: Example"
    """

    assert build._select_codesign_identity(output) == "C" * 40


def test_codesign_identity_selection_reuses_cached_identity() -> None:
    output = """
      1) AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA "Apple Development: First"
      2) BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB "Apple Development: Cached"
    """

    assert build._select_codesign_identity(output, preferred="b" * 40) == "B" * 40
