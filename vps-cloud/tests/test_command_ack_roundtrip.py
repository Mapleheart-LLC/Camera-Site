from pathlib import Path
import json
import sys

from fastapi.testclient import TestClient
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import get_db_connection
from main import app, init_db


def _latest_event_by_command_id(command_id: str) -> dict:
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT id, event, reason, payload_json
            FROM tpe_events
            WHERE json_extract(payload_json, '$.command_id') = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (command_id,),
        ).fetchone()
    assert row is not None, f"No event found for command_id={command_id}"
    return dict(row)


@pytest.fixture(scope="module")
def client() -> TestClient:
    init_db()
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_mdm_executed_command_id_roundtrip(client: TestClient) -> None:
    command_id = "itest-executed-001"
    payload = {
        "event": "mdm_executed",
        "command": "LOCK_DEVICE",
        "command_id": command_id,
        "timestamp": 1712345678000,
    }

    response = client.post("/api/tpe/webhook", json=payload)
    assert response.status_code == 200

    row = _latest_event_by_command_id(command_id)
    assert row["event"] == "mdm_executed"
    body = json.loads(row["payload_json"] or "{}")
    assert body.get("command_id") == command_id
    assert body.get("command") == "LOCK_DEVICE"


def test_mdm_failed_command_id_roundtrip(client: TestClient) -> None:
    command_id = "itest-failed-001"
    payload = {
        "event": "mdm_failed",
        "command": "FORCE_STOP_APP",
        "command_id": command_id,
        "status": "failed",
        "reason": "Root unavailable",
        "timestamp": 1712345679000,
    }

    response = client.post("/api/tpe/webhook", json=payload)
    assert response.status_code == 200

    row = _latest_event_by_command_id(command_id)
    assert row["event"] == "mdm_failed"
    body = json.loads(row["payload_json"] or "{}")
    assert body.get("command_id") == command_id
    assert body.get("command") == "FORCE_STOP_APP"
    assert body.get("status") == "failed"
    assert body.get("reason") == "Root unavailable"
