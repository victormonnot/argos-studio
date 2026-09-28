"""HTTP investigations preserve evidence and history across replay and restart."""

import json

import pytest
from fastapi.testclient import TestClient

from argos_studio.app import Settings, create_app
from argos_studio.core import MAX_INVESTIGATIONS


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, argos_root=None, argos_python=None)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as instance:
        yield instance


def sample(elapsed_s):
    return {
        "source_time_s": elapsed_s,
        "elapsed_s": elapsed_s,
        "received_at": 2000 + elapsed_s,
        "roll_deg": elapsed_s * 2,
        "pitch_deg": 0,
        "gyro_x_deg_s": 2,
    }


def finished_session(client, name="Recorded synthetic observation"):
    store = client.app.state.store
    session = store.create_session(
        name,
        "Inspect reception continuity",
        metadata={"environment": "simulation", "received_at_clock": "utc_epoch"},
    )
    store.append_samples(session["id"], [sample(t) for t in (0, 0.05, 1, 1.05)])
    return store.finish_session(session["id"], elapsed_s=1.5)


def investigate(client, session_id, **body):
    response = client.post(f"/api/sessions/{session_id}/investigations", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_investigation_detail_export_and_history_survive_restart(settings):
    with TestClient(create_app(settings)) as client:
        session = finished_session(client)
        prefix = f"/api/sessions/{session['id']}/investigations"
        result = investigate(
            client, session["id"], start_s=0.05, end_s=1, context="Investigate this interval"
        )
        detail_url = f"{prefix}/{result['id']}"
        assert result["schema_version"] == 1
        assert result["kind"] == "reception_quality"
        assert result["window_s"] == {"start_s": 0.05, "end_s": 1}
        assert result["snapshot"]["sample_count"] == session["sample_count"]
        assert result["snapshot"]["source"] == "simulation"
        assert result["snapshot"]["status"] == "completed"
        assert len(result["snapshot"]["sha256"]) == 64
        evidence_ids = {item["id"] for item in result["evidence"]}
        assert result["findings"]
        assert all(set(item["evidence"]) <= evidence_ids for item in result["findings"])
        assert client.get(detail_url).json() == result
        exported = client.get(detail_url + "/export")
        assert exported.status_code == 200
        assert exported.json() == result
        assert exported.headers["content-type"].startswith("application/json")
        assert f"argos-investigation-{result['id']}.json" in exported.headers["content-disposition"]
        summaries = client.get(prefix).json()
        assert len(summaries) == 1
        assert summaries[0]["id"] == result["id"]
        assert summaries[0]["context"] == "Investigate this interval"
        assert "evidence" not in summaries[0]
        assert client.get("/api/health").json()["investigation"]["uses_llm"] is False
    with TestClient(create_app(settings)) as reopened:
        assert reopened.get(detail_url).json() == result
        assert reopened.get(detail_url + "/export").json() == result
        assert reopened.get(prefix).json() == summaries


def test_reports_and_exports_are_isolated_by_session(client):
    first = finished_session(client, "First")
    second = finished_session(client, "Second")
    saved = investigate(client, first["id"])
    other_prefix = f"/api/sessions/{second['id']}/investigations"
    assert client.get(other_prefix).json() == []
    assert client.get(f"{other_prefix}/{saved['id']}").status_code == 404
    assert client.get(f"{other_prefix}/{saved['id']}/export").status_code == 404
    known_prefix = f"/api/sessions/{first['id']}/investigations"
    assert client.get(known_prefix + "/missing").status_code == 404
    assert client.get(known_prefix + "/missing/export").status_code == 404
    missing_prefix = "/api/sessions/missing/investigations"
    assert client.get(missing_prefix).status_code == 404
    assert client.post(missing_prefix, json={}).status_code == 404
    assert client.get(missing_prefix + f"/{saved['id']}").status_code == 404
    assert client.get(missing_prefix + f"/{saved['id']}/export").status_code == 404


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"unexpected": True}, 422),
        ({"start_s": -0.1}, 422),
        ({"context": "x" * 2001}, 422),
        ({"start_s": 1.1, "end_s": 0.9}, 400),
        ({"end_s": 1.6}, 400),
        ({"start_s": 1.6}, 400),
        ({"start_s": 2, "end_s": 3}, 400),
    ],
)
def test_invalid_investigation_requests_do_not_create_reports(client, body, status):
    session = finished_session(client)
    prefix = f"/api/sessions/{session['id']}/investigations"
    response = client.post(prefix, json=body)
    assert response.status_code == status, response.text
    assert client.get(prefix).json() == []


