"""SSE 事件总线（契约 §8）。

- 每个会话一个 SseBus：seq 会话内单调递增（从 1 起）；
- 事件环形缓冲 deque(maxlen=1000)，Last-Event-ID 断线重放，覆盖不足时补 gap 事件；
- 订阅者以 threading.Queue 扇出，SSE 端点桥接到 asyncio（多客户端并发）；
- 15s 注释行心跳与 60s ping 事件由端点/采集线程驱动，本模块不持时钟。
"""

from __future__ import annotations

import itertools
import json
import queue
import threading
import time
from collections import deque
from typing import Any

from .config import SSE_RING_CAPACITY


def now_ms() -> int:
    return int(time.time() * 1000)


class SseBus:
    """会话级事件总线：ring buffer + 扇出 + Last-Event-ID 重放。"""

    def __init__(self, session_id: str, capacity: int = SSE_RING_CAPACITY) -> None:
        self.session_id = session_id
        self._seq = itertools.count(1)
        self._ring: deque[tuple[int, dict[str, Any]]] = deque(maxlen=capacity)
        self._subscribers: list[queue.Queue[dict[str, Any]]] = []
        self._lock = threading.Lock()

    # ---- 发布 ----

    def publish(self, type_: str, **payload: Any) -> dict[str, Any]:
        """构造信封并入环、扇出；返回完整信封。"""
        envelope: dict[str, Any] = {
            "v": 1,
            "seq": next(self._seq),
            "ts": now_ms(),
            "type": type_,
            "sessionId": self.session_id,
        }
        envelope.update(payload)
        with self._lock:
            self._ring.append((envelope["seq"], envelope))
            subscribers = list(self._subscribers)
        for sub in subscribers:
            sub.put(envelope)
        return envelope

    # ---- 订阅 ----

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        q: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    # ---- Last-Event-ID 重放（契约 §8.1）----

    def replay(self, last_event_id: int) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """返回 (重放事件列表, gap 事件或 None)。

        - buffer 覆盖 (last_event_id, 当前]：原样重放；
        - buffer 最老 seq > last_event_id + 1：先发 gap {fromSeq,toSeq} 提示客户端
          全量拉 /metrics 校准，再重放现存事件。
        """
        with self._lock:
            events = [env for seq, env in self._ring if seq > last_event_id]
            oldest = self._ring[0][0] if self._ring else None
        gap: dict[str, Any] | None = None
        if events and oldest is not None and oldest > last_event_id + 1:
            gap = self.publish(
                "gap",
                fromSeq=last_event_id + 1,
                toSeq=oldest - 1,
            )
        return events, gap

    def latest_seq(self) -> int:
        with self._lock:
            return self._ring[-1][0] if self._ring else 0

    @staticmethod
    def format_event(envelope: dict[str, Any]) -> str:
        """单条 SSE 事件文本：event:/id:/data:（契约 §8.1）。"""
        data = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
        return f"event: {envelope['type']}\nid: {envelope['seq']}\ndata: {data}\n\n"

    @staticmethod
    def comment(text: str = " ping") -> str:
        """注释行心跳（15s 一条，不占 seq）。"""
        return f":{text}\n\n"
