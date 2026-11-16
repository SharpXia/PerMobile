from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QGridLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ..models.device import EnvironmentSnapshot
from ..models.history import HistoryEntry
from .base_page import BasePage


class HomePage(BasePage):
    navigation_requested = Signal(str)

    def __init__(self, workspace: str, parent: QWidget | None = None) -> None:
        super().__init__("首页", "查看当前工作区状态，并快速进入常用性能任务。", parent)

        self.add_section_title("当前 workspace")
        self.workspace_label = QLabel(workspace, self)
        self.workspace_label.setObjectName("currentWorkspaceLabel")
        self.workspace_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.workspace_label.setWordWrap(True)
        self.layout.addWidget(self.workspace_label)

        change_button = QPushButton("在设置中更改", self)
        change_button.setAccessibleName("打开设置并更改 workspace")
        change_button.clicked.connect(lambda: self.navigation_requested.emit("settings"))
        self.layout.addWidget(change_button, 0, Qt.AlignmentFlag.AlignLeft)

        self.add_section_title("环境摘要")
        self.environment_summary = QLabel(
            "尚未执行环境检测。前往“环境与设备”查看 CLI、adb 和设备状态。",
            self,
        )
        self.environment_summary.setObjectName("mutedText")
        self.environment_summary.setWordWrap(True)
        self.layout.addWidget(self.environment_summary)

        self.add_section_title("快捷入口")
        shortcuts = QGridLayout()
        shortcuts.setHorizontalSpacing(12)
        shortcuts.setVerticalSpacing(10)
        for index, (page_id, label) in enumerate(
            (
                ("environment", "环境与设备"),
                ("run", "单版本采集"),
                ("ab", "A/B 对比"),
                ("diagnose", "聚焦诊断"),
            )
        ):
            button = QPushButton(label, self)
            button.setMinimumHeight(42)
            button.clicked.connect(lambda _checked=False, target=page_id: self.navigation_requested.emit(target))
            shortcuts.addWidget(button, index // 2, index % 2)
        shortcuts_host = QWidget(self)
        shortcuts_host.setLayout(shortcuts)
        self.layout.addWidget(shortcuts_host)

        self.add_section_title("最近任务")
        self.recent_label = QLabel("暂无可识别的历史结果。", self)
        self.recent_label.setObjectName("mutedText")
        self.recent_label.setWordWrap(True)
        self.layout.addWidget(self.recent_label)
        self.finish()

    def set_workspace(self, workspace: str) -> None:
        self.workspace_label.setText(workspace)

    def set_environment_snapshot(self, snapshot: EnvironmentSnapshot) -> None:
        cli = snapshot.tool("cli")
        adb = snapshot.tool("adb")
        online = sum(1 for device in snapshot.devices if device.online)
        self.environment_summary.setText(
            f"CLI：{'可用' if cli and cli.available else '不可用'}；"
            f"adb：{'可用' if adb and adb.available else '不可用'}；"
            f"在线设备：{online}。"
        )

    def set_recent_entries(self, entries: Sequence[HistoryEntry]) -> None:
        recent = list(entries[:3])
        if not recent:
            self.recent_label.setText("暂无可识别的历史结果。")
            return
        self.recent_label.setText(
            "\n".join(
                f"• {entry.name}｜{entry.status}｜{entry.updated_at.astimezone():%m-%d %H:%M}"
                for entry in recent
            )
        )
