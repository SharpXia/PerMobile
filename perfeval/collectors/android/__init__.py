"""Android host collector primitives."""

from .adb import (
    AdbClient,
    AndroidDevice,
    parse_adb_devices,
    parse_launcher_activity_component,
    query_launcher_activities_command,
)
from .collector import index_android_artifacts, parse_android_artifacts
from .flow import InteractionCommand, build_interaction_commands, find_launch_activity, parse_flow_steps
from .gfxinfo import (
    parse_focused_window,
    parse_framestats_detail,
    parse_framestats_detail_events,
    parse_gfxinfo,
    parse_gfxinfo_events,
)
from .logcat import parse_logcat, parse_logcat_events
from .meminfo import parse_meminfo, parse_meminfo_events
from .plan import AndroidRunCommandPlan, build_android_run_plan
from .perfetto_trace import (
    FrameRecord,
    FrameTimelineResult,
    PerfettoTraceProcessor,
    TraceProcessorResolution,
    TraceProcessorUnavailable,
    query_frame_timeline,
    resolve_trace_processor,
)
from .preflight import AndroidPreflightResult, preflight_from_devices, run_preflight, select_device
from .runner import AndroidCollectionResult, AndroidCommandResult, AndroidRunExecutor, collect_android_run
from .samples import SamplesWriter, parse_samples_events, read_samples
from .samplers import build_samplers

__all__ = [
    "AdbClient",
    "AndroidCollectionResult",
    "AndroidCommandResult",
    "AndroidDevice",
    "AndroidPreflightResult",
    "AndroidRunExecutor",
    "AndroidRunCommandPlan",
    "FrameRecord",
    "FrameTimelineResult",
    "InteractionCommand",
    "PerfettoTraceProcessor",
    "SamplesWriter",
    "TraceProcessorResolution",
    "TraceProcessorUnavailable",
    "build_android_run_plan",
    "build_interaction_commands",
    "build_samplers",
    "collect_android_run",
    "find_launch_activity",
    "index_android_artifacts",
    "parse_adb_devices",
    "parse_launcher_activity_component",
    "parse_android_artifacts",
    "parse_flow_steps",
    "parse_focused_window",
    "parse_framestats_detail",
    "parse_framestats_detail_events",
    "parse_gfxinfo",
    "parse_gfxinfo_events",
    "parse_logcat",
    "parse_logcat_events",
    "parse_meminfo",
    "parse_meminfo_events",
    "parse_samples_events",
    "preflight_from_devices",
    "query_frame_timeline",
    "query_launcher_activities_command",
    "read_samples",
    "resolve_trace_processor",
    "run_preflight",
    "select_device",
]
