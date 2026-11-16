"""SSE 实时通道（契约 §8）：信封/seq 单调/hello/心跳/Last-Event-ID 重放/gap。"""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from conftest import make_session


def _read_events(client: TestClient, url: str, headers: dict | None = None, count: int = 5,
                 timeout_s: float = 8.0) -> list[dict]:
    """从 SSE 流读取 count 个事件的信封（event:/data: 成对解析）。"""
    events: list[dict] = []
    started = time.monotonic()
    with client.stream("GET", url, headers=headers or {}) as resp:
        assert resp.status_code == 200
        content_type = resp.headers["content-type"]
        assert content_type.startswith("text/event-stream")
        assert resp.headers["cache-control"] == "no-cache"
        current_event = None
        for line in resp.iter_lines():
            if time.monotonic() - started > timeout_s:
                break
            if line is None:
                continue
            text = line.rstrip("\r")
            if text.startswith("event:"):
                current_event = text.removeprefix("event:").strip()
            elif text.startswith("data:"):
                envelope = json.loads(text.removeprefix("data:").strip())
                envelope["_sse_event"] = current_event
                events.append(envelope)
                if len(events) >= count:
                    break
            elif text.startswith(":"):
                continue  # 15s 注释行心跳；流内正常事件远快于此，不会等到
    return events


def test_sse_hello_first_and_envelope(client: TestClient) -> None:
    sid = make_session(client)["id"]
    events = _read_events(client, f"/api/sessions/{sid}/events", count=4)
    # hello 连接建立后首条（契约 §8.3）
    assert events[0]["type"] == "hello"
    assert events[0]["state"] == "monitoring"
    # 信封结构（契约 §8.2）：{v, seq, ts, type, sessionId}
    for env in events:
        assert env["v"] == 1
        assert env["type"] == env["_sse_event"]  # event: 字段 = type
        assert isinstance(env["seq"], int) and env["seq"] >= 1
        assert isinstance(env["ts"], int)
        assert env["sessionId"] == sid
    # seq 单调（重放+实时混合段亦然）
    seqs = [env["seq"] for env in events]
    assert seqs == sorted(seqs)


def test_sse_sample_message_shape(client: TestClient) -> None:
    """sample 消息与 perfeval SampleRecord{tsMs,metricId,value,tags} 同构（契约 §10）。"""
    sid = make_session(client)["id"]
    events = _read_events(
        client, f"/api/sessions/{sid}/events", count=10,
    )
    samples = [e for e in events if e["type"] == "sample"]
    assert samples, "60ms 间隔下应至少出现一个 sample"
    sample = samples[0]
    assert sample["metricId"] == "cpu.total.pct"
    assert isinstance(sample["value"], (int, float))
    assert sample["unit"] == "%"
    assert isinstance(sample["tags"], dict)
    assert isinstance(sample["tsMs"], int)  # SampleRecord 同构字段


def test_sse_session_state_broadcast(client: TestClient) -> None:
    sid = make_session(client)["id"]
    client.post(f"/api/sessions/{sid}/record/start")
    # 迁移发生在连接前 → 以显式 Last-Event-ID="0" 声明重放意图
    events = _read_events(
        client, f"/api/sessions/{sid}/events",
        headers={"Last-Event-ID": "0"}, count=8,
    )
    states = [e for e in events if e["type"] == "session.state"]
    assert states and states[-1]["state"] == "recording"
    phases = [e for e in events if e["type"] == "phase"]
    assert phases and phases[-1]["phase"] == "collection"


