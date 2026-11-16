"""devicectl 客户端（真机 iOS，R24 契约 §26）。

与 ``providers/simctl.py``（iOS Simulator）的分工：本模块只依赖 macOS 自带的
``xcrun devicectl``（Xcode 15+ 随附；本机实测 xcrun 72 / devicectl 651.32），
服务真机 iOS 设备面。命令执行面统一经 ``run_devicectl``（``runner`` 注入面，
测试换假 runner，simctl.py:40-67 先例同哲学）；JSON 解析全部为纯函数，
fixture 以 tests/fixtures/realios/ 下的真机原文做 golden（§26.6 测试边界）。

数据源命令（命令面 --help/--json-output 本机实测）::

    xcrun devicectl list devices --json-output <path>      # 设备枚举（§26.1）
    xcrun devicectl device info apps --device <id> --json-output <path>    # §26.2
    xcrun devicectl device info processes --device <id> --json-output <path>
    xcrun devicectl device info lockState --device <id> --json-output <path>  # §26.4 预热
    xcrun devicectl device info details --device <id> --json-output <path>    # 详情（非契约面）
    xcrun devicectl device process launch --device <id> <bundle> --json-output <path>

诚实性注记（2026-11-16 本机实测，fixture 入库）：

- ``--json`` 短参**不存在**，必须 ``--json-output <path>``（契约 §26.1 实测确认）；
- jsonVersion 5 顶层为 ``{"info", "result": {"devices": [...]}}``，行内
  ``hardwareProperties/deviceProperties/connectionProperties`` 已标 deprecated
  （``_deprecationNotice`` 指向 ``properties`` 字典）——解析同时容忍两处取值；
- 行 ``identifier`` 是 coredevice UUID，**不是** UDID；UDID 在
  ``hardwareProperties.udid``（配对行在场）。§26.1 golden 的
  ``id="00008130-000955B7C2D1E84A"`` 即 UDID——xctrace ``--device`` 只认
  UDID/设备名（本机实测 identifier 形态报 "No devices found matching"），故
  设备 id 取 ``hardwareProperties.udid``（identifier 仅回退）；
- ``deviceProperties.connectionState`` 7/7 行实测恒 None——不作在线信号；
  ``connectionProperties.tunnelState`` 是唯一在线判据（§26.1 映射规则）。
"""

from __future__ import annotations

import json
import os
import posixpath
import subprocess
import tempfile
import threading
from typing import Any, Callable

# xcrun 缺失/不可执行（非 macOS 或命令行工具未装）→ 503 xctrace_unavailable
# details.reason="devicectl_missing"（契约 §3 §26 新增行，与 adb_not_found/
# simctl_unavailable 同「语义分离」先例；错误码常量定义在 xctrace 模块）

# xcrun 家族全局串行锁（并发 xcrun 命令会失败，勘察实测，xctrace.py 勘察注）：
# devicectl 与 xctrace 两个执行面共用本进程级 RLock——采集窗全程持锁
# （xctrace.run_sample_window），本模块所有命令执行（含采集窗内探测与聚合面
# 枚举）同锁排队，杜绝「探测与 record/export 并发互踩」。RLock：窗口锁内
# 嵌套调用（prewarm→lockState）同线程可重入。
XCRUN_FAMILY_LOCK = threading.RLock()


def run_devicectl(
    *args: str,
    timeout: float = 30.0,
    runner: Callable[[list[str], float], Any] | None = None,
) -> subprocess.CompletedProcess[str] | None:
    """``xcrun devicectl <args>`` 文本执行；命令失败/超时返回 None（不猜）。

    ``runner`` 注入面：测试用假 runner 驱动解析逻辑（无需真机在网）；生产缺省
    subprocess.run。xcrun 本体缺失（FileNotFoundError）**透传**给调用方按
    ``xctrace_unavailable``（details.reason="devicectl_missing"）归一——与命令
    非零退出（返回 None，走重试/无数据语义）区分（契约 §3 details.reason 两态）；
    超时等其余 SubprocessError → None。
    """
    command = ["xcrun", "devicectl", *args]
    # 与 xctrace 执行面同一把家族锁：采集窗进行中，本模块命令（设备枚举/apps/
    # processes/launch 探测）在此排队，不与 record/export 并发（勘察实测并发
    # xcrun 命令会失败）；RLock 对窗口锁内的 prewarm 调用同线程重入。
    with XCRUN_FAMILY_LOCK:
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
        except FileNotFoundError:
            raise
        except subprocess.SubprocessError:
            return None


