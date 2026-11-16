"""M6 测试 fixture（契约 §20/§21）：fake adb 传输层 + dumpsys 语料。

如实标注：真机路径（kgsl sysfs 存在性、/sys cpufreq、/proc task 遍历、logcat 流、
adb tcpip/connect 的真实 I/O）无设备不可测；本模块以 fake adb 应答表驱动**生产
代码**（RealProvider 探测、cpu_advanced 采样器、MemDetailSampler、wifi 命令构
造），只替身传输层——命令序列断言覆盖「单测用 mock adb 命令断言命令序列」。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

# ---- dumpsys meminfo 明细段语料（R17 golden 源；多列 PSS 表 + App Summary 段）----

MEMINFO_DETAIL_FIXTURE = """\
Applications Memory Usage (in Kilobytes):
Uptime: 123456789 Realtime: 234567890

** MEMINFO in pid 12345 [com.example.synthetic.game] **
                   Pss  Private  Private  SwapPss      Rss     Heap     Heap     Heap
                 Total    Dirty    Clean    Dirty    Total     Size    Alloc      Free
               ------   ------   ------   ------   ------   ------   ------   ------
  Native Heap    47,104   46,880        0        0   48,200   51,200   48,100    3,100
  Dalvik Heap    25,344   24,980        0        0   26,100   33,560   28,900    4,660
 Dalvik Other     4,096    4,080        0        0    5,120
        Stack       256      256        0        0      268
       Ashmem        128      112        0        0      640
      Gfx dev     8,192    8,120        0        0    8,300
   EGL mtrack    12,400   12,360        0        0   12,700
    .so mmap      3,120    1,200      800        0   12,400
    .dex mmap     1,536        0    1,400        0    2,048
    GL mtrack     6,144    6,000        0        0    6,300
  Other mmap        240      128       64        0    3,400
     Unknown     1,024      984       12        0    1,300
       TOTAL    99,584   94,600    2,900        0  121,000   84,760   77,000    7,760

 App Summary
                       Pss Kb
                    ------
           Java Heap:   26,000
         Native Heap:   46,880
                Code:    4,100
               Stack:      256
            Graphics:   26,480
       Private Other:   10,200
              System:   -1,500
               TOTAL:   99,584      TOTAL RSS:  121,000      TOTAL PSS:   99,584
 Objects
               Views:        0        ViewRootImpl:        0
"""

# 部分机型形态：无 Gfx dev / EGL mtrack / GL mtrack（契约 §21.3「部分机型 meminfo
# 无明细段」；AOSP 对全零 OTHER_STATS 行整行不打印——ActivityThread.dumpMemInfoTable
# 非零跳过循环，mem-2 的探测假阴性通道即源于此）
MEMINFO_DETAIL_PARTIAL_FIXTURE = """\
** MEMINFO in pid 12346 [com.example.synthetic.app] **
                 Total    Dirty    Clean
  Native Heap     8,192    8,000        0
  Dalvik Heap     6,144    6,000        0
 Dalvik Other       512      480        0
       TOTAL    14,848   14,480        0

 App Summary
           Java Heap:    6,000
         Native Heap:    8,000
