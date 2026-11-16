"""网络测速纯函数层（R53 最小切片，契约 §24）。

口径（契约 §24 声明，这里只放常量与纯函数，I/O 在 provider/routers）：

- 测的是「宿主 → 设备单条 HTTP 连接」的下行吞吐，链路为
  设备 curl → adb reverse 隧道 → 本机后端 `/api/speedtest/payload`；
  受 USB 带宽与 adb 隧道开销限制，**不代表**设备公网/WiFi 网络吞吐，
  与 PerfDog 网络测试（弱网模拟/抓包/pcap）不可直接对比。
- 单位：`bytesPerSec`（B/s，设备侧 curl `-w %{speed_download}` 自报值）与
  `mbps`（十进制 Mbit/s = B/s × 8 / 1e6）；两者同源换算，不双算。
- null 语义（契约 §1 空值原则）：curl 执行失败/输出不可解析 → 503
  `speedtest_unavailable`（details.reason 区分阶段），不返回 0 填充的测速结果。
"""

from __future__ import annotations

from typing import Iterator

# 请求体大小边界（契约 §24）：缺省 4 MiB，上限 32 MiB（--max-time 内可完成）
SPEEDTEST_MIN_BYTES = 1024
SPEEDTEST_MAX_BYTES = 32 * 1024 * 1024
SPEEDTEST_DEFAULT_BYTES = 4 * 1024 * 1024

# 测速时长边界（秒）：curl --max-time；超时即失败（rc!=0），不出部分值
SPEEDTEST_MIN_TIME_S = 5
SPEEDTEST_MAX_TIME_S = 300
SPEEDTEST_DEFAULT_TIME_S = 60

# adb 往返在 curl --max-time 之外允许的额外余量（进程超时 > 命令超时）
SPEEDTEST_ADB_GRACE_S = 15

SPEEDTEST_METHOD = "adb.reverse.shell.curl"

# 恒带口径声明（对标 §12 smooth.note 恒带哲学：口径不可直接对比处必须声明）
SPEEDTEST_NOTE = (
    "adb reverse + 设备侧 curl 口径：宿主→设备单条 HTTP 连接的下行吞吐，"
    "经 USB 与 adb 隧道（受链路带宽与隧道开销限制），不代表设备公网/WiFi 吞吐，"
    "与 PerfDog 网络测试（弱网模拟/抓包/pcap）不可直接对比（契约 §24）"
)

# 测速负载：确定性 64 KiB 非零块重复（不伪造随机性，也避免全零便于压缩的传输层
# 干扰；curl 不带 Accept-Encoding，无压缩路径，字节内容不影响 B/s 口径）
_PATTERN_BLOCK = bytes(range(256)) * 256


def payload_chunks(total_bytes: int) -> Iterator[bytes]:
    """按 64 KiB 确定性块流式产出 total_bytes 字节（Content-Length 由调用方另设）。"""
    if total_bytes <= 0:
        raise ValueError(f"bytes 必须 > 0，收到 {total_bytes}")
    full, rem = divmod(total_bytes, len(_PATTERN_BLOCK))
    for _ in range(full):
        yield _PATTERN_BLOCK
    if rem:
        yield _PATTERN_BLOCK[:rem]


def parse_speed_download(stdout: str) -> float | None:
    """解析 `curl -w %{speed_download}` 输出（B/s；末个 token，容忍前缀噪声）。

    不可解析返回 None（不猜；由调用方归一 503 curl_output_unparseable）。
    """
    text = stdout.strip()
    if not text:
        return None
    try:
        value = float(text.split()[-1])
    except ValueError:
        return None
    return value if value >= 0 else None


def mbps_from_bytes_per_sec(bytes_per_sec: float) -> float:
    """B/s → 十进制 Mbit/s（×8/1e6），保留 3 位小数（契约 §24 单位换算）。"""
    return round(bytes_per_sec * 8 / 1_000_000, 3)


def curl_command(url: str, *, max_time_s: int) -> list[str]:
    """设备侧 curl 命令（单引号在设备 shell 剥离，保护 `%{}` 与 URL 中的 `?`）。"""
    return [
        "curl", "-s", "-o", "/dev/null", "-w", "'%{speed_download}'",
        "--max-time", str(max_time_s), f"'{url}'",
    ]


def payload_path(total_bytes: int) -> str:
    """后端测速负载路径（含 bytes 查询参数）。"""
    return f"/api/speedtest/payload?bytes={total_bytes}"
