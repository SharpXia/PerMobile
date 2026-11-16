from __future__ import annotations

import time

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
)
from PySide6.QtGui import QPainter, QPalette
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QLabel,
    QVBoxLayout,
    QWidget,
)


class ActivityIndicator(QWidget):
    """macOS 风格的旋转活动指示器（渐隐辐条）。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        diameter: int = 28,
        line_count: int = 8,
        interval_ms: int = 80,
    ) -> None:
        super().__init__(parent)
        self._line_count = line_count
        self._current = 0
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._advance)
        self.setFixedSize(diameter, diameter)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    @property
    def is_spinning(self) -> bool:
        return self._timer.isActive()

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._timer.stop()
        super().hideEvent(event)

    def _advance(self) -> None:
        self._current = (self._current + 1) % self._line_count
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        color = self.palette().color(QPalette.ColorRole.WindowText)
        side = min(self.width(), self.height())
        outer = side / 2.0
        line_length = outer * 0.42
        line_width = max(2.0, side / 11.0)
        painter.translate(self.width() / 2.0, self.height() / 2.0)
        for index in range(self._line_count):
            # 距当前辐条越远越淡，形成追尾旋转效果。
            distance = (index - self._current) % self._line_count
            alpha = 60 + int(195 * (1.0 - distance / self._line_count))
            color.setAlpha(alpha)
            painter.setBrush(color)
            painter.save()
            painter.rotate(360.0 / self._line_count * index)
            painter.drawRoundedRect(
                QRectF(-line_width / 2.0, -outer, line_width, line_length),
                line_width / 2.0,
                line_width / 2.0,
            )
            painter.restore()


class LoadingOverlay(QWidget):
    """覆盖在内容区上方的半透明加载层，淡入淡出避免生硬切换。

    显示后至少保持 _MIN_VISIBLE_MS，避免短任务导致遮罩一闪而过。
    """

    _FADE_MS = 180
    _MIN_VISIBLE_MS = 350

    def __init__(self, parent: QWidget, text: str = "正在加载…") -> None:
        super().__init__(parent)
        self.setObjectName("loadingOverlay")
        self._active = False
        self._shown_at = 0.0

        card = QFrame(self)
        card.setObjectName("loadingCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(28, 22, 28, 22)
        card_layout.setSpacing(12)
        self.indicator = ActivityIndicator(card)
        card_layout.addWidget(self.indicator, 0, Qt.AlignmentFlag.AlignHCenter)
        self.label = QLabel(text, card)
        self.label.setObjectName("loadingText")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(self.label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(card, 0, Qt.AlignmentFlag.AlignCenter)

        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(0.0)
        self.setGraphicsEffect(self._effect)
        self._animation = QPropertyAnimation(self._effect, b"opacity", self)
        self._animation.setDuration(self._FADE_MS)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._animation.finished.connect(self._on_fade_finished)

        parent.installEventFilter(self)
        self.setGeometry(parent.rect())
        self.hide()

    @property
    def is_active(self) -> bool:
        return self._active

    def set_text(self, text: str) -> None:
        self.label.setText(text)

    def start(self) -> None:
        if self._active:
            return
        self._active = True
        self._shown_at = time.monotonic()
        parent = self.parentWidget()
        if parent is not None:
            self.setGeometry(parent.rect())
        self.show()
        self.raise_()
        self.indicator.start()
        self._fade_to(1.0)

    def stop(self) -> None:
        if not self._active:
            return
        self._active = False
        elapsed_ms = (time.monotonic() - self._shown_at) * 1000.0
        remaining = self._MIN_VISIBLE_MS - elapsed_ms
        if remaining > 0:
            QTimer.singleShot(int(remaining), self._fade_out_if_inactive)
        else:
            self._fade_to(0.0)

    def _fade_out_if_inactive(self) -> None:
        if not self._active:
            self._fade_to(0.0)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if watched is self.parentWidget() and event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.Show,
        ):
            self.setGeometry(self.parentWidget().rect())
        return super().eventFilter(watched, event)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        backdrop = self.palette().color(QPalette.ColorRole.Window)
        backdrop.setAlpha(150)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(backdrop)
        painter.drawRoundedRect(self.rect(), 10, 10)

    def _fade_to(self, opacity: float) -> None:
        self._animation.stop()
        self._animation.setStartValue(self._effect.opacity())
        self._animation.setEndValue(opacity)
        self._animation.start()

    def _on_fade_finished(self) -> None:
        if not self._active and self._effect.opacity() <= 0.01:
            self.hide()
            self.indicator.stop()