"""

# R17 golden 四明细项（KB→MB：/1024，与 perfeval meminfo.py._kb_to_mb 同式）。
# 语料为真实 AOSP 明细段标签形态（mem-1 修复 2026-11-06：明细段从不打印
# "Graphics" 行——图形三行恒为 Gfx dev/EGL mtrack/GL mtrack；gfx_mb = Gfx dev
# + EGL mtrack，GL mtrack 归 gl_mb 单列避免组内双计）
MEMINFO_DETAIL_GOLDEN_MB = {
    "native_mb": 46.0,      # 47,104 KB
    "dalvik_mb": 28.75,     # 25,344 + 4,096 KB（Dalvik Heap + Dalvik Other）
    "gfx_mb": 20.109,       # 8,192 + 12,400 KB（Gfx dev + EGL mtrack）
    "gl_mb": 6.0,           # 6,144 KB（GL mtrack）
}


def completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> Any:
    result = SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)
    return result


class FakeAdb:
    """RealProvider/采样器的 fake adb 传输层：命令序列记录 + 应答表。

    - ``commands``：逐条记录 (args..., serial)（wifi-mode/探测的命令序列断言源）；
    - ``responses``：匹配函数 → stdout（首个命中者生效；未命中回空串 rc=0）。
    """

    def __init__(self, responses: list[tuple[Any, str]] | None = None) -> None:
        self.commands: list[list[str]] = []
        self.serials: list[str | None] = []
        self.responses = responses or []

    def available(self) -> bool:
        return True

    def list_devices(self) -> list[Any]:
        """AdbClient.list_devices 替身（list_apps 的轻量在线检查用）。"""
        return [SimpleNamespace(serial="R3CN80ABCDEF", state="device")]

    def command(self, *args: str, serial: str | None = None) -> list[str]:
        return ["adb", *(["-s", serial] if serial else []), *args]

    def run(self, *args: str, serial: str | None = None, timeout: float | None = None) -> Any:
        self.commands.append([*args, serial])
        self.serials.append(serial)
        joined = " ".join(args)
        for matcher, stdout in self.responses:
            if matcher(joined):
                return completed(stdout)
        return completed("")

    # ---- 应答表条目工厂 ----

    @staticmethod
    def when(token: str, stdout: str) -> tuple[Any, str]:
        return (lambda joined, t=token: t in joined), stdout


def default_probe_responses(
    *,
    kgsl: bool = True,
    meminfo_detail: str = MEMINFO_DETAIL_FIXTURE,
    cores: int = 8,
) -> list[tuple[Any, str]]:
    """RealProvider profile 探测的标准应答表（机型特性 fixture）。

    实机验证修复对齐（2026-11-02）：_device_info 批量化后发 1 次全量 `getprop`
    + 1 次组合 shell（nproc/wm size/battery/display，以 __S__ 标记匹配）；
    list_apps 发单次 `ps -A -o PID,NAME`（不再逐包 pidof）。
    """
    return [
        FakeAdb.when(
            "getprop",
            "[ro.product.model]: [Pixel 8]\n"
            "[ro.build.version.release]: [14]\n"
            "[ro.product.cpu.abi]: [arm64-v8a]\n",
        ),
        FakeAdb.when(
            "__S__",
            f"{cores}\n"
            "__S__\n"
            "Physical size: 1080x2400\n"
            "__B__\n"
            "level: 87\nstatus: 2\n"
            "__D__\n"
            "mDefaultModeId=1 fps=120.0\n",
        ),
        # 2026-11-11 探测改 cat gpubusy 对齐数据面（小米 2405CPX3DC 拒目录列允文件读，
        # ls 形态在该机恒空 → gpuSource false-negative）；GPU 空闲时值形如 "0 0" 仍非空
        FakeAdb.when("cat /sys/class/kgsl/kgsl-3d0/gpubusy", "0 0\n" if kgsl else ""),
        FakeAdb.when("dumpsys meminfo system", meminfo_detail),
        FakeAdb.when("pm list packages", "package:com.example.game\n"),
        FakeAdb.when(
            "dumpsys activity",
            "topResumedActivity=ActivityRecord{ u0 com.example.game/.Main T1}\n",
        ),
        FakeAdb.when(
            "ps -A",
            "  PID NAME\n"
            "  1234 com.example.game\n"
            "  1300 com.example.game:push\n",
        ),
        FakeAdb.when("tcpip", ""),
        FakeAdb.when("connect", "connected to 192.168.1.23:5555\n"),
    ]


def fake_preflight_result(serial: str = "R3CN80ABCDEF") -> Any:
    """run_preflight 返回值替身（list_devices 的枚举源；monkeypatch 注入）。"""
    device = SimpleNamespace(
        serial=serial, state="device", model="Pixel 8",
        device="shiba", product="shiba",
    )
    wifi_device = SimpleNamespace(
        serial="192.168.1.23:5555", state="device", model="Pixel 8",
        device="shiba", product="shiba",
    )
    return SimpleNamespace(devices=[device, wifi_device], diagnostics=[])
