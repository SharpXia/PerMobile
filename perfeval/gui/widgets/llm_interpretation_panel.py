from __future__ import annotations

from urllib.parse import urlsplit

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..design_tokens import COMPONENTS
from ..models.llm import (
    INTERPRETATION_DEPTH_DESCRIPTIONS,
    INTERPRETATION_DEPTH_LABELS,
    InterpretationDepth,
    InterpretationSource,
    InterpretationState,
)
from ..services.llm_artifacts import read_snapshot
from ..services.llm_jobs import InterpretationJobManager
from ..services.llm_profiles import LlmProfileService


_STATE_LABELS = {
    InterpretationState.NOT_GENERATED: "未生成",
    InterpretationState.QUEUED: "排队中",
    InterpretationState.RUNNING: "生成中",
    InterpretationState.COMPLETED: "已完成",
    InterpretationState.FAILED: "失败",
    InterpretationState.CANCELLED: "已取消",
    InterpretationState.INTERRUPTED: "上次任务已中断",
}


class LlmInterpretationPanel(QFrame):
    settings_requested = Signal()
    open_result_requested = Signal(object)

    def __init__(
        self,
        profiles: LlmProfileService,
        jobs: InterpretationJobManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.profiles = profiles
        self.jobs = jobs
        self._source: InterpretationSource | None = None
        self._current_job_id: str | None = None
        self.setObjectName("llmInterpretationPanel")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        self.title_row = QHBoxLayout()
        self.title_row.setSpacing(COMPONENTS.space_2)
        self.title_label = QLabel("LLM 解读", self)
        self.title_label.setObjectName("sectionTitle")
        self.title_row.addWidget(self.title_label)
        self.state_label = QLabel("未生成", self)
        self.state_label.setObjectName("llmInterpretationState")
        self.state_label.setAccessibleName("LLM 解读状态")
        self.title_row.addWidget(self.state_label)
        self.title_row.addStretch(1)

        self.depth_label = QLabel("解读深度", self)
        self.depth_label.setObjectName("mutedText")
        self.depth_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.title_row.addWidget(
            self.depth_label,
            0,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
        )
        self.depth_combo = QComboBox(self)
        self.depth_combo.setObjectName("llmInterpretationDepthCombo")
        self.depth_combo.setAccessibleName("LLM 解读深度")
        self.depth_combo.setAccessibleDescription("控制解读篇幅、分析深度、日志片段数量和输出上限")
        self.depth_combo.setMinimumWidth(108)
        for depth, label in INTERPRETATION_DEPTH_LABELS.items():
            self.depth_combo.addItem(label, depth.value)
            self.depth_combo.setItemData(
                self.depth_combo.count() - 1,
                INTERPRETATION_DEPTH_DESCRIPTIONS[depth],
                Qt.ItemDataRole.ToolTipRole,
            )
        self._set_depth(self.profiles.interpretation_depth)
        self.depth_combo.currentIndexChanged.connect(self._depth_changed)
        self.title_row.addWidget(
            self.depth_combo,
            0,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
        )
        layout.addLayout(self.title_row)

        self.message_label = QLabel("选择模型后可在后台生成性能解读。", self)
        self.message_label.setObjectName("mutedText")
        self.message_label.setWordWrap(True)
        self.message_label.hide()

        self.controls_row = QHBoxLayout()
        self.controls_row.setSpacing(COMPONENTS.space_2)
        self.profile_combo = QComboBox(self)
        self.profile_combo.setObjectName("llmProfileCombo")
        self.profile_combo.setAccessibleName("LLM 模型配置")
        self.profile_combo.currentIndexChanged.connect(self._update_disclosure)
        self.controls_row.addWidget(self.profile_combo, 1)

        self.generate_button = QPushButton("生成解读", self)
        self.generate_button.setObjectName("primaryActionButton")
        self.generate_button.clicked.connect(self._generate)
        self.controls_row.addWidget(self.generate_button)
        self.cancel_button = QPushButton("取消", self)
        self.cancel_button.clicked.connect(self._cancel)
        self.cancel_button.hide()
        self.controls_row.addWidget(self.cancel_button)
        self.view_button = QPushButton("查看解读", self)
        self.view_button.clicked.connect(self._open)
        self.controls_row.addWidget(self.view_button)
        self.settings_button = QPushButton("前往设置", self)
        self.settings_button.clicked.connect(self.settings_requested)
        self.controls_row.addWidget(self.settings_button)
        for button in (
            self.generate_button,
            self.cancel_button,
            self.view_button,
            self.settings_button,
        ):
            button.setFixedHeight(COMPONENTS.primary_height)
        layout.addLayout(self.controls_row)

        self.disclosure_label = QLabel(self)
        self.disclosure_label.setObjectName("mutedText")
        self.disclosure_label.setWordWrap(True)
        self.disclosure_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.disclosure_label)

        profiles.changed.connect(self._reload_profiles)
        jobs.job_state_changed.connect(self._on_job_state)
        jobs.job_completed.connect(self._on_job_completed)
        jobs.job_failed.connect(self._on_job_failed)
        self._reload_profiles()
        self.set_source(None)

    @property
    def source(self) -> InterpretationSource | None:
        return self._source

    @property
    def has_active_job(self) -> bool:
        return bool(self._source and self.jobs.is_active_for(self._source))

    def set_source(self, source: InterpretationSource | None) -> None:
        self._source = source
        self._current_job_id = self.jobs.job_for_source(source) if source is not None else None
        self.setEnabled(source is not None)
        self.refresh()

    def refresh(self) -> None:
        source = self._source
        if source is None:
            self._set_state(InterpretationState.NOT_GENERATED, "完成测试或选择历史结果后可生成解读。")
            self.view_button.setEnabled(False)
            self.generate_button.setEnabled(False)
            self.cancel_button.hide()
            return
        snapshot = read_snapshot(source.root)
        if snapshot.depth is not None:
            self._set_depth(snapshot.depth)
        active_job = self.jobs.job_for_source(source)
        self._current_job_id = active_job
        active = active_job is not None
        self._set_state(snapshot.state, snapshot.message or _default_message(snapshot.state))
        self.view_button.setEnabled(snapshot.html_path is not None and snapshot.html_path.is_file())
        self.generate_button.setEnabled(not active)
        self.generate_button.setText("重新生成" if snapshot.html_path is not None else "生成解读")
        self.cancel_button.setVisible(active)
        self.settings_button.setVisible(not bool(self.profiles.usable_profiles))
        self._update_disclosure()

    def _reload_profiles(self) -> None:
        selected = self.profile_combo.currentData()
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for profile in self.profiles.profiles:
            suffix = "" if self.profiles.has_api_key(profile.profile_id) else "（缺少 API Key）"
            self.profile_combo.addItem(f"{profile.name} · {profile.model}{suffix}", profile.profile_id)
        preferred = selected or self.profiles.default_profile_id
        index = self.profile_combo.findData(preferred)
        self.profile_combo.setCurrentIndex(index if index >= 0 else (0 if self.profile_combo.count() else -1))
        self.profile_combo.blockSignals(False)
        self.refresh()

    def _generate(self) -> None:
        source = self._source
        profile_id = self.profile_combo.currentData()
        if source is None:
            return
        if not profile_id or not self.profiles.has_api_key(str(profile_id)):
            self._set_state(InterpretationState.NOT_GENERATED, "请先到设置页配置 LLM URL、模型和 API Key。")
            self.settings_requested.emit()
            return
        depth = InterpretationDepth(str(self.depth_combo.currentData() or InterpretationDepth.STANDARD.value))
        self.profiles.interpretation_depth = depth
        try:
            self._current_job_id = self.jobs.start(
                source,
                str(profile_id),
                depth=depth,
            )
        except Exception as exc:
            self._set_state(InterpretationState.FAILED, str(exc))
            return
        self.refresh()

    def _cancel(self) -> None:
        if self._current_job_id:
            self.jobs.cancel(self._current_job_id)

    def _open(self) -> None:
        if self._source is not None:
            descriptor = self._source.interpretation_descriptor
            if descriptor.exists:
                self.open_result_requested.emit(descriptor)

    def _on_job_state(self, job_id: str, source: InterpretationSource, state: InterpretationState) -> None:
        if self._source is None or source.source_key != self._source.source_key:
            return
        self._current_job_id = job_id if state in {InterpretationState.QUEUED, InterpretationState.RUNNING} else None
        self.refresh()

    def _on_job_completed(self, _job_id: str, source: InterpretationSource, _descriptor: object) -> None:
        if self._source is not None and source.source_key == self._source.source_key:
            self._current_job_id = None
            self.refresh()

    def _on_job_failed(self, _job_id: str, source: InterpretationSource, message: str) -> None:
        if self._source is not None and source.source_key == self._source.source_key:
            self._current_job_id = None
            self._set_state(InterpretationState.FAILED, message)
            self.refresh()

    def _set_state(self, state: InterpretationState, message: str) -> None:
        self.state_label.setText(_STATE_LABELS[state])
        self.state_label.setAccessibleDescription(message)
        self.state_label.setToolTip(message)
        self.state_label.setProperty("interpretationState", state.value)
        self.state_label.style().unpolish(self.state_label)
        self.state_label.style().polish(self.state_label)
        self.message_label.setText(message)

    def _update_disclosure(self) -> None:
        depth = InterpretationDepth(
            str(self.depth_combo.currentData() or InterpretationDepth.STANDARD.value)
        )
        depth_notice = INTERPRETATION_DEPTH_DESCRIPTIONS[depth]
        profile = self.profiles.profile(str(self.profile_combo.currentData() or ""))
        if profile is None:
            self.disclosure_label.setText(
                "生成时会发送受限的结构化报告数据和日志片段；不会上传 APK、二进制 trace "
                f"或完整日志。{depth_notice} AI 生成，仅供参考。"
            )
            return
        host = urlsplit(profile.base_url).hostname or profile.base_url
        transport_notice = (
            "当前使用 HTTP，API Key 和报告数据不会被传输层加密。"
            if urlsplit(profile.base_url).scheme.casefold() == "http"
            else ""
        )
        self.disclosure_label.setText(
            f"生成时会向 {host} 发送受限的结构化报告数据和日志片段；不会上传 APK、"
            f"二进制 trace 或完整日志。{depth_notice} AI 生成，仅供参考。{transport_notice}"
        )

    def _depth_changed(self) -> None:
        value = str(self.depth_combo.currentData() or InterpretationDepth.STANDARD.value)
        self.profiles.interpretation_depth = InterpretationDepth(value)
        self._update_disclosure()

    def _set_depth(self, depth: InterpretationDepth) -> None:
        index = self.depth_combo.findData(depth.value)
        if index < 0 or index == self.depth_combo.currentIndex():
            return
        self.depth_combo.blockSignals(True)
        self.depth_combo.setCurrentIndex(index)
        self.depth_combo.blockSignals(False)


def _default_message(state: InterpretationState) -> str:
    return {
        InterpretationState.NOT_GENERATED: "尚未生成 LLM 解读。",
        InterpretationState.QUEUED: "任务已进入后台队列。",
        InterpretationState.RUNNING: "正在等待 LLM 返回解读结果，可继续使用其他页面。",
        InterpretationState.COMPLETED: "LLM 解读已保存，可单独查看或从原始报告跳转。",
        InterpretationState.FAILED: "上次 LLM 解读失败，可检查配置后重试。",
        InterpretationState.CANCELLED: "上次 LLM 解读已取消。",
        InterpretationState.INTERRUPTED: "应用上次退出时任务尚未完成，请重新生成。",
    }[state]


__all__ = ["LlmInterpretationPanel"]
