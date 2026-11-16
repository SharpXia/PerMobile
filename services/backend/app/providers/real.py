"""real provider（H5）：复用主工程 perfeval 的 adb 能力（ARCH §4.3）。

- 设备枚举：perfeval.collectors.android.preflight.run_preflight（诊断码映射契约 §3）；
- 设备详情 best-effort getprop/dumpsys，探测失败置 null（不猜，契约 §4）；
- 应用列表：pm list packages + dumpsys activity 前台探测，失败置 null。

M6（契约 §20.2/§21.1/§21.3）：
- 设备 profile 探测（R21）：gpuSource 由 kgsl sysfs 存在性一次性读取（差距列
  「替代散落在 parser 的硬编码」），探测失败置 null；cpuCores/refreshRateHz 复用
  既有探测面；usb/wifi 按连接形态判定（adb connect 的 serial 形如 host:port）；
- meminfo 明细段探测（R17 勾选组硬性依赖，契约 §21.3）：`dumpsys meminfo system`
  一次解析，四明细项全部可解析才整组可用；
- WiFi 模式（R15）：tcpip/connect 命令执行 + 连接成功的 WiFi 行登记（同一物理
  serial 的 USB 行与 WiFi 行并存时为独立设备 id，id = f"{usb-serial}-wifi"）。

导入失败（perfeval 不可用）时在构造期抛 ImportError，由 main 归一到 internal_error。
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from typing import Any

from perfeval.collectors.android.adb import AdbClient
from perfeval.collectors.android.preflight import run_preflight

from ..errors import ApiError
from ..registry import available_metric_ids
from ..services.capability_matrix import device_profile, wire_profile
from ..services.meminfo_detail import detail_channel_available, parse_meminfo_detail
from ..services.speedtest import (
    SPEEDTEST_ADB_GRACE_S,
    SPEEDTEST_METHOD,
    SPEEDTEST_NOTE,
    curl_command,
    mbps_from_bytes_per_sec,
    parse_speed_download,
    payload_path,
)
from .base import DeviceProvider

# adb state -> 契约 state 枚举（online | offline | unauthorized）
_STATE_MAP = {
    "device": "online",
    "offline": "offline",
    "unauthorized": "unauthorized",
}

# adb tcpip 约定端口（契约 §21.1 golden）
WIFI_PORT = 5555

# kgsl GPU 驱动节点（[F§3.7] Android 仅部分机型有 kgsl；存在性即 GPU 利用率源）
_KGSL_NODE = "/sys/class/kgsl/kgsl-3d0"

# power.* 数据源探测节点（perfeval power_sampler 同源；SM_F9560 实测整目录被 SELinux 屏蔽）
_POWER_CAPACITY_NODE = "/sys/class/power_supply/battery/capacity"

# adb connect 后 `adb devices` 的 serial 形态（host:port）
_WIFI_SERIAL_RE = re.compile(r"^[\w.-]+:\d+$")

# dumpsys display 中模式行/复合行内的刷新率（`fps=120.00001`）
_DISPLAY_FPS_RE = re.compile(r"fps=([\d.]+)")

_MEMINFO_DETAIL_PROBE_TARGET = "system"  # 常驻系统进程，探测明细段可用性（首选）

# dumpsys package <pkg> 的版本行（S2 version-metadata 接线 2026-11-10）：
# versionName=1.2.3 / versionCode=123（新版 ROM 后者可带空格后缀 flags/minSdk）
_VERSION_NAME_RE = re.compile(r"versionName=(\S+)")
_VERSION_CODE_RE = re.compile(r"versionCode=(\d+)")
# mem-2 修复（2026-11-06 体检）：探测样本与被测应用解耦——AOSP 对全零 OTHER_STATS
# 行整行不打印（ActivityThread.dumpMemInfoTable 非零跳过循环），system_server 明细
# 段整段缺失时补探至多 N 个三方包。2026-11-16 门禁判据修订：detail_channel_available
# （native/dalvik 任一可解析）替代 detail_complete（四项全齐）——图形池行只在有
# GL 上下文的进程出现，SM_F9560 与模拟器 system 实测均无 gfx 行，旧判据系统性误禁
# 整组；单项行缺失由采样层逐项不出点兜住。
_MEMINFO_DETAIL_FALLBACK_PACKAGES = 3

_SPEEDTEST_DEFAULT_TIME_S = 60  # 与 services.speedtest.SPEEDTEST_DEFAULT_TIME_S 同值（方法缺省参数用）


def _free_tcp_port() -> int:
    """取一个本地空闲 TCP 端口（adb reverse 目标；先绑 0 取号后释放）。

    取号到 reverse 建立之间存在极小竞态窗口（端口被抢占即 reverse 失败 → 503，
    不重试不猜），实机单用户场景足够。
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _shell(adb: AdbClient, serial: str, *args: str, timeout: float = 5.0) -> str | None:
    """best-effort shell 读数；失败返回 None（契约：探测失败置 null，不猜）。"""
    try:
        completed = adb.run("shell", *args, serial=serial, timeout=timeout)
        if completed.returncode != 0:
            return None
        text = completed.stdout.strip()
        return text or None
    except (OSError, subprocess.SubprocessError):
        return None