def run_devicectl_json(
    *args: str,
    timeout: float = 30.0,
    runner: Callable[[list[str], float], Any] | None = None,
) -> str:
    """带 ``--json-output`` 的 devicectl 命令 → JSON 文本；失败返回 ""（不猜）。

    ``--json-output <path>`` 落文件后读取（契约 §26.1 数据源形态）；文件缺失/
    为空时回退解析 stdout（devicectl 对 ``-`` 形态亦支持，探测期兼容）。非零
    退出按「无数据」返回 ""，调用方按各自语义处置（枚举=空列表/预热=重试）。
    xcrun 缺失透传 FileNotFoundError（调用方归一 xctrace_unavailable）。
    """
    fd, path = tempfile.mkstemp(prefix="pd-devicectl-", suffix=".json")
    os.close(fd)
    try:
        completed = run_devicectl(
            *args, "--json-output", path, timeout=timeout, runner=runner
        )
        if completed is None or completed.returncode != 0:
            return ""
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
            if text.strip():
                return text
        return completed.stdout or ""
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---- 设备枚举（契约 §26.1：过滤三规则 + 映射规则）----

# transportType → connection（契约 §26.1 映射规则）。wired 为 USB 有线形态的
# 原文值——本机未接 USB 线未实测（契约原文「待真机 USB 接入实测回填，S1 实装以
# fixture 固化」），fixture devices-sample.json 末行以该值固化在线 usb 形态，
# README 标注待 USB 复测回填。
TRANSPORT_CONNECTION = {
    "localNetwork": "wifi",
    "wired": "usb",
}


def _row_props(row: dict[str, Any]) -> tuple[dict, dict, dict]:
    """行 → (hardwareProperties, deviceProperties, connectionProperties)。

    jsonVersion 5 三组 *Properties 已标 deprecated（replacement=``properties``
    字典）；两处取值等价时优先 deprecated 原文（与 §26.1 规则原文对齐），
    properties 侧兜底（未来版本移除后不炸）。
    """
    props = row.get("properties") or {}
    hw = row.get("hardwareProperties") or props.get("hardware") or {}
    dp = row.get("deviceProperties") or props.get("device") or {}
    cp = row.get("connectionProperties") or props.get("connection") or {}
    return hw, dp, cp


def parse_devices_json(stdout: str) -> list[dict[str, Any]]:
    """``devicectl list devices --json-output`` → 契约 §4/§26.1 设备行列表。

    **过滤规则（§26.1 硬性三类行不出）**：

    1. ``hardwareProperties.platform == "iOS"`` 限定——watchOS 等非 iOS 行不出
       （本机实测 Watch7,2 行 platform=="watchOS" 被拦下）；
    2. 模拟器行不出：``hardwareProperties.reality == "simulated"`` 或
       ``connectionProperties.transportType == "sameMachine"``——任一命中即排除
       （防与 simctl 源同 UDID 双行）；
    3. ``connectionProperties.pairingState == "unsupported"`` 未配对行不出
       （本机实测仅带 ECID 无 UDID 的未配对行带该值，tunnelState=unavailable）。

    **映射规则（§26.1 硬性）**：

    - ``hardwareProperties.udid`` → ``id``/``serial``（identifier 是 coredevice
      UUID 非 UDID，见模块 docstring；仅无 udid 时回退 identifier）；
    - ``hardwareProperties.marketingName`` → ``model``（缺失回退设备名）；
    - ``connectionProperties.transportType`` → ``connection``（TRANSPORT_CONNECTION）；
      其余 transport 值不出行（不猜——宁可少一行不出错行）；
    - ``connectionProperties.tunnelState`` → ``state``：``connected`` →
      ``online``、``disconnected``/``unavailable`` → ``offline``（不猜第三态）；
      ``connectionState`` 恒不作为信号（7/7 行实测恒 None）；
    - ``info.os`` = ``"iOS <osVersionNumber>"``（osVersionNumber 缺失置 None）；
      ``info.cpu``/``resolution``/``batteryLevel``/``refreshRateHz`` devicectl
      列表不暴露 → ``null``（不猜，§26.1）。
    """
    try:
        body = json.loads(stdout)
    except (TypeError, ValueError):
        return []
    result = body.get("result") if isinstance(body, dict) else None
    rows = (result or body if isinstance(result, dict) else body).get("devices") or []
    devices: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        hw, dp, cp = _row_props(row)
        # 规则 1：platform=="iOS" 限定（watchOS 等非 iOS 行不出）
        if hw.get("platform") != "iOS":
            continue
        # 规则 2：模拟器行不出（任一命中即排除）
        if hw.get("reality") == "simulated" or cp.get("transportType") == "sameMachine":
            continue
        # 规则 3：未配对行不出
        if cp.get("pairingState") == "unsupported":
            continue
        # transport → connection；未映射值不出行（不猜）
        connection = TRANSPORT_CONNECTION.get(cp.get("transportType"))
        if connection is None:
            continue
        # tunnel 是唯一在线判据（connectionState 恒 None 不作信号；不猜第三态）
        tunnel = cp.get("tunnelState") or dp.get("tunnelState")
        state = "online" if tunnel == "connected" else "offline"
        udid = hw.get("udid") or row.get("identifier") or ""
        os_version = dp.get("osVersionNumber")
        devices.append(
            {
                "id": udid,
                "platform": "ios",
                "model": hw.get("marketingName") or dp.get("name") or udid,
                "serial": udid,
                "state": state,
                "connection": connection,
                "info": {
                    "os": f"iOS {os_version}" if os_version else None,
                    "cpu": None,  # devicectl 列表不暴露（§26.1 不猜）
                    "resolution": None,
                    "batteryLevel": None,
                    "refreshRateHz": None,
                },
            }
        )
    return devices