def test_sse_last_event_id_replay(client: TestClient) -> None:
    sid = make_session(client)["id"]
    # 第一段连接：取若干事件，记住最后 seq
    first = _read_events(client, f"/api/sessions/{sid}/events", count=6)
    last_seq = first[-1]["seq"]
    seen_types = {e["type"] for e in first}
    assert "sample" in seen_types
    time.sleep(0.15)  # 断线窗口内产生新事件
    # 重连：Last-Event-ID 重放缺失事件（契约 §8.1）
    replayed = _read_events(
        client, f"/api/sessions/{sid}/events",
        headers={"Last-Event-ID": str(last_seq)}, count=4,
    )
    assert replayed[0]["type"] == "hello"  # hello 仍是首条
    old = [e for e in replayed if e["seq"] > last_seq and e["seq"] != replayed[0]["seq"]]
    assert old, "重放应覆盖 (last_seq, 当前] 区间内的历史事件"
    # gap 仅在环形缓冲不覆盖时出现（短窗口测试不应出现）
    assert not any(e["type"] == "gap" for e in replayed)


def test_sse_gap_on_buffer_overflow(client: TestClient) -> None:
    """环形缓冲覆盖不足时发 gap 提示全量拉 /metrics 校准（契约 §8.1）。"""
    sid = make_session(client)["id"]
    # 通过 app.state 拿到 bus，伪造 1200 条历史事件把 1000 环冲掉
    app = client.app
    session = next(s for s in app.state.sessions.list() if s.id == sid)
    for _ in range(1200):
        session.bus.publish("sample", metricId="cpu.total.pct", value=1.0,
                            unit="%", tags={}, tsMs=0)
    replayed = _read_events(
        client, f"/api/sessions/{sid}/events",
        headers={"Last-Event-ID": "5"}, count=3,
    )
    assert replayed[0]["type"] == "hello"
    gap = [e for e in replayed if e["type"] == "gap"]
    assert gap and gap[0]["fromSeq"] == 6 and gap[0]["toSeq"] >= 6


def test_sse_unknown_type_passthrough(client: TestClient) -> None:
    """未知 type/未知字段原样透传；客户端按契约 §11.1 忽略（服务端透传无损）。"""
    sid = make_session(client)["id"]
    app = client.app
    session = next(s for s in app.state.sessions.list() if s.id == sid)
    session.bus.publish("some.future.type", extraField={"x": 1})
    events = _read_events(
        client, f"/api/sessions/{sid}/events",
        headers={"Last-Event-ID": "0"}, count=6,
    )
    future = [e for e in events if e["type"] == "some.future.type"]
    assert future and future[0]["extraField"] == {"x": 1}


def test_sse_events_404(client: TestClient) -> None:
    resp = client.get("/api/sessions/sess-99/events")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "session_not_found"


def test_sse_heartbeats(tmp_path) -> None:
    """15s 注释行心跳 + 60s ping 事件（契约 §8.1）——短窗口注入验证。"""
    from app.config import Settings
    from app.main import create_app
    from app.providers.synthetic import SyntheticProvider

    settings = Settings(
        provider="synthetic",
        runs_dir=tmp_path / "runs",
        sse_comment_heartbeat_s=0.3,
        sse_ping_heartbeat_s=0.8,
    )
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings, provider=SyntheticProvider())) as fast:
        body = fast.post("/api/sessions", json={
            "deviceId": "synthetic-001",
            "packageName": "com.example.synthetic.game",
            "metrics": [{"metricId": "cpu.total.pct", "intervalMs": 100}],
        }).json()
        sid = body["id"]
        comments: list[str] = []
        pings: list[dict] = []
        started = time.monotonic()
        with fast.stream("GET", f"/api/sessions/{sid}/events") as resp:
            assert resp.status_code == 200
            for line in resp.iter_lines():
                if line is None:
                    continue
                text = line.rstrip("\r")
                if text.startswith(":"):
                    comments.append(text.strip())
                elif text.startswith("data:"):
                    env = json.loads(text.removeprefix("data:").strip())
                    if env["type"] == "ping":
                        pings.append(env)
                        if len(pings) >= 2:
                            break
                if time.monotonic() - started > 10:
                    break
        assert comments, "应出现 ': ping' 注释行心跳"
        assert all(c == ": ping" for c in comments)
        assert len(pings) >= 2, "应出现周期性 type=ping 事件"
        for ping in pings:
            assert ping["v"] == 1 and ping["sessionId"] == sid
