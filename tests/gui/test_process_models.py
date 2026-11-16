from __future__ import annotations

import pytest

from perfeval.gui.models.result import TaskOutcome, classify_outcome
from perfeval.gui.models.task import InvalidTaskTransition, TaskState, TaskStateMachine


def test_process_state_machine_enforces_lifecycle_and_manual_cancel() -> None:
    machine = TaskStateMachine()
    for state in (
        TaskState.VALIDATING,
        TaskState.READY,
        TaskState.STARTING,
        TaskState.RUNNING,
        TaskState.WAITING_FOR_USER,
        TaskState.CANCELLING,
        TaskState.COMPLETED,
        TaskState.IDLE,
    ):
        assert machine.transition(state) == state

    with pytest.raises(InvalidTaskTransition):
        machine.transition(TaskState.RUNNING)


@pytest.mark.parametrize(
    ("exit_code", "status", "kwargs", "expected"),
    [
        (0, None, {}, TaskOutcome.COMPLETED),
        (0, "pass", {}, TaskOutcome.PASS),
        (0, "warn", {}, TaskOutcome.WARN),
        (0, "unknown", {}, TaskOutcome.UNKNOWN),
        (0, "partial", {}, TaskOutcome.PARTIAL),
        (1, "warn", {}, TaskOutcome.FAIL),
        (4, "pass", {}, TaskOutcome.INVALID),
        (10, None, {}, TaskOutcome.TOOL_ERROR),
        (130, None, {}, TaskOutcome.CANCELLED),
        (15, None, {"user_cancelled": True}, TaskOutcome.CANCELLED),
        (None, None, {"launch_error": True}, TaskOutcome.LAUNCH_ERROR),
        (9, None, {}, TaskOutcome.UNEXPECTED_ERROR),
    ],
)
def test_process_exit_and_status_mapping(exit_code, status, kwargs, expected) -> None:
    assert classify_outcome(exit_code, structured_status=status, **kwargs) == expected

