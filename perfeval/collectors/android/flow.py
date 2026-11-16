from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .adb import AdbClient


@dataclass(frozen=True)
class InteractionCommand:
    """A single command to execute during the interaction phase of a run."""

    name: str
    command: tuple[str, ...] = ()
    output_name: str | None = None
    wait_s: float = 0.0
    is_manual: bool = False
    prompt: str = ""
    timeout_s: float | None = None
    timeout_message: str = ""


def parse_flow_steps(flow_config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Parse flow steps from a scenario flow config.

    Returns a list of normalised step dicts.  Each dict has at least a ``type``
    key.  Supported types: ``launch``, ``idle``, ``manual``, ``reset_gfxinfo``,
    ``swipe``.
    """
    if not flow_config:
        return []
    raw_steps = flow_config.get("steps")
    if not isinstance(raw_steps, list):
        return []
    steps: list[dict[str, Any]] = []
    for raw in raw_steps:
        if not isinstance(raw, dict):
            continue
        step_type = raw.get("type")
        if not isinstance(step_type, str):
            continue
        steps.append(dict(raw))
    return steps


def find_launch_activity(steps: list[dict[str, Any]]) -> str | None:
    """Find the activity to launch from flow steps.

    Returns the activity string (e.g. ``.MainActivity``) or ``None`` when no
    ``launch`` step is present.
    """
    for step in steps:
        if step.get("type") == "launch" and isinstance(step.get("activity"), str):
            return step["activity"]
    return None


def build_interaction_commands(
    steps: list[dict[str, Any]],
    *,
    adb: AdbClient,
    package_name: str,
    serial: str | None = None,
) -> list[InteractionCommand]:
    """Convert parsed flow steps into executable interaction commands.

    ``launch`` steps are skipped (handled by the plan builder).  All other
    step types produce one or more :class:`InteractionCommand` instances.
    """
    commands: list[InteractionCommand] = []
    for step in steps:
        step_type = step.get("type")
        if step_type == "launch":
            continue
        elif step_type == "idle":
            duration_ms = step.get("durationMs", 0)
            commands.append(
                InteractionCommand(
                    name="idle",
                    wait_s=float(duration_ms) / 1000.0,
                )
            )
        elif step_type == "manual":
            prompt = step.get("prompt", "Press Enter to start collection")
            timeout_s = None
            timeout_ms = step.get("timeoutMs")
            if isinstance(timeout_ms, (int, float)) and timeout_ms > 0:
                timeout_s = float(timeout_ms) / 1000.0
            commands.append(
                InteractionCommand(
                    name="manual",
                    is_manual=True,
                    prompt=str(prompt),
                    timeout_s=timeout_s,
                )
            )
        elif step_type == "reset_gfxinfo":
            commands.append(
                InteractionCommand(
                    name="reset_gfxinfo",
                    command=tuple(
                        adb.command(
                            "shell", "dumpsys", "gfxinfo", package_name, "reset", serial=serial
                        )
                    ),
                )
            )
        elif step_type == "swipe":
            start_x = int(step.get("startX", 540))
            start_y = int(step.get("startY", 1600))
            end_x = int(step.get("endX", 540))
            end_y = int(step.get("endY", 400))
            duration_ms = int(step.get("durationMs", 300))
            repeat = max(1, int(step.get("repeat", 1)))
            interval_ms = int(step.get("intervalMs", 500))
            for index in range(repeat):
                swipe_cmd = adb.command(
                    "shell",
                    "input",
                    "swipe",
                    str(start_x),
                    str(start_y),
                    str(end_x),
                    str(end_y),
                    str(duration_ms),
                    serial=serial,
                )
                commands.append(
                    InteractionCommand(
                        name=f"swipe_{index + 1}",
                        command=tuple(swipe_cmd),
                        wait_s=float(interval_ms) / 1000.0,
                    )
                )
    return commands
