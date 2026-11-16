from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .adb import (
    AdbClient,
    clear_data_command,
    force_stop_command,
    install_command,
    launch_command,
    query_launcher_activities_command,
)
from .flow import InteractionCommand

# screenrecord 单次上限 180s（系统限制），默认 time-limit 上限预留 5s 余量（SCREEN_RECORD_SYNC §1）
SCREENRECORD_TIME_LIMIT_MAX_S = 175
# flow 预计窗口超过该值时提前分段录制（SCREEN_RECORD_SYNC §1：预计超过 170s 分段）
SCREENRECORD_SEGMENT_THRESHOLD_S = 170
# 首段 time-limit 在估计窗口基础上预留的启动/收尾余量（min(估计窗口 + 10s, 175s)）
SCREENRECORD_WINDOW_MARGIN_S = 10
# 默认码率（SCREEN_RECORD_SYNC §3：bitrateMbps: 8）
SCREENRECORD_DEFAULT_BITRATE_MBPS = 8.0
# 录屏有效帧数据最小字节数：pull 结果 <1KB 判定"无有效帧数据"（真机 smoke：
# 部分 ROM 限制 shell screenrecord，启动无报错但产出 0 字节文件）
SCREENRECORD_MIN_VALID_BYTES = 1024


@dataclass(frozen=True)
class ScreenRecordPlan:
    """screenrecord 三段命令计划（SCREEN_RECORD_SYNC §5：start/stop/pull）。

    start_commands / pull_commands 与 remote_paths / local_paths 按段一一对应
    （交互窗口超过单段 180s 限制时按估计窗口分段，多余段为 best-effort：
    flow 提前结束时对应文件不存在，pull 失败被容忍）。

    extendable=True（含 manual 步骤且未设 maxDurationMs）表示段数不可预估：
    计划只预生成首段，runner 侧 watchdog 在段到时后调用 segment_commands()
    动态生成续段，直到 collect 前 stop 为止。
    """

    start_commands: tuple[tuple[str, ...], ...]
    stop_command: tuple[str, ...]
    pull_commands: tuple[tuple[str, ...], ...]
    remote_paths: tuple[str, ...]
    local_paths: tuple[str, ...]
    time_limit_s: int
    estimated_window_s: float
    segmented: bool
    extendable: bool = False
    # 动态续段（segment_commands）所需的构造参数
    shell_prefix: tuple[str, ...] = ()
    bitrate_mbps: float = SCREENRECORD_DEFAULT_BITRATE_MBPS
    size: str | None = None
    run_id: str = "run"
    artifact_dir: str = "artifacts/android"

    @property
    def segment_count(self) -> int:
        return len(self.start_commands)

    def segment_commands(
        self,
        index: int,
    ) -> tuple[tuple[str, ...], tuple[str, ...], str, str] | None:
        """第 index 段（0-based）的 (start, pull, remote, local) 命令组。

        预算内的段取预生成命令；extendable 计划按同一命名规则动态生成续段
        （runner watchdog 中途 rollover 使用）；超出录制预算返回 None。
        """
        if 0 <= index < len(self.start_commands):
            return (
                self.start_commands[index],
                self.pull_commands[index],
                self.remote_paths[index],
                self.local_paths[index],
            )
        if index < 0 or not self.extendable:
            return None
        suffix = f"-seg{index + 1:03d}"
        remote_path = f"/sdcard/perfeval-{self.run_id}{suffix}.mp4"
        local_path = str(Path(self.artifact_dir) / f"screen{suffix}.mp4")
        script = _screen_record_start_script(
            time_limit_s=self.time_limit_s,
            remote_path=remote_path,
            bitrate_mbps=self.bitrate_mbps,
            size=self.size,
        )
        start_command = (*self.shell_prefix, script)
        # shell_prefix 形如 (adb, [-s serial], "shell")；pull 复用同一前缀
        pull_command = (*self.shell_prefix[:-1], "pull", remote_path, local_path)
        return start_command, pull_command, remote_path, local_path