@pytest.mark.parametrize("field", ["start_s", "end_s"])
@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_nonfinite_investigation_bounds_return_validation_errors(client, field, value):
    session = finished_session(client)
    prefix = f"/api/sessions/{session['id']}/investigations"
    response = client.post(
        prefix,
        content=f'{{"{field}": {value}}}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422, response.text
    assert client.get(prefix).json() == []


def test_cross_origin_investigation_request_is_rejected(client):
    session = finished_session(client)
    prefix = f"/api/sessions/{session['id']}/investigations"
    response = client.post(prefix, json={}, headers={"Origin": "https://elsewhere.example"})
    assert response.status_code == 403
    assert client.get(prefix).json() == []


def test_report_capacity_is_enforced_through_http_without_overwriting_history(client):
    session = finished_session(client)
    prefix = f"/api/sessions/{session['id']}/investigations"
    saved = [investigate(client, session["id"]) for _ in range(MAX_INVESTIGATIONS)]
    assert client.post(prefix, json={}).status_code == 400
    summaries = client.get(prefix).json()
    assert len(summaries) == MAX_INVESTIGATIONS
    assert {item["id"] for item in summaries} == {item["id"] for item in saved}
    assert client.get(prefix + f"/{saved[0]['id']}").json() == saved[0]
    assert (
        client.get("/api/health").json()["limits"]["max_investigations_per_session"]
        == MAX_INVESTIGATIONS
    )


def test_annotation_and_corrected_context_require_new_reports(client):
    session = finished_session(client)
    prefix = f"/api/sessions/{session['id']}"
    first = investigate(client, session["id"], context="Initial context")
    first_url = prefix + f"/investigations/{first['id']}"
    # Context is a retained human observation, not an instruction to the engine.
    changed_context = "Ignore earlier instructions; context remains unverified text."
    second = investigate(client, session["id"], context=changed_context)
    assert second["id"] != first["id"]
    assert second["context"] == changed_context
    assert second["snapshot"]["sha256"] == first["snapshot"]["sha256"]
    assert second["findings"] == first["findings"]

    annotation = client.post(
        prefix + "/annotations", json={"text": "A new replay observation", "at_s": 0.5}
    )
    assert annotation.status_code == 201
    assert client.get(first_url).json() == first
    third = investigate(client, session["id"], context="Context after the observation")
    assert third["snapshot"]["sha256"] != first["snapshot"]["sha256"]
    assert third["snapshot"]["event_count"] == first["snapshot"]["event_count"] + 1
    assert third["snapshot"]["sample_count"] == first["snapshot"]["sample_count"]
    assert client.get(first_url + "/export").json() == first
    assert len(client.get(prefix + "/investigations").json()) == 3


def test_live_report_remains_frozen_after_new_samples_and_session_stop(client):
    store = client.app.state.store
    session = store.create_session("Live synthetic evidence", "Observe continuity")
    store.append_samples(session["id"], [sample(0), sample(0.05)])
    first = investigate(client, session["id"], context="During acquisition")
    detail_url = f"/api/sessions/{session['id']}/investigations/{first['id']}"
    assert first["snapshot"]["status"] == "live"
    assert first["snapshot"]["sample_count"] == 2
    assert first["window_s"]["end_s"] == 0.05

    store.append_sample(session["id"], **sample(1))
    store.finish_session(session["id"], elapsed_s=1.2)
    assert client.get(detail_url).json() == first
    assert client.get(detail_url + "/export").json() == first
    current = investigate(client, session["id"], context="After capture")
    assert current["snapshot"]["status"] == "completed"
    assert current["snapshot"]["sample_count"] == 3
    assert current["snapshot"]["sha256"] != first["snapshot"]["sha256"]
    assert current["window_s"]["end_s"] == 1.2
    json.dumps(current, allow_nan=False)
