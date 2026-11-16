from __future__ import annotations

from PySide6.QtWidgets import QFileDialog, QWidget

from perfeval.gui.widgets.activity_indicator import LoadingOverlay
from perfeval.gui.widgets.path_picker import PathPicker, PathPickerMode
from perfeval.gui.widgets.page_toast import PageToast
from perfeval.gui.widgets.validation_banner import ValidationBanner, ValidationSeverity


def test_path_picker_emits_changes_and_uses_directory_dialog(qtbot, monkeypatch, tmp_path) -> None:
    picker = PathPicker(mode=PathPickerMode.DIRECTORY, caption="选择 workspace")
    qtbot.addWidget(picker)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_args, **_kwargs: str(tmp_path))

    with qtbot.waitSignal(picker.path_changed):
        picker.browse_button.click()

    assert picker.path == str(tmp_path)
    assert picker.line_edit.accessibleName() == "选择 workspace"


def test_validation_banner_includes_textual_severity_and_clears(qtbot) -> None:
    banner = ValidationBanner()
    qtbot.addWidget(banner)

    banner.set_messages(["scenario 不能为空", "repeat 必须大于 0"], severity=ValidationSeverity.ERROR)

    assert banner.isVisibleTo(banner.parentWidget())
    assert banner.title_label.text() == "错误"
    assert banner.messages == ("scenario 不能为空", "repeat 必须大于 0")
    assert "错误" in banner.accessibleDescription()

    banner.clear()
    assert not banner.isVisible()
    assert banner.messages == ()


def test_page_toast_overlays_parent_and_hides_after_timeout(qtbot) -> None:
    parent = QWidget()
    parent.resize(800, 500)
    qtbot.addWidget(parent)
    toast = PageToast(parent)
    parent.show()

    toast.set_messages("扫描完成", severity=ValidationSeverity.INFO, duration_ms=250)

    assert toast.isVisibleTo(parent)
    assert toast.messages == ("扫描完成",)
    assert toast.geometry().bottom() < parent.rect().bottom()
    qtbot.waitUntil(lambda: toast.isHidden(), timeout=1000)


def test_loading_overlay_tracks_parent_and_spins_only_while_active(qtbot) -> None:
    host = QWidget()
    qtbot.addWidget(host)
    host.resize(400, 300)
    host.show()  # 隐藏控件不派发 Resize，需先显示才能验证跟随
    overlay = LoadingOverlay(host, text="正在扫描历史结果…")

    assert not overlay.is_active
    assert not overlay.isVisibleTo(host)

    overlay.start()
    assert overlay.is_active
    assert overlay.isVisibleTo(host)
    assert overlay.indicator.is_spinning
    assert overlay.label.text() == "正在扫描历史结果…"

    host.resize(640, 480)
    qtbot.waitUntil(lambda: overlay.size() == host.size())

    overlay.stop()
    assert not overlay.is_active
    # 淡出动画结束后才真正隐藏并停转。
    qtbot.waitUntil(lambda: not overlay.isVisibleTo(host), timeout=2000)
    qtbot.waitUntil(lambda: not overlay.indicator.is_spinning, timeout=2000)