@dataclass(frozen=True)
class AndroidRunCommandPlan:
    install: list[str] | None
    resolve_activity: list[str] | None
    force_stop: list[str] | None
    clear_data: list[str] | None
    clear_logcat: list[str]
    reset_gfxinfo: list[str]
    launch: list[str] | None
    collect_window: list[str]
    collect_gfxinfo: list[str]
    collect_framestats: list[str]
    collect_meminfo: list[str]
    collect_logcat: list[str]
    perfetto: list[str] | None = None
    perfetto_remote_path: str | None = None
    perfetto_pull: list[str] | None = None
    screenrecord: ScreenRecordPlan | None = None
    interaction_commands: tuple[InteractionCommand, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


def build_android_run_plan(
    *,
    package_name: str,
    activity: str | None = None,
    apk_path: str | None = None,
    serial: str | None = None,
    clear_data: bool = True,
    reinstall: bool = True,
    auto_resolve_activity: bool = False,
    perfetto_enabled: bool = False,
    perfetto_duration_ms: int = 20_000,
    perfetto_remote_path: str = "/data/misc/perfetto-traces/perfeval.pftrace",
    perfetto_local_path: str | None = None,
    screen_record_config: Mapping[str, Any] | None = None,
    screen_record_run_id: str | None = None,
    screen_record_artifact_dir: str | Path | None = None,
    post_launch_wait_s: float = 0.0,
    interaction_commands: list[InteractionCommand] | None = None,
    adb: AdbClient | None = None,
) -> AndroidRunCommandPlan:
    client = adb or AdbClient()
    notes: list[str] = []
    resolve_activity = None
    if activity:
        component = activity if "/" in activity else f"{package_name}/{activity}"
        launch = launch_command(client, component, serial=serial)
    elif auto_resolve_activity:
        launch = None
        resolve_activity = query_launcher_activities_command(
            client,
            package_name,
            serial=serial,
        )
        notes.append("launch.activity.auto_resolve")
    else:
        launch = None
        notes.append("launch.skipped.no_activity")
    launch_requested = bool(activity) or auto_resolve_activity
    install = install_command(client, apk_path, serial=serial) if apk_path and reinstall else None
    if apk_path and not reinstall:
        notes.append("install.skipped.reinstall_false")
    if not apk_path:
        notes.append("install.skipped.no_apk")
    perfetto = None
    if perfetto_enabled:
        duration_s = max(1, int(round(perfetto_duration_ms / 1000)))
        perfetto = client.command(
            "shell",
            "perfetto",
            "--background-wait",
            "-o",
            perfetto_remote_path,
            "-t",
            f"{duration_s}s",
            "--app",
            package_name,
            "sched",
            "freq",
            "idle",
            "am",
            "wm",
            "gfx",
            "view",
            "binder_driver",
            "dalvik",
            serial=serial,
        )
    perfetto_pull = None
    if perfetto_enabled and perfetto_local_path:
        perfetto_pull = client.command("pull", perfetto_remote_path, perfetto_local_path, serial=serial)
    screenrecord = build_screen_record_plan(
        adb=client,
        run_id=screen_record_run_id or "run",
        config=screen_record_config,
        interaction_commands=interaction_commands,
        post_launch_wait_s=post_launch_wait_s,
        artifact_dir=screen_record_artifact_dir or "artifacts/android",
        serial=serial,
    )
    return AndroidRunCommandPlan(
        install=install,
        resolve_activity=resolve_activity,
        force_stop=(
            force_stop_command(client, package_name, serial=serial)
            if launch_requested
            else None
        ),
        clear_data=(
            clear_data_command(client, package_name, serial=serial)
            if clear_data and launch_requested
            else None
        ),
        clear_logcat=client.command("logcat", "-c", serial=serial),
        reset_gfxinfo=client.command("shell", "dumpsys", "gfxinfo", package_name, "reset", serial=serial),
        launch=launch,
        collect_window=client.command("shell", "dumpsys", "window", serial=serial),
        collect_gfxinfo=client.command("shell", "dumpsys", "gfxinfo", package_name, serial=serial),
        collect_framestats=client.command("shell", "dumpsys", "gfxinfo", package_name, "framestats", serial=serial),
        collect_meminfo=client.command("shell", "dumpsys", "meminfo", package_name, serial=serial),
        collect_logcat=client.command("logcat", "-d", "-v", "epoch", serial=serial),
        perfetto=perfetto,
        perfetto_remote_path=perfetto_remote_path if perfetto_enabled else None,
        perfetto_pull=perfetto_pull,
        screenrecord=screenrecord,
        interaction_commands=tuple(interaction_commands) if interaction_commands else (),
        notes=tuple(notes),
    )


def _screen_record_start_script(
    *,
    time_limit_s: int,
    remote_path: str,
    bitrate_mbps: float,
    size: str | None,
) -> str:
    """设备侧后台启动脚本（SCREEN_RECORD_SYNC §1）。

    `nohup ... >/dev/null 2>&1 &`：adb shell 立即返回不阻塞 runner，nohup 免疫
    shell 退出时的 SIGHUP；`--bugreport` 叠加时间戳水印提供独立交叉校验。
    """
    parts = [
        "nohup",
        "screenrecord",
        "--bugreport",
        "--time-limit",
        str(time_limit_s),
    ]
    if bitrate_mbps > 0:
        parts.extend(["--bit-rate", str(int(round(bitrate_mbps * 1_000_000)))])
    if size:
        parts.extend(["--size", size])
    parts.append(remote_path)
    parts.append(">/dev/null")
    parts.append("2>&1")
    parts.append("&")
    return " ".join(parts)


def estimate_interaction_window_s(
    interaction_commands: list[InteractionCommand] | None,
    *,
    post_launch_wait_s: float = 0.0,
) -> float:
    """估计 flow 交互窗口（秒）：launch 后等待 + 各步骤等待/手动超时之和。

    手动步骤按其最长等待（timeout_s）计入；无显式等待的 adb 操作按 0 计入
    （screenrecord time-limit 已有 10s 余量兜底）。
    """
    total = max(0.0, float(post_launch_wait_s))
    for cmd in interaction_commands or ():
        if cmd.is_manual and cmd.timeout_s is not None:
            total += max(0.0, float(cmd.timeout_s))
        else:
            total += max(0.0, float(cmd.wait_s))
    return total


def build_screen_record_plan(
    *,
    adb: AdbClient,
    run_id: str,
    config: Mapping[str, Any] | None,
    interaction_commands: list[InteractionCommand] | None,
    post_launch_wait_s: float,
    artifact_dir: str | Path,
    serial: str | None = None,
) -> ScreenRecordPlan | None:
    """按归并后的 screenRecord 配置（resolve_screen_record_config 产出）构造三段命令
    计划；未启用返回 None。

    分段策略（ux/feedback-fixes fix-3）：
    - 纯自动步骤：估计窗口 + 余量 <= 175s 单段；否则按 175s/段预分段（每段自然
      到时后由 runner 起下一段）。人工步骤不存在时窗口可预估，est 逻辑保留。
    - 含 manual 步骤：人工时长不可预估，time-limit 直接用单段上限 175s（不再按
      timeoutMs 估算——估算偏小时录屏会在人工步骤中段提前自然到时），collect 前
      的 pkill -INT 负责提前正常收尾；超过 175s 的超长窗口由 runner 侧 watchdog
      中途 rollover 覆盖。未设 maxDurationMs 时段数不可预估（extendable，动态续
      段）；设了 maxDurationMs 时按录制预算预分段、预算封顶。
    """
    resolved = config if isinstance(config, Mapping) else None
    if resolved is None or resolved.get("enabled") is not True:
        return None
    bitrate_mbps = resolved.get("bitrateMbps")
    bitrate_mbps = (
        float(bitrate_mbps)
        if isinstance(bitrate_mbps, (int, float)) and not isinstance(bitrate_mbps, bool)
        else SCREENRECORD_DEFAULT_BITRATE_MBPS
    )
    size = resolved.get("size")
    size = size.strip() if isinstance(size, str) and size.strip() else None
    max_duration_ms = resolved.get("maxDurationMs")
    max_duration_ms = (
        int(max_duration_ms)
        if isinstance(max_duration_ms, int) and not isinstance(max_duration_ms, bool) and max_duration_ms > 0
        else None
    )
    estimated_window_s = estimate_interaction_window_s(
        interaction_commands,
        post_launch_wait_s=post_launch_wait_s,
    )
    if max_duration_ms is not None:
        estimated_window_s = min(estimated_window_s, max_duration_ms / 1000.0)
    has_manual = any(cmd.is_manual for cmd in interaction_commands or ())
    if has_manual:
        extendable = max_duration_ms is None
        if max_duration_ms is not None:
            # 明确录制预算：预算同时封顶单段 time-limit 与段数（175s 粒度）
            budget_s = max(1.0, max_duration_ms / 1000.0)
            time_limit_s = max(
                1,
                min(SCREENRECORD_TIME_LIMIT_MAX_S, int(math.ceil(budget_s))),
            )
            segment_count = max(1, int(math.ceil(budget_s / SCREENRECORD_TIME_LIMIT_MAX_S)))
        else:
            # 人工时长不可预估：直接用单段上限，超长窗口由 watchdog 动态续段
            time_limit_s = SCREENRECORD_TIME_LIMIT_MAX_S
            segment_count = 1
    else:
        time_limit_s = min(
            int(round(estimated_window_s)) + SCREENRECORD_WINDOW_MARGIN_S,
            SCREENRECORD_TIME_LIMIT_MAX_S,
        )
        time_limit_s = max(1, time_limit_s)
        extendable = False
        if estimated_window_s > SCREENRECORD_SEGMENT_THRESHOLD_S:
            segment_count = int(math.ceil(estimated_window_s / SCREENRECORD_TIME_LIMIT_MAX_S))
        else:
            segment_count = 1
    start_commands: list[tuple[str, ...]] = []
    pull_commands: list[tuple[str, ...]] = []
    remote_paths: list[str] = []
    local_paths: list[str] = []
    for index in range(1, segment_count + 1):
        suffix = "" if index == 1 else f"-seg{index:03d}"
        remote_path = f"/sdcard/perfeval-{run_id}{suffix}.mp4"
        local_path = str(Path(artifact_dir) / f"screen{suffix}.mp4")
        remote_paths.append(remote_path)
        local_paths.append(local_path)
        start_commands.append(
            tuple(
                adb.command(
                    "shell",
                    _screen_record_start_script(
                        time_limit_s=time_limit_s,
                        remote_path=remote_path,
                        bitrate_mbps=bitrate_mbps,
                        size=size,
                    ),
                    serial=serial,
                )
            )
        )
        pull_commands.append(tuple(adb.command("pull", remote_path, local_path, serial=serial)))
    shell_prefix = start_commands[0][: start_commands[0].index("shell") + 1]
    return ScreenRecordPlan(
        start_commands=tuple(start_commands),
        stop_command=tuple(adb.command("shell", "pkill -INT screenrecord", serial=serial)),
        pull_commands=tuple(pull_commands),
        remote_paths=tuple(remote_paths),
        local_paths=tuple(local_paths),
        time_limit_s=time_limit_s,
        estimated_window_s=estimated_window_s,
        segmented=segment_count > 1,
        extendable=extendable,
        shell_prefix=shell_prefix,
        bitrate_mbps=bitrate_mbps,
        size=size,
        run_id=run_id,
        artifact_dir=str(artifact_dir),
    )


def _normalize_screen_record_entry(config: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """归并 scenario YAML `collectors.android.screenRecord`（默认关闭，§3）。"""
    if config is None or not isinstance(config, Mapping):
        return None
    collectors = config.get("collectors")
    android = collectors.get("android") if isinstance(collectors, Mapping) else None
    entry = android.get("screenRecord") if isinstance(android, Mapping) else None
    if entry is False or entry is None:
        return None
    if isinstance(entry, Mapping):
        if entry.get("enabled") is not True:
            return None
        bitrate = entry.get("bitrateMbps", SCREENRECORD_DEFAULT_BITRATE_MBPS)
        bitrate = float(bitrate) if isinstance(bitrate, (int, float)) and not isinstance(bitrate, bool) else SCREENRECORD_DEFAULT_BITRATE_MBPS
        size = entry.get("size")
        if not isinstance(size, str) or not size.strip():
            size_px = entry.get("sizePx")
            if isinstance(size_px, int) and not isinstance(size_px, bool) and size_px > 0:
                size = f"{size_px}x{size_px}"
            else:
                size = None
        else:
            size = size.strip()
        max_duration_ms = entry.get("maxDurationMs")
        if isinstance(max_duration_ms, int) and not isinstance(max_duration_ms, bool) and max_duration_ms > 0:
            max_duration_ms = int(max_duration_ms)
        else:
            max_duration_ms = None
    else:
        # `screenRecord: true` 简写 = 全默认开启
        bitrate = SCREENRECORD_DEFAULT_BITRATE_MBPS
        size = None
        max_duration_ms = None
    return {
        "enabled": True,
        "bitrateMbps": max(0.0, bitrate),
        "size": size,
        "maxDurationMs": max_duration_ms,
    }


def resolve_screen_record_config(
    config: Mapping[str, Any] | None,
    *,
    screen_record: bool = False,
    screen_record_off: bool = False,
) -> dict[str, Any] | None:
    """归并 scenario YAML `collectors.android.screenRecord` 与 CLI 覆盖参数。

    --screen-record-off 直接关闭（优先级最高）；--screen-record 显式开启
    （YAML 未配置时按默认参数启用）；默认关闭（SCREEN_RECORD_SYNC §3）。
    返回 None 表示不启用录屏。
    """
    if screen_record_off:
        return None
    resolved = _normalize_screen_record_entry(config)
    if resolved is not None:
        return resolved
    if screen_record:
        return {
            "enabled": True,
            "bitrateMbps": SCREENRECORD_DEFAULT_BITRATE_MBPS,
            "size": None,
            "maxDurationMs": None,
        }
    return None


def resolve_sampling_config(
    config: Mapping[str, Any] | None,
    *,
    sampling_off: bool = False,
    sampling_interval_ms: int | None = None,
) -> dict[str, Any]:
    """归并 scenario YAML `collectors.android.samplers` 与 CLI 覆盖参数。

    --sampling-off 直接关闭；--sampling-interval-ms 覆盖每个采样器的 intervalMs。
    """
    if sampling_off:
        return {}
    collectors = config.get("collectors") if isinstance(config, Mapping) else None
    android = collectors.get("android") if isinstance(collectors, Mapping) else None
    samplers = android.get("samplers") if isinstance(android, Mapping) else None
    resolved: dict[str, Any] = {}
    if isinstance(samplers, Mapping):
        for name, entry in samplers.items():
            resolved[str(name)] = dict(entry) if isinstance(entry, Mapping) else entry
    if sampling_interval_ms is not None and sampling_interval_ms > 0:
        for name, entry in resolved.items():
            if entry is False:
                continue
            if isinstance(entry, Mapping):
                entry["intervalMs"] = int(sampling_interval_ms)
            else:
                resolved[name] = {"enabled": entry if entry is not None else True, "intervalMs": int(sampling_interval_ms)}
    return resolved