class RealProvider(DeviceProvider):
    mode = "real"

    def __init__(self, adb: AdbClient | None = None) -> None:
        self._adb = adb or AdbClient()
        # M6 profile 探测缓存（per serial 一次性读，list_devices 高频调用不重复探测）
        self._gpu_source_cache: dict[str, str | None] = {}
        self._power_supply_cache: dict[str, bool | None] = {}
        self._meminfo_detail_cache: dict[str, bool | None] = {}
        # S2 version-metadata（2026-11-10）：应用版本探测缓存（(serial, package) →
        # 结果或 None；None=探测失败也缓存——save 时点快照语义，不重试不猜）
        self._app_version_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
        self._bg_probes: set[str] = set()
        # 设备信息 TTL 缓存（实机验证修复：_device_info 批量化 + 10s 内不重复探测）
        self._info_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        # R15：连接成功的 WiFi 行登记 {usb_serial: host}（设备枚举时以独立行出现）
        self._wifi_hosts: dict[str, str] = {}
        self._probe_lock = threading.Lock()
        self._bg_lock = threading.Lock()

    # ---- 设备 ----

    def list_devices(self) -> list[dict[str, Any]]:
        if not self._adb.available():
            raise ApiError("adb_not_found", "adb 不可用（映射 preflight android.adb.not_found）")
        try:
            result = run_preflight(self._adb)
        except Exception as exc:  # noqa: BLE001 - adb devices 执行异常归一
            raise ApiError(
                "adb_devices_failed", f"adb devices 执行失败: {exc}"
            ) from exc
        if "android.adb.devices_failed" in result.diagnostics:
            raise ApiError(
                "adb_devices_failed",
                "adb devices 执行失败（映射 preflight android.adb.devices_failed）",
                details={"preflight": "android.adb.devices_failed"},
            )
        online_serials = {dev.serial: dev for dev in result.devices}
        devices: list[dict[str, Any]] = []
        for dev in result.devices:
            state = _STATE_MAP.get(dev.state, "offline")
            if _WIFI_SERIAL_RE.match(dev.serial):
                continue  # WiFi 连接行并入宿主 serial 的 wifi 独立行（见下）
            devices.append(
                {
                    "id": dev.serial,
                    "platform": "android",
                    "model": dev.model or dev.device or dev.product or dev.serial,
                    "serial": dev.serial,
                    "state": state,
                    "connection": "usb",
                    "profile": wire_profile(self.get_profile(dev.serial)),
                    "info": self._device_info(dev.serial),
                }
            )
        # R15：已登记 WiFi host 且 adb 侧确实在线 → 同一物理设备的 WiFi 独立行
        #（id = f"{usb_serial}-wifi"，契约 §21.1「同一物理 serial 的 USB 行与 WiFi 行
        # 并存时为两个 provider 分配的独立设备 id」；host:port 行不在线则不出行，不伪造）
        for usb_serial, host in self._wifi_hosts.items():
            wifi_dev = online_serials.get(f"{host}:{WIFI_PORT}")
            if wifi_dev is None:
                continue
            state = _STATE_MAP.get(wifi_dev.state, "offline")
            devices.append(
                {
                    "id": f"{usb_serial}-wifi",
                    "platform": "android",
                    "model": (
                        wifi_dev.model or wifi_dev.device or wifi_dev.product
                        or f"{usb_serial}-wifi"
                    ),
                    "serial": usb_serial,
                    "state": state,
                    "connection": "wifi",
                    "profile": wire_profile(self.get_profile(usb_serial, over_wifi=True)),
                    "info": self._device_info(f"{host}:{WIFI_PORT}"),
                }
            )
        return devices

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        for device in self.list_devices():
            if device["id"] == device_id:
                return device
        return None

    # ---- 设备会话定位（wifi 行 id → adb 侧实际 serial）----

    def adb_serial(self, device_id: str) -> str:
        """设备 id → adb serial：wifi 行映射回 host:port 形态（shell 命令目标）。"""
        if device_id.endswith("-wifi"):
            usb_serial = device_id[: -len("-wifi")]
            host = self._wifi_hosts.get(usb_serial)
            if host is not None:
                return f"{host}:{WIFI_PORT}"
        return device_id

    def launch_app(self, device_id: str, package_name: str) -> dict[str, Any]:
        """launchOnCreate=true 的 app_prepare（契约 §6.1，实机验证补接线 2026-11-03）。

        复用 perfeval plan 层命令构造（ARCH §4.4 接缝）：query_launcher_activities →
        parse_launcher_activity_component → launch_command（am start -W）。
        返回 ``{"component": str | None, "error": str | None}``——不抛异常，失败由
        调用方广播 phase failed + diagnostic error、会话保持 monitoring。
        """
        from perfeval.collectors.android.adb import (
            launch_command,
            parse_launcher_activity_component,
            query_launcher_activities_command,
        )

        serial = self.adb_serial(device_id)
        try:
            completed = self._adb.run(
                *query_launcher_activities_command(self._adb, package_name)[1:],
                serial=serial, timeout=15.0,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {"component": None, "error": f"resolve_activity failed: {exc}"}
        component = parse_launcher_activity_component(
            completed.stdout, package_name=package_name
        )
        if component is None:
            return {"component": None, "error": "android.launch_activity_unresolved"}
        try:
            launched = self._adb.run(
                *launch_command(self._adb, component)[1:],
                serial=serial, timeout=30.0,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {"component": component, "error": f"launch failed: {exc}"}
        if launched.returncode != 0 or "Error" in (launched.stdout or ""):
            detail = (launched.stdout or launched.stderr or "").strip()[:200]
            return {"component": component, "error": f"am start failed: {detail}"}
        return {"component": component, "error": None}

    # ---- M6 profile 探测（R21，契约 §20.2）----

    def get_profile(self, device_id: str, *, over_wifi: bool = False) -> dict[str, Any] | None:
        """设备 profile（capability_matrix 判定输入；设备不存在返回 None）。"""
        usb_serial = device_id.removesuffix("-wifi")
        target = self.adb_serial(device_id)
        if over_wifi:
            host = self._wifi_hosts.get(usb_serial)
            target = f"{host}:{WIFI_PORT}" if host else target
        info = self._device_info(target)
        cores = self._parse_cores(info.get("cpu"))
        refresh = info.get("refreshRateHz")
        return device_profile(
            usb=not over_wifi,
            wifi=over_wifi,
            gpu_source=self._probe_gpu_source(usb_serial),
            cpu_cores=cores,
            refresh_rate_hz=refresh,
            meminfo_detail=self._meminfo_detail_cached_or_bg(usb_serial),
            power_supply=self._probe_power_supply(usb_serial),
        )

    @staticmethod
    def _parse_cores(cpu_text: Any) -> int | None:
        """"8 cores" → 8；探测失败 → None（不猜）。"""
        if not isinstance(cpu_text, str):
            return None
        token = cpu_text.split("cores", 1)[0].strip()
        return int(token) if token.isdigit() else None

    def _probe_gpu_source(self, serial: str) -> str | None:
        """kgsl sysfs 存在性 → "kgsl"；不存在/探测失败 → None（R21，探测失败不猜）。"""
        with self._probe_lock:
            if serial in self._gpu_source_cache:
                return self._gpu_source_cache[serial]
        # 实机验证修复（2026-11-03）：原 `sh -c "ls … 2>/dev/null"` 经 adb 空格拼接后
        # 引号丢失，设备侧变成 `sh -c ls <node> 2>/dev/null` —— `sh -c` 只把 `ls` 当
        # 脚本、无参执行列出根目录，恒非空 → 恒报 "kgsl"（模拟器实机踩中）。改为
        # 单 argv 元素直传，设备 shell 正确解析重定向。
        # 真机回归修复（2026-11-11，小米 2405CPX3DC/Android 16）：该机 SELinux
        # 拒目录列（`ls kgsl-3d0` 空输出）但允文件读（`cat gpubusy` 可读）——探测
        # 改为对齐数据面手段：直接 cat gpu.busy.pct 实际读取的节点
        # （gpu_thermal_sampler GPUBUSY_PATH），非空即 kgsl 存在，空/失败仍 None。
        text = _shell(self._adb, serial, f"cat {_KGSL_NODE}/gpubusy 2>/dev/null", timeout=5.0)
        source = "kgsl" if text else None
        with self._probe_lock:
            self._gpu_source_cache[serial] = source
        return source

    def _probe_power_supply(self, serial: str) -> bool | None:
        """power.* 数据源可读性（2026-11-03 实机验证新增）。

        perfeval power_sampler 读 `/sys/class/power_supply/battery/*`；SM_F9560 被
        SELinux 拒绝整个目录（`ls /sys/class/power_supply/` Permission denied），
        power.* 三指标在该设备无数据源。可读且 capacity 为数值 → True；命令成功但
        输出为空（节点存在但被屏蔽读数）→ False；命令失败 → None（不猜）。
        """
        with self._probe_lock:
            if serial in self._power_supply_cache:
                return self._power_supply_cache[serial]
        try:
            completed = self._adb.run(
                "shell", f"cat {_POWER_CAPACITY_NODE} 2>/dev/null",
                serial=serial, timeout=5.0,
            )
            if completed.returncode != 0:
                result: bool | None = None
            else:
                text = (completed.stdout or "").strip()
                result = bool(text.isdigit()) if text else False
        except (OSError, subprocess.SubprocessError):
            result = None
        with self._probe_lock:
            self._power_supply_cache[serial] = result
        return result

    def _meminfo_detail_cached_or_bg(self, serial: str) -> bool | None:
        """缓存命中直接返回；未探测过则后台线程探测，未决期间返回 None。

        实机验证修复（2026-11-02）：`dumpsys meminfo` 在部分机型上秒级，同步探会让
        list_devices 首屏被它拖死。探测结果落缓存后下一轮刷新生效（None 按「探测中，
        暂不可勾」处理，不猜）。需要确定性结果的调用方（测试/显式预热）直接调
        `_probe_meminfo_detail`。
        """
        with self._probe_lock:
            if serial in self._meminfo_detail_cache:
                return self._meminfo_detail_cache[serial]
            with self._bg_lock:
                if serial in self._bg_probes:
                    return None  # 后台探测已在途
                self._bg_probes.add(serial)

        def _run() -> None:
            try:
                self._probe_meminfo_detail(serial)
            finally:
                with self._bg_lock:
                    self._bg_probes.discard(serial)

        threading.Thread(
            target=_run, daemon=True, name=f"meminfo-probe-{serial}"
        ).start()
        return None

    def _probe_meminfo_detail(self, serial: str) -> bool | None:
        """R17 勾选组硬性依赖：`dumpsys meminfo system` 明细段**通道存在** → True
        （detail_channel_available，契约 §21.3 2026-11-16 修订——native/dalvik 任一
        可解析即放行，图形池行缺席不再否定整组）；明细段整体缺失 → 补探三方包
        （mem-2 修复）后仍缺 → False；探测命令失败 → None（按缺失处理，不猜）。"""
        with self._probe_lock:
            if serial in self._meminfo_detail_cache:
                return self._meminfo_detail_cache[serial]
        try:
            completed = self._adb.run(
                "shell", "dumpsys", "meminfo", _MEMINFO_DETAIL_PROBE_TARGET,
                serial=serial, timeout=15.0,
            )
            ok = completed.returncode == 0
            text = completed.stdout if ok else ""
        except (OSError, subprocess.SubprocessError):
            text = ""
            ok = None
        detail = parse_meminfo_detail(text) if text else None
        if ok is None:
            result: bool | None = None  # 探测命令失败（不猜）
        else:
            result = detail_channel_available(detail)
        if result is not True:
            fallback = self._probe_meminfo_detail_fallback(serial)
            if fallback is not None:
                result = fallback
        with self._probe_lock:
            self._meminfo_detail_cache[serial] = result
        return result

    def _probe_meminfo_detail_fallback(self, serial: str) -> bool | None:
        """system_server 无明细段时的补探（mem-2 修复）：取至多 N 个三方包逐个
        `dumpsys meminfo <pkg>`，任一 detail_channel_available → True（被测应用
        形态以应用进程为准，探测目标不应否定应用侧能力）；全部无明细段 → False；
        无法列包/全部探测命令失败 → None（维持首选探测结论，不猜）。"""
        try:
            listed = self._adb.run(
                "shell", "pm", "list", "packages", "-3",
                serial=serial, timeout=10.0,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if listed.returncode != 0:
            return None
        packages = [
            line.removeprefix("package:").strip()
            for line in listed.stdout.splitlines()
            if line.startswith("package:")
        ]
        probed_any = False
        for package in packages[:_MEMINFO_DETAIL_FALLBACK_PACKAGES]:
            if not package:
                continue
            try:
                completed = self._adb.run(
                    "shell", "dumpsys", "meminfo", package,
                    serial=serial, timeout=15.0,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if completed.returncode != 0:
                continue
            probed = parse_meminfo_detail(completed.stdout) if completed.stdout else None
            if probed is None:
                # 无明细行（包未运行或无明细段）：不计入判定（不猜）
                continue
            probed_any = True
            if detail_channel_available(probed):
                return True
        # 补探过且无一齐全 → False（维持假阴性判定）；一个都没探成 → None（不覆盖
        # 首选探测结论，不猜）
        return probed_any or None

    # ---- S2 version-metadata：应用版本只读探测（2026-11-10）----

    def app_version(self, device_id: str, package_name: str) -> dict[str, Any] | None:
        """`dumpsys package <pkg>` 的 versionName/versionCode（save 时点快照）。

        缓存模式同 _meminfo_detail_cached_or_bg 的探针缓存（per (serial, package)
        一次性读）：save 路径每次运行只探一次（非热路径，同步探测），失败结果
        （命令失败/输出不可解析/权限拒绝）同样入缓存——按 None 上抛由调用方置
        manifest.appVersion=null（不伪造、不重试，save 时点快照语义）。测试/
        显式预热可直接调本方法（确定性结果）。
        """
        serial = self.adb_serial(device_id)
        cache_key = (serial, package_name)
        with self._probe_lock:
            if cache_key in self._app_version_cache:
                return self._app_version_cache[cache_key]
        result = self._probe_app_version(serial, package_name)
        with self._probe_lock:
            self._app_version_cache[cache_key] = result
        return result

    def _probe_app_version(self, serial: str, package_name: str) -> dict[str, Any] | None:
        try:
            completed = self._adb.run(
                "shell", "dumpsys", "package", package_name,
                serial=serial, timeout=15.0,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        stdout = completed.stdout or ""
        name_match = _VERSION_NAME_RE.search(stdout)
        code_match = _VERSION_CODE_RE.search(stdout)
        if name_match is None and code_match is None:
            return None  # 输出不可解析（包不存在/权限拒绝）→ None（不伪造）
        return {
            "versionName": name_match.group(1) if name_match else None,
            "versionCode": int(code_match.group(1)) if code_match else None,
        }

    # ---- R15 WiFi 模式（契约 §21.1，real-only）----

    def wifi_enable(self, device_id: str) -> dict[str, Any]:
        """`adb -s <serial> tcpip 5555`：重启 adbd 于 TCP 模式。"""
        serial = self.adb_serial(device_id)
        try:
            completed = self._adb.run(
                "tcpip", str(WIFI_PORT), serial=serial, timeout=15.0
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ApiError(
                "wifi_connect_failed", f"adb tcpip 执行失败：{exc}"
            ) from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[:200]
            raise ApiError(
                "wifi_connect_failed",
                f"adb tcpip {WIFI_PORT} 失败（rc={completed.returncode}）",
                details={"adb": detail} if detail else {},
            )
        return {
            "deviceId": device_id,
            "action": "enable",
            "state": "tcpip_enabled",
            "port": WIFI_PORT,
        }

    def wifi_connect(self, device_id: str, host: str) -> dict[str, Any]:
        """`adb connect <host>:5555`：成功登记 WiFi 行（同 serial 独立设备 id）。"""
        serial = self.adb_serial(device_id)
        try:
            completed = self._adb.run(
                "connect", f"{host}:{WIFI_PORT}", serial=None, timeout=15.0
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ApiError(
                "wifi_connect_failed", f"adb connect 执行失败：{exc}"
            ) from exc
        output = (completed.stdout or "").strip()
        # adb connect 成功输出形如 "connected to host:5555"/"already connected to ..."
        if completed.returncode != 0 or not output.startswith(
            ("connected to", "already connected")
        ):
            raise ApiError(
                "wifi_connect_failed",
                f"adb connect {host}:{WIFI_PORT} 失败（rc={completed.returncode}）：{output[:200]}",
            )
        with self._probe_lock:
            self._wifi_hosts[serial] = host
        return {
            "deviceId": f"{serial}-wifi",
            "serial": serial,
            "action": "connect",
            "host": host,
            "port": WIFI_PORT,
            "connection": "wifi",
        }

    # ---- R53 最小切片：网络测速（契约 §24，real-only）----

    def speedtest(
        self,
        device_id: str,
        *,
        bytes_count: int,
        backend_port: int,
        max_time_s: int = _SPEEDTEST_DEFAULT_TIME_S,
    ) -> dict[str, Any]:
        """设备侧实测下行吞吐（契约 §24）。

        链路：`adb reverse tcp:<本地临时端口> tcp:<后端端口>` → 设备 shell
        `curl http://127.0.0.1:<本地端口>/api/speedtest/payload?bytes=N` →
        后端 `/api/speedtest/payload`。失败阶段由 details.reason 区分
        （curl_missing / adb_reverse_failed / curl_failed / curl_output_unparseable），
        全部归一 503 `speedtest_unavailable`——不返回 0 填充的测速结果（§1 空值原则）。
        adb reverse 无论成败都在 finally 中清理（不留残留隧道）。
        """
        serial = self.adb_serial(device_id)
        # 1) 设备侧 curl 存在性探测（toybox which；缺失即 honest 降级，不换实现不猜）
        probe = _shell(self._adb, serial, "which", "curl", timeout=5.0)
        if probe is None:
            raise ApiError(
                "speedtest_unavailable",
                "设备侧 curl 不可用（which curl 无结果），下行测速通道不可用",
                details={"reason": "curl_missing"},
            )
        local_port = _free_tcp_port()
        try:
            reversed_ = self._adb.run(
                "reverse", f"tcp:{local_port}", f"tcp:{backend_port}",
                serial=serial, timeout=10.0,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ApiError(
                "speedtest_unavailable", f"adb reverse 执行失败：{exc}",
                details={"reason": "adb_reverse_failed"},
            ) from exc
        if reversed_.returncode != 0:
            detail = (reversed_.stderr or reversed_.stdout or "").strip()[:200]
            raise ApiError(
                "speedtest_unavailable",
                f"adb reverse tcp:{local_port} 建立失败（rc={reversed_.returncode}）",
                details={"reason": "adb_reverse_failed", **({"adb": detail} if detail else {})},
            )
        try:
            url = f"http://127.0.0.1:{local_port}{payload_path(bytes_count)}"
            started = time.monotonic()
            try:
                measured = self._adb.run(
                    "shell", *curl_command(url, max_time_s=max_time_s),
                    serial=serial, timeout=max_time_s + SPEEDTEST_ADB_GRACE_S,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ApiError(
                    "speedtest_unavailable", f"设备侧 curl 执行失败：{exc}",
                    details={"reason": "curl_failed"},
                ) from exc
            wall_ms = int((time.monotonic() - started) * 1000)
            if measured.returncode != 0:
                detail = (measured.stderr or measured.stdout or "").strip()[:200]
                raise ApiError(
                    "speedtest_unavailable",
                    f"设备侧 curl 退出码 {measured.returncode}（超时或链路失败）",
                    details={"reason": "curl_failed", **({"curl": detail} if detail else {})},
                )
            speed = parse_speed_download(measured.stdout)
            if speed is None:
                raise ApiError(
                    "speedtest_unavailable",
                    "curl -w %{speed_download} 输出不可解析（不猜不填零）",
                    details={"reason": "curl_output_unparseable"},
                )
            if speed <= 0:
                # 固定大小负载下 0 B/s = 0 字节传输（链路异常），不是真实测速值；
                # 返回 0 填充结果违反 §1 空值原则，按失败处理。
                raise ApiError(
                    "speedtest_unavailable",
                    "curl rc=0 但下行速率为 0（0 字节传输，链路异常）",
                    details={"reason": "curl_zero_transfer"},
                )
            return {
                "deviceId": device_id,
                "method": SPEEDTEST_METHOD,
                "bytes": bytes_count,
                "durationMs": wall_ms,
                "bytesPerSec": speed,
                "mbps": mbps_from_bytes_per_sec(speed),
                "note": SPEEDTEST_NOTE,
            }
        finally:
            try:
                self._adb.run(
                    "reverse", "--remove", f"tcp:{local_port}",
                    serial=serial, timeout=10.0,
                )
            except (OSError, subprocess.SubprocessError):
                pass  # 清理失败不掩盖主结果；残留隧道随 adb 服务终止回收

    # ---- 应用 ----

    def list_apps(self, device_id: str) -> dict[str, Any] | None:
        adb = self._adb
        serial = self.adb_serial(device_id)
        # 轻量在线检查（1 次 adb 往返；原 get_device → 全量 profile 探测，
        # 实机上把应用列表的固定开销放大了数秒）
        try:
            online = {d.serial for d in adb.list_devices() if d.state == "device"}
        except (OSError, subprocess.SubprocessError):
            online = set()
        if serial not in online:
            return None
        packages_text = _shell(adb, serial, "pm", "list", "packages", "-3")
        if packages_text is None:
            return {"deviceId": device_id, "apps": []}
        package_names = [
            line.split(":", 1)[1].strip()
            for line in packages_text.splitlines()
            if line.startswith("package:") and len(line.split(":", 1)) == 2
        ]
        # 实机验证修复：单次 `ps -A` 建 pid 表，替代逐包 pidof（86 个三方包 =
        # 86 次 adb 往返 ≈ 10s+，是应用列表超时的直接原因）；顺带补全 R20 子进程
        proc_table = self._process_table(serial)
        foreground_name = self._foreground_name(serial)
        apps: list[dict[str, Any]] = []
        for package in package_names:
            pid = proc_table.get(package)
            children = [
                {"pid": p, "name": n, "isMain": False}
                for n, p in sorted(proc_table.items())
                if p is not None and n.startswith(package + ":")
            ]
            running = pid is not None or bool(children)
            apps.append(
                {
                    "packageName": package,
                    "name": package,
                    "pid": pid,
                    "running": running,
                    "isForeground": (package == foreground_name) if foreground_name else None,
                    "processes": (
                        [{"pid": pid, "name": package, "isMain": True}, *children]
                        if running
                        else []
                    ),
                }
            )
        return {"deviceId": device_id, "apps": apps}

    def _process_table(self, serial: str) -> dict[str, int]:
        """一次 `ps -A -o PID,NAME` → {进程名: pid}（同名取首个；解析失败为空表）。"""
        text = _shell(self._adb, serial, "ps", "-A", "-o", "PID,NAME", timeout=8.0)
        if not text:
            return {}
        table: dict[str, int] = {}
        for line in text.splitlines()[1:]:  # 首行是表头
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            try:
                table.setdefault(parts[1].strip(), int(parts[0]))
            except ValueError:
                continue
        return table

    def _foreground_name(self, serial: str) -> str | None:
        """topResumedActivity 行直接解析包名（一次 dumpsys；原 pid→ps -p 两跳省去）。

        实机验证修复（2026-11-03）：Android 16 真机（SM_F9560）dumpsys activity 无
        `topResumedActivity` 行，只有 `Resumed: ActivityRecord{… u0 <pkg>/…}`（模拟器
        两种行并存）——解析顺序 topResumedActivity 优先、`Resumed:` 行兜底，两者
        均取 `u0` 后首个 token 的 `/` 前段。
        """
        text = _shell(
            self._adb, serial, "dumpsys", "activity", "activities", timeout=8.0
        )
        if not text:
            return None
        return self._parse_foreground_name(text)

    @staticmethod
    def _parse_foreground_name(text: str) -> str | None:
        for key in ("topResumedActivity", "Resumed:"):
            for line in text.splitlines():
                if key not in line:
                    continue
                tokens = line.split()
                for i, token in enumerate(tokens):
                    if token == "u0" and i + 1 < len(tokens):
                        name = tokens[i + 1].split("/", 1)[0]
                        if name:
                            return name
        return None

    def list_metric_ids(self) -> list[str]:
        return available_metric_ids(self.mode)

    # _device_info 的 TTL（静态属性 + 慢变量；10s 内重复调用不产生 adb 往返）
    _INFO_TTL_S = 10.0

    def _device_info(self, serial: str) -> dict[str, Any]:
        """设备信息：批量探测（2 次 adb 往返）+ 10s TTL 缓存。

        实机验证修复（2026-11-02）：原实现 7 次 shell 往返/台（3×getprop + wm size +
        dumpsys battery + dumpsys display + nproc），设备列表首屏 2 台 ≈ 6s+，叠加
        采样中 adb 命令串行化后撞前端超时；现合并为 1 次全量 getprop + 1 次组合
        shell（nproc/wm size/battery/display），并按 serial 做 TTL 缓存。
        """
        with self._probe_lock:
            cached = self._info_cache.get(serial)
            if cached is not None and (time.monotonic() - cached[0]) < self._INFO_TTL_S:
                return cached[1]
        adb = self._adb
        parsed = self._parse_getprop(_shell(adb, serial, "getprop") or "")
        combined = _shell(
            adb, serial,
            # 实机验证修复（2026-11-03）：单 argv 元素直传（原 `sh -c` 多词脚本经
            # adb 拼接引号丢失，分号虽侥幸落到顶层执行成功，但属错误构造，统一修正）
            "nproc; echo __S__; wm size; echo __B__; dumpsys battery; echo __D__; dumpsys display",
            timeout=8.0,
        ) or ""
        cpu_count: int | None = None
        resolution_value: str | None = None
        battery_level: int | None = None
        refresh_hz: int | None = None
        section = "cpu"
        size_text = battery_text = display_text = ""
        for line in combined.splitlines():
            t = line.strip()
            if t == "__S__":
                section = "size"
                continue
            if t == "__B__":
                section = "battery"
                continue
            if t == "__D__":
                section = "display"
                continue
            if section == "cpu":
                if cpu_count is None and t.isdigit():
                    cpu_count = int(t)
            elif section == "size":
                size_text += line + "\n"
            elif section == "battery":
                battery_text += line + "\n"
            elif section == "display":
                display_text += line + "\n"
        if size_text:
            size_lines = [ln for ln in size_text.splitlines() if ln.strip()]
            if size_lines:
                resolution_value = (
                    size_lines[-1].split(":", 1)[-1].strip()
                    if "Override size" not in size_text
                    else size_lines[0].split(":", 1)[-1].strip()
                )
        if battery_text:
            for line in battery_text.splitlines():
                if line.strip().startswith("level:"):
                    try:
                        battery_level = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        battery_level = None
                    break
        if display_text:
            # 实机验证修复（2026-11-03）：真机（SM_F9560 Android 16）`fps=` 嵌在
            # DisplayDeviceInfo 复合行（supportedModes [{… fps=120.00001 …}]）内，
            # 原 `rsplit("=")` 取行尾值解析失败 → 恒 null。改为正则取 `fps=<float>`
            # 首个可解析值（真机 120 / 模拟器 60 实测通过）；无匹配仍 null 不猜。
            for line in display_text.splitlines():
                match = _DISPLAY_FPS_RE.search(line)
                if match:
                    try:
                        refresh_hz = int(float(match.group(1)))
                    except ValueError:
                        refresh_hz = None
                    else:
                        break
        model = parsed.get("ro.product.model")
        android_version = parsed.get("ro.build.version.release")
        abi = parsed.get("ro.product.cpu.abi")
        info = {
            "os": f"Android {android_version}" if android_version else None,
            "cpu": f"{cpu_count} cores" if cpu_count else None,
            "resolution": resolution_value,
            "batteryLevel": battery_level,
            "refreshRateHz": refresh_hz,
            "abi": abi,
        }
        with self._probe_lock:
            self._info_cache[serial] = (time.monotonic(), info)
        return info

    @staticmethod
    def _parse_getprop(dump: str) -> dict[str, str]:
        """`getprop` 全量输出（`[key]: [value]` 行）→ 字典；空输出为空表。"""
        out: dict[str, str] = {}
        for line in dump.splitlines():
            if ": " not in line:
                continue
            key, _, value = line.partition(": ")
            key = key.strip().strip("[]")
            value = value.strip().strip("[]")
            if key:
                out[key] = value
        return out


def detect_mode(adb: AdbClient | None = None) -> str:
    """auto 模式探测：adb 可用且有 ≥1 台在线设备 → real，否则 synthetic（H5，如实报告）。

    注意不复用 preflight.ok：其 ambiguous 规则面向「未指定 serial 的自动选择」，
    而本 API 的会话始终显式指定 deviceId，多设备在线不影响 per-device 会话。
    """
    client = adb or AdbClient()
    if not client.available():
        return "synthetic"
    try:
        devices = client.list_devices()
    except Exception:  # noqa: BLE001 - 探测异常按无设备处理
        return "synthetic"
    return "real" if any(device.state == "device" for device in devices) else "synthetic"
