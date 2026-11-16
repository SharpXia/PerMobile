from __future__ import annotations

from enum import StrEnum

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QStyle, QVBoxLayout, QWidget


class ValidationSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


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


class ValidationBanner(QFrame):
    """Page-level validation summary that never relies on color alone."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("validationBanner")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAccessibleName("校验结果")

        outer = QHBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(10)

        self.icon_label = QLabel(self)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        outer.addWidget(self.icon_label)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(3)
        self.title_label = QLabel(self)
        self.title_label.setObjectName("validationTitle")
        text_layout.addWidget(self.title_label)
        self.message_label = QLabel(self)
        self.message_label.setWordWrap(True)
        self.message_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text_layout.addWidget(self.message_label)
        outer.addLayout(text_layout, 1)

        self._messages: tuple[str, ...] = ()
        self._severity = ValidationSeverity.INFO
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
        self.title_label.setText(_SEVERITY_TITLE[severity])
        self.message_label.setText("\n".join(f"• {message}" for message in normalized))
        icon = self.style().standardIcon(_SEVERITY_ICON[severity])
        self.icon_label.setPixmap(icon.pixmap(20, 20))
        self.setAccessibleDescription(f"{_SEVERITY_TITLE[severity]}：{'；'.join(normalized)}")
        self.style().unpolish(self)
        self.style().polish(self)
        self.show()

    def clear(self) -> None:
        self._messages = ()
        self.message_label.clear()
        self.setAccessibleDescription("")
        self.hide()
