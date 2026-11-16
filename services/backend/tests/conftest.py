"""后端测试夹具（H3）：固定 synthetic provider + 隔离 runs 目录。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.providers.synthetic import SyntheticProvider  # noqa: E402


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    """synthetic provider + 临时 runs 目录与 case 索引，测试互不污染。"""
    settings = Settings(
        provider="synthetic",
        runs_dir=tmp_path / "runs",
        cases_db_path=tmp_path / "data" / "cases.db",
    )
    app = create_app(settings, provider=SyntheticProvider())
    with TestClient(app) as c:
        c.runs_dir = settings.runs_dir  # type: ignore[attr-defined]
        c.cases_db_path = settings.cases_db_path  # type: ignore[attr-defined]
        yield c


def make_session(client: TestClient, **overrides: object) -> dict:
    body: dict = {
        "deviceId": "synthetic-001",
        "packageName": "com.example.synthetic.game",
        "metrics": [{"metricId": "cpu.total.pct", "intervalMs": 60}],
        "interactive": False,
        "launchOnCreate": False,
    }
    body.update(overrides)
    resp = client.post("/api/sessions", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()
