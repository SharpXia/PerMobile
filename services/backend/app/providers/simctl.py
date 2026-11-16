"""simctl 客户端（iOS Simulator，M-iOS 契约 §23）。

与 ``providers/real.py`` 的分工：real 走 perfeval adb；本模块只依赖 macOS 自带的
``xcrun simctl``（无第三方依赖）。命令执行面统一经 ``run_simctl``（测试可注入
假 runner，fixture 驱动单测不依赖模拟器常驻——与 m4_fixtures 的 command_runner
注入接缝同哲学）。

解析函数全部为纯函数（fixture 原文 → 结构），单测以 tests/fixtures/simulator/
下的真机（模拟器）原文做 golden：

- ``parse_devices_json``：``simctl list devices --json`` → 契约 §4 设备行
  （platform="ios"，connection="simulator"，state Booted→online 否则 offline）；
- ``parse_listapps``：``simctl listapps``（NeXT 风格 plist，非 XML——plistlib
  不适用，容忍手工状态机解析）→ 应用行；
- ``parse_launchctl_uikit``：``simctl spawn booted launchctl list`` 的
  ``UIKitApplication:<bundle>[hex]`` 行 → {bundle: pid}（运行中应用表）；
- ``parse_visibility_events`` / ``foreground_from_visibility``：SpringBoard
  统一日志（ndjson）``[app<bundle>:pid] Setting process visibility to: …`` →
  前台应用 best-effort（无信号置 None，不猜——契约 §4「探测失败置 null」）；
- ``parse_ps_stat``：宿主 ``ps -o pid,pcpu,rss,comm -p <pid>`` 单行 →
  (pcpu, rss_kb)（模拟器宿主口径采样数据源，§23.2）。

诚实性口径（§23）：CPU/内存为**模拟器宿主口径**（iOS 应用进程在本机以宿主进程
运行，ps 的是宿主进程），数值与真机 iOS（Xcode/PerfDog 口径）不可直接对比；
FPS 无逐帧公开 API，不出点不造数（§23.2 note 声明）。
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable

# xcrun 缺失/不可执行（非 macOS 或命令行工具未装）→ 503 simctl_unavailable（§3 §23）
SIMCTL_UNAVAILABLE = "simctl_unavailable"


def run_simctl(
    *args: str,
    timeout: float = 15.0,
    runner: Callable[[list[str], float], Any] | None = None,
) -> subprocess.CompletedProcess[str] | None:
    """``xcrun simctl <args>`` 文本执行；命令失败/超时返回 None（不猜）。

    ``runner`` 注入面：测试用假 runner 驱动解析逻辑（无需模拟器）；生产缺省
    subprocess.run。xcrun 本体缺失（FileNotFoundError）由调用方按
    ``simctl_unavailable`` 归一——本函数仅在 runner 注入时透传异常，
    生产路径捕获 OSError 返回 None（设备枚举场景避免 500）。
    """
    command = ["xcrun", "simctl", *args]
    if runner is not None:
        return runner(command, timeout)
    try:
        return subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


# ---- 设备枚举（契约 §4 + §23.1）----

_RUNTIME_KEY_RE = re.compile(r"^com\.apple\.CoreSimulator\.SimRuntime\.iOS-(\d+)-(\d+)$")


def parse_devices_json(
    stdout: str, *, host_cores: int | None = None
) -> list[dict[str, Any]]:
    """``simctl list devices --json`` → 契约 §4 设备行列表（iOS 运行时 only）。

    - watchOS/tvOS 行按 runtime key 过滤掉（只服务 iOS Simulator）；
    - ``state``：Booted→online，其余（Shutdown 等）→offline；
    - ``info``：os="iOS <ver> (Simulator)"、cpu 为宿主核数（模拟器应用进程跑在
      宿主 CPU 上，宿主口径如实声明）、resolution/batteryLevel/refreshRateHz
      探测不到一律 null（不猜——模拟器无公开读数，§23.1）。
    """
    try:
        body = json.loads(stdout)
    except (TypeError, ValueError):
        return []
    devices: list[dict[str, Any]] = []
    for runtime_key, rows in (body.get("devices") or {}).items():
        match = _RUNTIME_KEY_RE.match(runtime_key)
        if match is None:
            continue
        runtime_label = f"iOS {match.group(1)}.{match.group(2)}"
        for row in rows or []:
            if not row.get("isAvailable", True):
                continue  # 不可用运行时/设备不出行（不伪造）
            state = "online" if row.get("state") == "Booted" else "offline"
            udid = row.get("udid") or ""
            devices.append(
                {
                    "id": udid,
                    "platform": "ios",
                    "model": row.get("name") or udid,
                    "serial": udid,
                    "state": state,
                    "connection": "simulator",
                    "info": {
                        "os": f"{runtime_label} (Simulator)",
                        "cpu": f"{host_cores} cores (host)" if host_cores else None,
                        "resolution": None,
                        "batteryLevel": None,
                        "refreshRateHz": None,
                    },
                }
            )
    return devices


# ---- 应用枚举（``simctl listapps`` NeXT 风格 plist）----

_TOP_KEY_RE = re.compile(r'^"?(?P<key>[^"\s={}()]+)"?\s*=\s*\{\s*$')
_PAIR_RE = re.compile(r'^"?([^"\s=]+)"?\s*=\s*(.*?),?\s*$')


def parse_listapps(stdout: str) -> list[dict[str, Any]]:
    """``simctl listapps`` 原文 → 应用行（契约 §4 apps 元素子集）。

    NeXT 风格 plist：顶层每应用一个 ``"<bundle>" = { … };`` 块；只取块内标量键
    （CFBundleIdentifier/CFBundleDisplayName/CFBundleName/ApplicationType）。
    嵌套 dict/list（GroupContainers/SBAppTags 等）按花括号深度跳过，不解析。
    CFBundleIdentifier 缺失时回退顶层键（同值两种形态实测并存）。
    """
    apps: list[dict[str, Any]] = []
    depth = 0
    current_top: str | None = None
    fields: dict[str, str] = {}
    for raw_line in (stdout or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if depth == 1:
            if line == "};":
                if current_top is not None:
                    apps.append(_app_entry(current_top, fields))
                current_top, fields = None, {}
                depth = 0
                continue
            # 嵌套容器（GroupContainers/SBAppTags 值体 …）：先于标量对判定进入
            # 深度 2 跳过（值体形如 `Key =         {`，花括号配平后回到本层）
            if line.endswith("{") and not line.endswith("};"):
                depth = 2
                continue
            pair = _PAIR_RE.match(line)
            if pair is not None:
                key, value = pair.group(1), pair.group(2).strip()
                if value.endswith(";"):
                    value = value[:-1].strip()
                value = value.strip('"')
                fields[key] = value
                continue
            continue
        # depth >= 2：嵌套容器内部，只跟踪花括号配平
        if depth >= 2:
            if line.endswith("};") or line == "}":
                depth -= 1
            elif line.endswith("{"):
                depth += 1
            continue
        # depth == 0：应用块起始
        match = _TOP_KEY_RE.match(line)
        if match is not None:
            current_top = match.group("key")
            depth = 1
    return apps


def _app_entry(top_key: str, fields: dict[str, str]) -> dict[str, Any]:
    bundle = fields.get("CFBundleIdentifier") or top_key
    name = fields.get("CFBundleDisplayName") or fields.get("CFBundleName") or bundle
    app_type = fields.get("ApplicationType") or "Unknown"
    return {
        "packageName": bundle,
        "name": name,
        "appType": app_type,
    }


# ---- 运行中应用（``simctl spawn booted launchctl list``）----

_Uikit_RE = re.compile(r"^(\d+)\s+\S+\s+UIKitApplication:([^[\s]+)\[")


def parse_launchctl_uikit(stdout: str) -> dict[str, int]:
    """launchctl list 输出 → {bundle_id: pid}（仅 UIKitApplication 行；同名取首个）。

    非 UIKitApplication 行（系统 daemon，pid 可为 `-`）跳过。模拟器用户应用经
    SpringBoard 拉起后即以此标签出现（iOS 27.0 实测）。
    """
    table: dict[str, int] = {}
    for line in (stdout or "").splitlines():
        match = _Uikit_RE.match(line.strip())
        if match is None:
            continue
        pid_text, bundle = match.group(1), match.group(2)
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        table.setdefault(bundle, pid)
    return table


# ---- 前台探测（统一日志 FBSystemService activation 行）----

# FBSystemService 激活行：`[FBSystemService][0x…] Activation request for
# <bundle> contained LSCacheGUID …`（iOS 27.0 实测：每次 UI 应用激活/复激活
# 各发 1-2 条；widget renderer 等 scene 常驻进程不触发 activation，实测无噪音）。
_ACTIVATION_RE = re.compile(r"Activation request for ([A-Za-z0-9._-]+)")


def parse_activation_bundles(stdout: str) -> list[str]:
    """``log show --style ndjson`` 原文 → activation bundle 序列（行序=时间序）。

    ``eventMessage`` 无 `Activation request for <bundle>` 的行（count 行/log
    自身行等）跳过。已探测并否决的备选口径：`Setting process visibility to:
    Foreground/Background` 行——iOS 27.0 实测场景状态机存在「视觉前台但事件
    Background」（权限弹窗即触发，截图比对证实），作前台判据不可靠；保留
    visibility 解析仅供后续诊断用途。
    """
    bundles: list[str] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        message = row.get("eventMessage")
        if not isinstance(message, str):
            continue
        match = _ACTIVATION_RE.search(message)
        if match is not None:
            bundles.append(match.group(1))
    return bundles


def foreground_from_activation(bundles: list[str]) -> str | None:
    """activation 序列 → 当前前台 bundle（best-effort）。

    规则：取窗口内**最后一次激活**的应用（iOS 单前台：新激活即顶掉旧前台）。
    窗口内无激活信号 → None（不猜）。局限（如实声明，§23.1）：App 经 Home 键
    退后台但无新应用激活时，信号滞留为最后激活应用——activation 只承载「激活」
    不承载「退前台」。
    """
    return bundles[-1] if bundles else None


# ---- visibility 行解析（已否决为前台判据，保留供诊断；见 parse_activation_bundles 注）----

# pid 可为 -1（launch 意图事件，实测 iOS 27.0）；state 取 Foreground|Background
# （Unknown 等过渡态不采集）。
_VISIBILITY_MSG_RE = re.compile(
    r"\[app<(?P<bundle>[^>\]]+)>(?::(?P<pid>-?\d+))?\]\s+"
    r"Setting process visibility to:\s*(?P<state>Foreground|Background)"
)


def parse_visibility_events(stdout: str) -> list[dict[str, Any]]:
    """``log show --style ndjson`` 原文 → visibility 事件列表（按行序=时间序）。

    每行一个 ndjson JSON；``eventMessage`` 缺 ``[app<bundle>:pid] Setting
    process visibility to: Foreground|Background`` 的行（count 行/log 自身行/
    Unknown 过渡态）跳过。返回 ``[{bundle, pid, state}]``（pid=-1 的 launch
    意图事件落 pid=None）；ndjson 行解析失败跳过（不猜）。

    **已否决为前台判据**（iOS 27.0 实测：视觉前台但事件 Background，截图比对
    证实）——前台判定走 parse_activation_bundles（见其 docstring）。
    """
    events: list[dict[str, Any]] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        message = row.get("eventMessage")
        if not isinstance(message, str):
            continue
        match = _VISIBILITY_MSG_RE.search(message)
        if match is None:
            continue
        pid = int(match.group("pid")) if match.group("pid") else None
        events.append(
            {
                "bundle": match.group("bundle"),
                "pid": pid if (pid is not None and pid > 0) else None,
                "state": match.group("state"),
            }
        )
    return events


def foreground_from_visibility(events: list[dict[str, Any]]) -> str | None:
    """visibility 事件流 → 栈式判定（仅诊断用途，非前台判定主路径）。

    Foreground 压栈（去重栈顶），匹配栈顶的 Background 弹栈，终态栈顶=候选。
    iOS 27.0 实测该口径与视觉前台不符（见 parse_activation_bundles 注），
    生产前台判定走 foreground_from_activation。
    """
    stack: list[str] = []
    for event in events:
        bundle = event.get("bundle")
        state = event.get("state")
        if not bundle or not state:
            continue
        if state == "Foreground":
            if not stack or stack[-1] != bundle:
                stack.append(bundle)
        elif state == "Background" and stack and stack[-1] == bundle:
            stack.pop()
    return stack[-1] if stack else None


# ---- 宿主口径采样（``ps -o pid,pcpu,rss,comm -p <pid>``）----

_PS_HEADER_RE = re.compile(r"^\s*PID\s+%?CPU\s+RSS\s+COMM\s*$")


def parse_ps_stat(stdout: str) -> tuple[float, float] | None:
    """宿主 ps 输出 → (pcpu, rss_mb)；数据行缺失/解析失败 → None（不猜）。

    rss 单位 KB（macOS ps 约定），换算 MB 返回；pcpu 为 macOS ps 的衰减均值
    CPU%（多核可 >100，宿主口径如实透出，§23.2 tags.basis=host）。
    """
    for line in (stdout or "").splitlines()[1:]:
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pcpu = float(parts[1])
            rss_kb = float(parts[2])
        except ValueError:
            continue
        return pcpu, round(rss_kb / 1024.0, 1)
    return None