# ---- 应用枚举（契约 §26.2：apps + processes 两命令合成）----


def parse_apps_json(stdout: str) -> list[dict[str, Any]] | None:
    """``devicectl device info apps --json-output`` → 应用行列表。

    每行取 bundleIdentifier/name/url（bundle 目录 URL，用于与 processes 的
    executable 按 bundle 目录匹配）；命令失败/JSON 不可解析 → None（调用方
    404 语义，与 SimulatorProvider._installed_apps 同哲学）。
    """
    try:
        body = json.loads(stdout)
    except (TypeError, ValueError):
        return None
    result = body.get("result") if isinstance(body, dict) else None
    if not isinstance(result, dict):
        return None
    apps = result.get("apps")
    if not isinstance(apps, list):
        return None
    rows: list[dict[str, Any]] = []
    for app in apps:
        if not isinstance(app, dict) or not app.get("bundleIdentifier"):
            continue
        rows.append(
            {
                "packageName": app["bundleIdentifier"],
                "name": app.get("name") or app["bundleIdentifier"],
                # file:///…/Runner.app/ → "Runner.app"（bundle 目录名，匹配用）
                "bundleDir": _url_last_segment(app.get("url")),
            }
        )
    return rows


def parse_processes_json(stdout: str) -> list[dict[str, Any]] | None:
    """``devicectl device info processes --json-output`` → 进程行列表。

    ``result.runningProcesses`` 每行 ``{executable: file:///…/App.app/Bin,
    processIdentifier: pid}`` → ``{pid, name, path}``；``name`` = executable
    路径末段（xctrace ``--attach`` 目标名即它，契约 §26.2）。解析失败 → None。
    """
    try:
        body = json.loads(stdout)
    except (TypeError, ValueError):
        return None
    result = body.get("result") if isinstance(body, dict) else None
    if not isinstance(result, dict):
        return None
    processes = result.get("runningProcesses")
    if not isinstance(processes, list):
        return None
    rows: list[dict[str, Any]] = []
    for proc in processes:
        if not isinstance(proc, dict):
            continue
        pid = proc.get("processIdentifier")
        path = _file_url_to_path(proc.get("executable"))
        if not isinstance(pid, int) or not path:
            continue
        rows.append(
            {
                "pid": pid,
                "name": posixpath.basename(path),
                "path": path,
            }
        )
    return rows


