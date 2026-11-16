from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QRect, Qt, Signal
from PySide6.QtGui import QMouseEvent, QPainter
from PySide6.QtWidgets import QComboBox, QStyleOptionViewItem, QStyledItemDelegate, QWidget


_REMOVE_AREA_WIDTH = 32


class _RecentValueDelegate(QStyledItemDelegate):
    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index,
    ) -> None:
        text_option = QStyleOptionViewItem(option)
        text_option.rect = option.rect.adjusted(0, 0, -_REMOVE_AREA_WIDTH, 0)
        super().paint(painter, text_option, index)

        painter.save()
        painter.setPen(option.palette.color(option.palette.ColorRole.PlaceholderText))
        painter.drawText(
            _remove_rect(option.rect),
            Qt.AlignmentFlag.AlignCenter,
            "×",
        )
        painter.restore()


class RecentValueComboBox(QComboBox):
    """Editable combo whose popup rows can be removed from saved history."""

    remove_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.view().setItemDelegate(_RecentValueDelegate(self.view()))
        self.view().viewport().installEventFilter(self)

    def set_recent_values(self, values: tuple[str, ...], *, current_text: str) -> None:
        self.blockSignals(True)
        try:
            self.clear()
            self.addItems(values)
            self.setEditText(current_text)
        finally:
            self.blockSignals(False)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt API
        if watched is self.view().viewport() and isinstance(event, QMouseEvent):
            if event.button() is Qt.MouseButton.LeftButton and event.type() in {
                QEvent.Type.MouseButtonPress,
                QEvent.Type.MouseButtonRelease,
            }:
                position = event.position().toPoint()
                index = self.view().indexAt(position)
                if index.isValid() and _remove_rect(self.view().visualRect(index)).contains(position):
                    if event.type() is QEvent.Type.MouseButtonRelease:
                        self.remove_requested.emit(str(index.data()))
                    return True
        return super().eventFilter(watched, event)


def _remove_rect(item_rect: QRect) -> QRect:
    return QRect(
        item_rect.right() - _REMOVE_AREA_WIDTH + 1,
        item_rect.top(),
        _REMOVE_AREA_WIDTH,
        item_rect.height(),
    )
