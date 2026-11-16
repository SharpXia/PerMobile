from __future__ import annotations

import unicodedata
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from perfeval.core.config import sanitize_slug
from perfeval.gui.services.command_builder import CandidateInput

from .path_picker import PathPicker, PathPickerMode


_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class CandidateRow(QFrame):
    """One ordered candidate entry; ownership stays with CandidateEditor."""

    changed = Signal()
    remove_requested = Signal(object)
    move_requested = Signal(object, int)

    def __init__(
        self,
        candidate_id: str,
        apk: str = "",
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("candidateRow")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.index_label = QLabel(self)
        self.index_label.setObjectName("candidateIndex")

        self.id_edit = QLineEdit(candidate_id, self)
        self.id_edit.setObjectName("candidateId")
        self.id_edit.setAccessibleName("Candidate ID")
        self.id_edit.setPlaceholderText("例如 v4_3_2")

        self.apk_picker = PathPicker(
            mode=PathPickerMode.EXISTING_FILE,
            caption="选择 Candidate APK",
            file_filter="Android APK (*.apk);;所有文件 (*)",
            placeholder="选择 candidate APK",
            parent=self,
        )
        self.apk_picker.setObjectName("candidateApk")
        if apk:
            self.apk_picker.set_path(apk)

        self.slug_label = QLabel(self)
        self.slug_label.setObjectName("candidateOutputSlug")
        self.slug_label.setWordWrap(True)
        self.slug_label.setAccessibleName("Candidate 输出目录名")

        self.move_up_button = QPushButton("上移", self)
        self.move_up_button.setAccessibleName("上移 Candidate")
        self.move_down_button = QPushButton("下移", self)
        self.move_down_button.setAccessibleName("下移 Candidate")
        self.remove_button = QPushButton("删除", self)
        self.remove_button.setAccessibleName("删除 Candidate")

        actions = QWidget(self)
        actions_layout = QHBoxLayout(actions)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(6)
        actions_layout.addWidget(self.move_up_button)
        actions_layout.addWidget(self.move_down_button)
        actions_layout.addWidget(self.remove_button)

        layout = QGridLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(8)
        layout.addWidget(self.index_label, 0, 0)
        layout.addWidget(actions, 0, 1, 1, 2)
        layout.addWidget(QLabel("ID", self), 1, 0)
        layout.addWidget(self.id_edit, 1, 1, 1, 2)
        layout.addWidget(QLabel("APK", self), 2, 0)
        layout.addWidget(self.apk_picker, 2, 1, 1, 2)
        layout.addWidget(self.slug_label, 3, 0, 1, 3)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(2, 0)

        self.id_edit.textChanged.connect(self._on_value_changed)
        self.apk_picker.path_changed.connect(self.changed)
        self.move_up_button.clicked.connect(lambda: self.move_requested.emit(self, -1))
        self.move_down_button.clicked.connect(lambda: self.move_requested.emit(self, 1))
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self))
        self._update_slug()

    @property
    def candidate_id(self) -> str:
        return self.id_edit.text()

    @property
    def apk(self) -> str:
        return self.apk_picker.path

    @property
    def output_slug(self) -> str:
        error = _id_error(self.candidate_id)
        return "" if error else sanitize_slug(
            unicodedata.normalize("NFKC", self.candidate_id.strip())
        )

    def set_position(self, index: int, total: int) -> None:
        self.index_label.setText(f"Candidate {index + 1}")
        self.index_label.setAccessibleName(f"Candidate {index + 1}")
        self.move_up_button.setEnabled(index > 0)
        self.move_down_button.setEnabled(index < total - 1)
        self.remove_button.setEnabled(total > 1)

    def _on_value_changed(self) -> None:
        self._update_slug()
        self.changed.emit()

    def _update_slug(self) -> None:
        error = _id_error(self.candidate_id)
        if error:
            self.slug_label.setText(f"输出目录名：待修正（{error}）")
            return
        self.slug_label.setText(f"输出目录名：{self.output_slug}")