def match_app_processes(
    apps: list[dict[str, Any]], processes: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """processes 按 bundle 目录归到应用（契约 §26.2「按 bundle 匹配」）。

    规则：executable 路径含 ``/<bundleDir>/``（如 ``/Runner.app/``）→ 归属该
    应用；返回 ``{packageName: [{pid, name, isMain}]}``。``isMain`` = executable
    末段 == bundle 目录去后缀（Runner.app → Runner）；Extension（…appex 内）
    归属同 bundle 但 isMain=False。返回 dict 恒非 None（无匹配 = 空表）。
    """
    matched: dict[str, list[dict[str, Any]]] = {}
    for app in apps:
        bundle_dir = app.get("bundleDir")
        if not bundle_dir:
            continue
        stem = bundle_dir.removesuffix(".app").removesuffix(".appex")
        hits = [
            proc
            for proc in processes
            if f"/{bundle_dir}/" in f"/{proc['path'].lstrip('/')}"
        ]
        if not hits:
            continue
        entries = [
            {
                "pid": proc["pid"],
                "name": proc["name"],
                "isMain": proc["name"] == stem,
            }
            for proc in hits
        ]
        entries.sort(key=lambda item: (not item["isMain"], item["pid"]))
        matched[app["packageName"]] = entries
    return matched


def resolve_main_process(
    apps: list[dict[str, Any]] | None,
    processes: list[dict[str, Any]] | None,
    package_name: str,
) -> dict[str, Any] | None:
    """bundle → 主进程 {pid, name}；未安装/未运行/仅 Extension 存活 → None（不猜）。

    xctrace ``--attach`` 只认进程名（禁 PID attach，勘察实测 exit 21）——采集
    链经本函数把 packageName 归一到 attach 目标名。仅 isMain 单元格（可执行
    末段 == bundle 目录去后缀）作主进程；Extension 进程（…appex）不作 attach
    目标（采它的 CPU/内存口径不对）。
    """
    if apps is None or processes is None:
        return None
    entries = match_app_processes(apps, processes).get(package_name) or []
    main = next((e for e in entries if e["isMain"]), None)
    if main is None:
        return None
    return {"pid": main["pid"], "name": main["name"]}


# ---- launch（契约 §26.2 launchOnCreate 真机形态）----


def parse_launch_json(stdout: str) -> int | None:
    """``devicectl device process launch --json-output`` → pid。

    ``result.process.processIdentifier``（本机实测 15436 形态）；缺失/不可解析
    → None（调用方按 launch 失败广播，不猜）。
    """
    try:
        body = json.loads(stdout)
    except (TypeError, ValueError):
        return None
    result = body.get("result") if isinstance(body, dict) else None
    if not isinstance(result, dict):
        return None
    pid = (result.get("process") or {}).get("processIdentifier")
    return pid if isinstance(pid, int) else None


# ---- lockState（契约 §26.4 预热探测）----


def parse_lock_state_json(stdout: str) -> dict[str, Any] | None:
    """``devicectl device info lockState --json-output`` → result 字典。

    本机实测 result 形态：``{deviceIdentifier, passcodeRequired: bool,
    unlockedSinceBoot: bool}``。预热语义只看命令成功与否（outcome=success），
    本解析供探测/诊断读数；不可解析 → None。
    """
    try:
        body = json.loads(stdout)
    except (TypeError, ValueError):
        return None
    result = body.get("result") if isinstance(body, dict) else None
    return result if isinstance(result, dict) else None


# ---- details（设备详情；非 §26.1 契约字段源，仅供诊断/未来扩展）----


def parse_details_json(stdout: str) -> dict[str, Any] | None:
    """``devicectl device info details --json-output`` → 详情读数。

    提取非契约面的设备读数（udid/marketingName/osVersionNumber/cpuCores/
    serialNumber/reality）——§26.1 设备行 cpuCores 等恒 null 的契约不因此改变
    （列表面不暴露即不猜）；本解析仅供 live smoke 与诊断面取真值。
    """
    try:
        body = json.loads(stdout)
    except (TypeError, ValueError):
        return None
    result = body.get("result") if isinstance(body, dict) else None
    if not isinstance(result, dict):
        return None
    hw = result.get("hardwareProperties") or {}
    dp = result.get("deviceProperties") or {}
    props = result.get("properties") or {}
    prop_hw = props.get("hardware") or {}
    cpu_count = (prop_hw.get("cpuCount") or {}).get("logicalCores")
    if cpu_count is None:
        cpu_count = (hw.get("cpuCount") or {}).get("logicalCores")
    return {
        "udid": hw.get("udid") or prop_hw.get("udid"),
        "marketingName": hw.get("marketingName") or prop_hw.get("marketingName"),
        "osVersionNumber": dp.get("osVersionNumber"),
        "serialNumber": hw.get("serialNumber") or prop_hw.get("serialNumber"),
        "cpuCores": cpu_count if isinstance(cpu_count, int) else None,
        "reality": hw.get("reality") or prop_hw.get("reality"),
    }


# ---- 工具 ----


def _file_url_to_path(url: Any) -> str | None:
    """``file:///a/b/C.app/Bin`` → ``/a/b/C.app/Bin``；非 file URL → None。"""
    if not isinstance(url, str) or not url.startswith("file://"):
        return None
    path = url[len("file://"):]
    # 去掉 query/fragment（devicectl 实测无，防御性保留）
    path = path.split("?", 1)[0].split("#", 1)[0]
    return path or None


def _url_last_segment(url: Any) -> str | None:
    """``file:///…/Runner.app/`` → ``Runner.app``（bundle 目录名）；缺失 → None。"""
    path = _file_url_to_path(url)
    if not path:
        return None
    segment = posixpath.basename(path.rstrip("/"))
    return segment or None
