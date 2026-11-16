"""GET /api/health（H2 + 契约 §2）：200 与响应 schema。"""

from __future__ import annotations

import platform

from fastapi.testclient import TestClient


def test_health_ok(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == "1.0.0"
    assert body["backend"]["framework"] == "fastapi"  # H2/ARCH §6.1 如实报告
    assert body["backend"]["python"] == platform.python_version()
    # synthetic fixture 下 providers 如实为 synthetic（H5）
    assert body["providers"] == {"devices": "synthetic", "metrics": "synthetic"}
    assert isinstance(body["uptimeSec"], int) and body["uptimeSec"] >= 0


def test_health_uptime_monotonic(client: TestClient) -> None:
    first = client.get("/api/health").json()["uptimeSec"]
    second = client.get("/api/health").json()["uptimeSec"]
    assert second >= first


def test_health_matches_contract_example(client: TestClient) -> None:
    """契约 §2 示例 JSON 字段结构对拍（值可不同，结构一致，允许附加字段 §11.1）。"""
    body = client.get("/api/health").json()
    assert set(body) >= {"status", "version", "backend", "providers", "uptimeSec"}
    assert set(body["backend"]) >= {"framework", "python"}
    assert set(body["providers"]) == {"devices", "metrics"}
