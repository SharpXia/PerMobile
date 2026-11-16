"""设备级命令队列（M6 R22，ARCH §4.1 多设备并发注记 / [P§5.4] 按设备 actor 隔离）。

一设备一线程/队列隔离：同一设备的 adb 命令串行执行，一设备阻塞不拖垮其他设备。
会话维度隔离由 SessionManager ``device_busy``（§3 既有码）保证——同一设备同时
只允许一个活跃会话，会话内采集（SamplingScheduler 单线程轮询）天然串行；本队列
覆盖**跨端点**的设备级命令互斥（如 wifi-mode 的 tcpip/connect 与会话采集并存）。

队列不吞异常：被包裹函数的异常原样上抛（调用方按各自契约转为错误体/诊断）。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class DeviceCommandQueue:
    """per-device 串行执行锁：``run_serially(device_id, fn)`` 独占该设备命令窗口。"""

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._registry_lock = threading.Lock()

    def _lock_for(self, device_id: str) -> threading.Lock:
        with self._registry_lock:
            lock = self._locks.get(device_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[device_id] = lock
            return lock

    def run_serially(self, device_id: str, fn: Callable[[], T]) -> T:
        """在 device_id 的串行窗口内执行 fn；同设备互斥、跨设备并行。"""
        lock = self._lock_for(device_id)
        with lock:
            return fn()
