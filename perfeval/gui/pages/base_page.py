from __future__ import annotations

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget


class BasePage(QWidget):
    """Shared page spacing and heading hierarchy for MVP screens."""

    def __init__(self, title: str, description: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.page_title = title

        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(34, 28, 34, 30)
        self.layout.setSpacing(20)

        header = QFrame(self)
        header.setObjectName("pageHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 4)
        header_layout.setSpacing(0)

        heading = QVBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.setSpacing(6)

        eyebrow = QLabel("PERFORMANCE LAB", header)
        eyebrow.setObjectName("pageEyebrow")
        heading.addWidget(eyebrow)

        self.title_label = QLabel(title, header)
        self.title_label.setObjectName("pageTitle")
        self.title_label.setAccessibleName(f"当前页面：{title}")
        self.title_label.setWordWrap(True)
        self.title_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        heading.addWidget(self.title_label)

        self.description_label = QLabel(description, header)
        self.description_label.setObjectName("pageDescription")
        self.description_label.setWordWrap(True)
        self.description_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        heading.addWidget(self.description_label)
        header_layout.addLayout(heading, 1)
        self.layout.addWidget(header)

    def add_section_title(self, title: str) -> QLabel:
        label = QLabel(title, self)
        label.setObjectName("sectionTitle")
        self.layout.addWidget(label)
        return label

    def finish(self) -> None:
        self.layout.addStretch(1)