class CandidateEditor(QWidget):
    """Ordered 1–4 candidate editor with preflight ID collision checks."""

    changed = Signal()

    def __init__(
        self,
        *,
        baseline_id: str = "baseline",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._baseline_id = baseline_id
        self._rows: list[CandidateRow] = []

        self.rows_container = QWidget(self)
        self.rows_layout = QVBoxLayout(self.rows_container)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(8)

        self.add_button = QPushButton("添加 Candidate", self)
        self.add_button.setObjectName("addCandidateButton")
        self.add_button.setAccessibleName("添加 Candidate，最多四个")
        self.add_button.clicked.connect(self.add_candidate)

        footer = QHBoxLayout()
        footer.addWidget(self.add_button)
        footer.addStretch(1)
        self.count_label = QLabel(self)
        self.count_label.setAccessibleName("Candidate 数量")
        footer.addWidget(self.count_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.rows_container)
        layout.addLayout(footer)

        self.add_candidate("candidate")

    @property
    def count(self) -> int:
        return len(self._rows)

    @property
    def rows(self) -> tuple[CandidateRow, ...]:
        return tuple(self._rows)

    def set_baseline_id(self, baseline_id: str) -> None:
        self._baseline_id = baseline_id
        self.changed.emit()

    def add_candidate(self, candidate_id: str | None = None, apk: str = "") -> CandidateRow | None:
        if len(self._rows) >= 4:
            return None
        resolved_id = candidate_id or self._next_candidate_id()
        row = CandidateRow(resolved_id, apk, parent=self.rows_container)
        row.changed.connect(self.changed)
        row.remove_requested.connect(self._remove_row)
        row.move_requested.connect(self._move_row)
        self._rows.append(row)
        self.rows_layout.addWidget(row)
        self._refresh_rows()
        self.changed.emit()
        return row

    def remove_candidate(self, index: int) -> bool:
        if len(self._rows) <= 1 or not 0 <= index < len(self._rows):
            return False
        row = self._rows[index]
        self._remove_row(row)
        return True

    def move_candidate(self, index: int, offset: int) -> bool:
        target = index + offset
        if not 0 <= index < len(self._rows) or not 0 <= target < len(self._rows):
            return False
        row = self._rows[index]
        self._move_row(row, offset)
        return True

    def candidate_inputs(self) -> tuple[CandidateInput, ...]:
        return tuple(
            CandidateInput(row.candidate_id, Path(row.apk))
            for row in self._rows
        )

    def validation_messages(self) -> tuple[str, ...]:
        messages: list[str] = []
        baseline_error = _id_error(self._baseline_id)
        if baseline_error:
            messages.append(f"Baseline ID {baseline_error}")

        seen_ids: dict[str, str] = {}
        seen_slugs: dict[str, str] = {}
        if not baseline_error:
            baseline_key = unicodedata.normalize("NFKC", self._baseline_id).casefold()
            baseline_slug = sanitize_slug(
                unicodedata.normalize("NFKC", self._baseline_id.strip())
            ).casefold()
            seen_ids[baseline_key] = "Baseline"
            seen_slugs[baseline_slug] = "Baseline"

        for index, row in enumerate(self._rows, start=1):
            label = f"Candidate {index}"
            error = _id_error(row.candidate_id)
            if error:
                messages.append(f"{label} ID {error}")
            else:
                normalized_id = unicodedata.normalize("NFKC", row.candidate_id)
                id_key = normalized_id.casefold()
                slug = sanitize_slug(unicodedata.normalize("NFKC", row.candidate_id.strip()))
                slug_key = slug.casefold()
                if id_key in seen_ids:
                    messages.append(f"{label} ID 与 {seen_ids[id_key]} 重复：{row.candidate_id}")
                elif slug_key in seen_slugs:
                    messages.append(
                        f"{label} 清洗后的输出名与 {seen_slugs[slug_key]} 冲突：{slug}"
                    )
                seen_ids[id_key] = label
                seen_slugs[slug_key] = label
            if not row.apk:
                messages.append(f"{label} APK 不能为空")
        return tuple(messages)

    def clear(self) -> None:
        for row in self._rows:
            self.rows_layout.removeWidget(row)
            row.deleteLater()
        self._rows = []
        self.add_candidate("candidate")

    def _next_candidate_id(self) -> str:
        used = {row.candidate_id.casefold() for row in self._rows}
        for index in range(1, 5):
            candidate_id = "candidate" if index == 1 else f"candidate-{index}"
            if candidate_id.casefold() not in used:
                return candidate_id
        return f"candidate-{len(self._rows) + 1}"

    def _remove_row(self, row: CandidateRow) -> None:
        if len(self._rows) <= 1 or row not in self._rows:
            return
        self._rows.remove(row)
        self.rows_layout.removeWidget(row)
        row.deleteLater()
        self._refresh_rows()
        self.changed.emit()

    def _move_row(self, row: CandidateRow, offset: int) -> None:
        if row not in self._rows:
            return
        index = self._rows.index(row)
        target = index + offset
        if not 0 <= target < len(self._rows):
            return
        self._rows.pop(index)
        self._rows.insert(target, row)
        self.rows_layout.removeWidget(row)
        self.rows_layout.insertWidget(target, row)
        self._refresh_rows()
        self.changed.emit()

    def _refresh_rows(self) -> None:
        total = len(self._rows)
        for index, row in enumerate(self._rows):
            row.set_position(index, total)
        self.add_button.setEnabled(total < 4)
        self.count_label.setText(f"{total} / 4")


def _id_error(value: str) -> str | None:
    if not value or not value.strip():
        return "不能为空"
    if value != value.rstrip(" ."):
        return "不能以空格或点结尾"
    if any(ord(char) < 32 for char in value) or any(char in '<>:"/\\|?*' for char in value):
        return "包含跨平台非法字符"
    normalized = unicodedata.normalize("NFKC", value.strip())
    basename = normalized.split(".", 1)[0].upper()
    slug = sanitize_slug(normalized)
    slug_basename = slug.split(".", 1)[0].upper()
    if basename in _WINDOWS_RESERVED_NAMES or slug_basename in _WINDOWS_RESERVED_NAMES:
        return "不能使用 Windows 保留名称"
    return None
