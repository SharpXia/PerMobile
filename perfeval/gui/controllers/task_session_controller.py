from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
from uuid import uuid4

from PySide6.QtCore import QObject, Signal

from perfeval.gui.models.preset import TaskLaunchConfig
from perfeval.gui.models.result import TaskResult
from perfeval.gui.models.task import ManualRequest, TaskPhaseEvent, TaskSpec, TaskState

from .task_controller import TaskController
from ..services.experiment_cache import ExperimentCacheService


class TaskSessionController(QObject):
    launch_changed = Signal(object)
    phase_changed = Signal(object)
    phases_reset = Signal(object)
    result_changed = Signal(object)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        controller: TaskController | None = None,
        experiment_cache: ExperimentCacheService | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller or TaskController(self)
        self._experiment_cache = experiment_cache or ExperimentCacheService()
        self._launch: TaskLaunchConfig | None = None
        self._last_launch: TaskLaunchConfig | None = None
        self._last_result: TaskResult | None = None
        self._phases: OrderedDict[str, TaskPhaseEvent] = OrderedDict()
        self._phase_events_received = False

        self.controller.phase_changed.connect(self._on_phase)
        self.controller.finished.connect(self._on_finished)
        self.controller.state_changed.connect(self._on_state_changed)

    @property
    def launch(self) -> TaskLaunchConfig | None:
        return self._launch

    @property
    def last_launch(self) -> TaskLaunchConfig | None:
        return self._last_launch

    @property
    def result(self) -> TaskResult | None:
        return self._last_result

    @property
    def phases(self) -> tuple[TaskPhaseEvent, ...]:
        return tuple(self._phases.values())

    @property
    def phase_events_received(self) -> bool:
        return self._phase_events_received

    @property
    def state(self) -> TaskState:
        return self.controller.state

    @property
    def is_active(self) -> bool:
        return self.controller.is_active

    def start(self, launch: TaskLaunchConfig | TaskSpec) -> bool:
        if self.is_active:
            raise RuntimeError("已有任务正在运行")
        resolved = launch if isinstance(launch, TaskLaunchConfig) else TaskLaunchConfig.from_task(launch)
        self._launch = resolved
        self._last_launch = resolved
        self._last_result = None
        self._phase_events_received = False
        self._phases.clear()
        self.phases_reset.emit(resolved.phase_ids)
        self.launch_changed.emit(resolved)
        started = self.controller.start(resolved.task)
        if started:
            self._experiment_cache.begin(resolved)
        if not started and not self.controller.is_active:
            self._launch = None
        return started

    def start_task(self, task: TaskSpec) -> bool:
        return self.start(task)

    def repeat_last(self) -> bool:
        if self._last_launch is None:
            return False
        launch = self._experiment_cache.fresh_launch(self._last_launch)
        task = replace(launch.task, task_id=uuid4().hex)
        return self.start(replace(launch, task=task))

    def cancel(self) -> bool:
        return self.controller.cancel()

    def continue_manual(self) -> bool:
        return self.controller.continue_manual()

    def reset(self) -> None:
        self.controller.reset()
        self._launch = None
        self._last_result = None
        self._phase_events_received = False
        self._phases.clear()
        self.phases_reset.emit(())

    def shutdown(self) -> bool:
        return self.controller.shutdown()

    def _on_phase(self, event: TaskPhaseEvent) -> None:
        self._phase_events_received = True
        key = f"{event.variant_id or ''}:{event.repeat_index or 0}:{event.phase_id}"
        self._phases[key] = event
        self.phase_changed.emit(event)

    def _on_state_changed(self, state: TaskState) -> None:
        if state is not TaskState.STARTING or self._launch is not None:
            return
        task = self.controller.current_task
        if task is None:
            return
        resolved = TaskLaunchConfig.from_task(task)
        self._launch = resolved
        self._last_launch = resolved
        self._last_result = None
        self._phase_events_received = False
        self._phases.clear()
        self.phases_reset.emit(resolved.phase_ids)
        self.launch_changed.emit(resolved)

    def _on_finished(self, result: TaskResult) -> None:
        self._last_result = result
        if self._last_launch is not None:
            self._experiment_cache.finish(self._last_launch, result)
        self._launch = None
        self.result_changed.emit(result)


__all__ = ["TaskSessionController"]
