from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QStyle, QWidget

from .validation_banner import ValidationSeverity


_SEVERITY_TITLE = {
    ValidationSeverity.ERROR: "错误",
    ValidationSeverity.WARNING: "警告",
    ValidationSeverity.INFO: "提示",
}

_SEVERITY_ICON = {
    ValidationSeverity.ERROR: QStyle.StandardPixmap.SP_MessageBoxCritical,
    ValidationSeverity.WARNING: QStyle.StandardPixmap.SP_MessageBoxWarning,
    ValidationSeverity.INFO: QStyle.StandardPixmap.SP_MessageBoxInformation,
}

_DEFAULT_DURATION_MS = {
    ValidationSeverity.ERROR: 7_000,
    ValidationSeverity.WARNING: 5_000,
    ValidationSeverity.INFO: 3_000,
}


class PageToast(QFrame):
    """Short-lived page overlay that does not consume layout space."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("pageToast")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAccessibleName("页面提示")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 11, 14, 11)
        layout.setSpacing(10)

        self.icon_label = QLabel(self)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.icon_label)

        self.message_label = QLabel(self)
        self.message_label.setObjectName("pageToastText")
        self.message_label.setWordWrap(True)
        self.message_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.message_label, 1)

        self._messages: tuple[str, ...] = ()
        self._severity = ValidationSeverity.INFO
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        parent.installEventFilter(self)
        self.hide()

    @property
    def messages(self) -> tuple[str, ...]:
        return self._messages

    @property
    def severity(self) -> ValidationSeverity:
        return self._severity

    def set_messages(
        self,
        messages: str | list[str] | tuple[str, ...],
        *,
        severity: ValidationSeverity = ValidationSeverity.ERROR,
        duration_ms: int | None = None,
    ) -> None:
        if isinstance(messages, str):
            normalized = (messages.strip(),) if messages.strip() else ()
        else:
            normalized = tuple(message.strip() for message in messages if message.strip())
        if not normalized:
            self.clear()
            return

        self._messages = normalized
        self._severity = severity
        self.setProperty("severity", severity.value)
        self.message_label.setText(_display_text(normalized))
        icon = self.style().standardIcon(_SEVERITY_ICON[severity])
        self.icon_label.setPixmap(icon.pixmap(20, 20))
        self.setAccessibleDescription(f"{_SEVERITY_TITLE[severity]}：{'；'.join(normalized)}")
        self.style().unpolish(self)
        self.style().polish(self)
        self._reposition()
        self.show()
        self.raise_()
        timeout = _DEFAULT_DURATION_MS[severity] if duration_ms is None else max(250, duration_ms)
        self._timer.start(timeout)

    def clear(self) -> None:
        self._timer.stop()
        self._messages = ()
        self.message_label.clear()
        self.setAccessibleDescription("")
        self.hide()

    def eventFilter(self, watched: object, event: QEvent) -> bool:  # noqa: N802 - Qt API
        if watched is self.parentWidget() and event.type() in {
            QEvent.Type.Resize,
            QEvent.Type.Show,
            QEvent.Type.LayoutRequest,
        }:
            self._reposition()
        return super().eventFilter(watched, event)

    def _reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        width = min(560, max(280, parent.width() - 48))
        self.message_label.setFixedWidth(max(180, width - 68))
        height = self.sizeHint().height()
        self.resize(width, height)
        self.move(
            max(12, (parent.width() - width) // 2),
            max(12, parent.height() - height - 24),
        )


def _display_text(messages: tuple[str, ...]) -> str:
    visible = messages[:3]
    text = "\n".join(visible)
    if len(messages) > len(visible):
        text += f"\n…另有 {len(messages) - len(visible)} 条提示"
    return text


__all__ = ["PageToast"]
