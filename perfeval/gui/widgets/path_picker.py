from __future__ import annotations

from enum import StrEnum

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QWidget,
)


class PathPickerMode(StrEnum):
    DIRECTORY = "directory"
    EXISTING_FILE = "existing_file"
    SAVE_FILE = "save_file"


class PathPicker(QWidget):
    """A keyboard-accessible path field with a native browse dialog."""

    path_changed = Signal(str)
    browse_requested = Signal()

    def __init__(
        self,
        *,
        mode: PathPickerMode = PathPickerMode.DIRECTORY,
        caption: str = "选择路径",
        file_filter: str = "所有文件 (*)",
        placeholder: str = "选择或输入路径",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.mode = mode
        self.caption = caption
        self.file_filter = file_filter
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.line_edit = QLineEdit(self)
        self.line_edit.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.line_edit.setPlaceholderText(placeholder)
        self.line_edit.setAccessibleName(caption)
        self.line_edit.textChanged.connect(self.path_changed)
        layout.addWidget(self.line_edit, 1)

        self.browse_button = QPushButton("浏览…", self)
        self.browse_button.setObjectName("pathBrowseButton")
        self.browse_button.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )
        self.browse_button.setAccessibleName(f"{caption}，浏览")
        self.browse_button.clicked.connect(self._browse)
        layout.addWidget(self.browse_button)

    @property
    def path(self) -> str:
        return self.line_edit.text().strip()

    def set_path(self, path: str) -> None:
        self.line_edit.setText(path)

    def clear(self) -> None:
        self.line_edit.clear()

    def set_read_only(self, read_only: bool) -> None:
        self.line_edit.setReadOnly(read_only)
        self.browse_button.setEnabled(not read_only)

    def _browse(self) -> None:
        self.browse_requested.emit()
        initial = self.path
        selected = ""
        if self.mode is PathPickerMode.DIRECTORY:
            selected = QFileDialog.getExistingDirectory(self, self.caption, initial)
        elif self.mode is PathPickerMode.EXISTING_FILE:
            selected, _ = QFileDialog.getOpenFileName(self, self.caption, initial, self.file_filter)
        else:
            selected, _ = QFileDialog.getSaveFileName(self, self.caption, initial, self.file_filter)
        if selected:
            self.set_path(selected)
