import asyncio
import hashlib
import time

import pytest
from fastapi.testclient import TestClient

from argos_studio.app import Settings, create_app
from argos_studio.core import Store
from argos_studio.simulator import Simulator


@pytest.fixture
def settings(tmp_path):
    return Settings(
        data_dir=tmp_path,
        argos_root=None,
        argos_python=None,
        period_s=0.015,
        max_duration_s=3,
        dropout_duration_s=0.5,
    )


def wait_for(client, session_id, predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/sessions/{session_id}")
        assert response.status_code == 200, response.text
        snapshot = response.json()
        if predicate(snapshot):
            return snapshot
        time.sleep(0.025)
    pytest.fail("Timed out waiting for simulator state")


def start(client):
    response = client.post(
        "/api/sessions", json={"name": "Bench timing", "objective": "Observe gaps"}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_live_gap_annotation_stop_replay_and_restart(settings):
    with TestClient(create_app(settings)) as client:
        session_id = start(client)
        prefix = f"/api/sessions/{session_id}"
        snapshot = wait_for(client, session_id, lambda item: len(item["samples"]) >= 3)
        assert snapshot["live"]["freshness"] == "fresh"
        assert snapshot["session"]["source"] == "simulation"
        assert client.post("/api/sessions", json={"name": "second"}).status_code == 400
        note = client.post(
            prefix + "/annotations", json={"text": "Inspect interruption", "at_s": 0}
        )
        assert note.status_code == 201
        assert client.post(prefix + "/dropout", json={}).status_code == 200
        assert client.post(prefix + "/dropout", json={}).status_code == 400
        stale = wait_for(client, session_id, lambda item: item["live"]["freshness"] == "stale")
        assert stale["live"]["age_s"] >= 0.3
        wait_for(
            client,
            session_id,
            lambda item: any(event["kind"] == "dropout_ended" for event in item["events"]),
        )
        stop = client.post(prefix + "/stop", json={})
        assert stop.status_code == 200, stop.text
        assert stop.json()["status"] == "completed"
        assert client.post(prefix + "/stop", json={}).json() == stop.json()
        analysis = client.get(prefix + "/analysis").json()
        assert len(analysis["gaps"]) == 1
        gap = analysis["gaps"][0]
        assert gap["duration_s"] >= 0.5
        rows = client.get(prefix).json()["samples"]
        assert rows[gap["after_seq"]]["elapsed_s"] == gap["end_s"]
        window = client.get(prefix, params={"start_s": gap["start_s"], "end_s": gap["end_s"]})
        assert len(window.json()["samples"]) == 2
        assert client.post(prefix + "/dropout", json={}).status_code == 400
        exported = client.get(prefix + "/export")
        assert exported.json()["samples"] == rows
        assert "attachment" in exported.headers["content-disposition"]
    with TestClient(create_app(settings)) as reopened:
        snapshot = reopened.get(prefix).json()
        assert snapshot["session"]["status"] == "completed"
        assert snapshot["samples"] == rows
        assert snapshot["live"]["freshness"] == "offline"
        assert any(event["text"] == "Inspect interruption" for event in snapshot["events"])


def test_shutdown_and_recovery_never_resume_acquisition(settings):
    with TestClient(create_app(settings)) as client:
        session_id = start(client)
        wait_for(client, session_id, lambda item: len(item["samples"]) >= 2)
    store = Store(settings.data_dir / "studio.sqlite3")
    assert store.get_session(session_id)["status"] == "interrupted"
    abandoned = store.create_session("Lost process", "")
    with TestClient(create_app(settings)) as client:
        recovered = client.get(f"/api/sessions/{abandoned['id']}").json()
        assert recovered["session"]["status"] == "interrupted"
        assert recovered["session"]["ended_at"] is None
        assert not recovered["live"]["connected"]


def test_duration_limit_stops_source_and_allows_next_session(settings):
    settings.max_duration_s = 0.12
    with TestClient(create_app(settings)) as client:
        session_id = start(client)
        result = wait_for(client, session_id, lambda item: item["session"]["status"] == "completed")
        assert result["session"]["elapsed_s"] == 0.12
        assert any(event["kind"] == "duration_limit" for event in result["events"])
        assert start(client) != session_id


def test_separate_process_cannot_take_over_same_data_directory(settings):
    with TestClient(create_app(settings)):
        with pytest.raises(RuntimeError, match="already in use"):
            with TestClient(create_app(settings)):
                pass


def test_invalid_windows_annotations_and_unknown_sessions(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/sessions/missing").status_code == 404
        session_id = start(client)
        prefix = f"/api/sessions/{session_id}"
        assert client.get(prefix, params={"start_s": 2, "end_s": 1}).status_code == 400
        assert client.get(prefix + "/analysis?start_s=nan").status_code == 422
        assert (
            client.post(prefix + "/annotations", json={"text": " ", "at_s": 0}).status_code == 400
        )
        assert (
            client.post(prefix + "/annotations", json={"text": "Future", "at_s": 100}).status_code
            == 400
        )
        assert (
            client.post(prefix + "/annotations", json={"text": "Negative", "at_s": -1}).status_code
            == 422
        )
        assert (
            client.post("/api/sessions", json={"name": "Real", "source": "hardware"}).status_code
            == 422
        )


def test_browser_boundary_and_explicit_import_configuration(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/health").json()["argos_import"]["available"] is False
        assert (
            client.post(
                "/api/import/argos",
                content=b"data",
                headers={"content-type": "application/octet-stream"},
            ).status_code
            == 503
        )
        assert (
            client.post(
                "/api/sessions",
                json={"name": "CSRF"},
                headers={"origin": "https://unrelated.example"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/sessions",
                content='{"name":"wrong type"}',
                headers={"content-type": "text/plain"},
            ).status_code
            == 415
        )
        assert client.get("/api/health", headers={"host": "unrelated.example"}).status_code == 400
        assert "script-src 'self'" in client.get("/api/health").headers["content-security-policy"]


def test_import_is_replay_only_and_retains_original(settings, monkeypatch):
    import argos_studio.app as app_module

    raw = b"original validated recording"
    sample = {
        "source_time_s": 7,
        "elapsed_s": 1,
        "received_at": 44,
        "roll_deg": 12,
        "pitch_deg": 0,
        "gyro_x_deg_s": 0,
    }
    monkeypatch.setattr(Settings, "import_available", lambda _: True)
    monkeypatch.setattr(
        app_module,
        "read_argos_recording",
        lambda *args, **kwargs: {
            "samples": [sample],
            "duration_s": 5,
            "metadata": {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "environment": "unknown",
                "received_at_clock": "source_local_monotonic",
            },
        },
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/import/argos?filename=../../flight.jsonl",
            content=raw,
            headers={"content-type": "application/octet-stream"},
        )
        assert response.status_code == 201, response.text
        imported = response.json()
        assert imported["name"] == "flight.jsonl"
        assert imported["elapsed_s"] == 5
        prefix = f"/api/sessions/{imported['id']}"
        assert client.get(prefix + "/raw").content == raw
        assert client.post(prefix + "/dropout", json={}).status_code == 400
        assert client.post(prefix + "/stop", json={}).json()["status"] == "completed"
        assert client.get(prefix).json()["samples"][0]["received_at"] == 44
        assert (
            client.get(prefix + "/export").json()["session"]["metadata"]["sha256"]
            == hashlib.sha256(raw).hexdigest()
        )


def test_capacity_prevents_additional_sessions(settings, monkeypatch):
    import argos_studio.app as app_module

    monkeypatch.setattr(app_module, "MAX_SESSIONS", 1)
    with TestClient(create_app(settings)) as client:
        session_id = start(client)
        assert client.post(f"/api/sessions/{session_id}/stop", json={}).status_code == 200
        assert client.post("/api/sessions", json={"name": "Over capacity"}).status_code == 409


def test_stop_before_acquisition_task_has_started(tmp_path):
    async def exercise():
        runtime = Simulator(Store(tmp_path / "race.sqlite3"))
        session = await runtime.start("Immediate stop", "")
        stopped = await runtime.stop(session["id"])
        assert stopped["status"] == "completed"
        assert stopped["sample_count"] == 0
        next_session = await runtime.start("Another source", "")
        await runtime.stop(next_session["id"])

    asyncio.run(exercise())


def test_stop_during_dropout_cannot_resume(settings):
    with TestClient(create_app(settings)) as client:
        session_id = start(client)
        prefix = f"/api/sessions/{session_id}"
        wait_for(client, session_id, lambda item: len(item["samples"]) >= 2)
        assert client.post(prefix + "/dropout", json={}).status_code == 200
        stopped = client.post(prefix + "/stop", json={}).json()
        time.sleep(settings.dropout_duration_s + 0.05)
        snapshot = client.get(prefix).json()
        assert snapshot["session"]["status"] == "completed"
        assert snapshot["session"]["sample_count"] == stopped["sample_count"]
        assert not snapshot["live"]["connected"]
        assert not any(event["kind"] == "dropout_ended" for event in snapshot["events"])


def test_import_is_blocked_during_acquisition(settings, monkeypatch):
    monkeypatch.setattr(Settings, "import_available", lambda _: True)
    with TestClient(create_app(settings)) as client:
        start(client)
        response = client.post(
            "/api/import/argos",
            content=b"unused",
            headers={"content-type": "application/octet-stream"},
        )
        assert response.status_code == 409
