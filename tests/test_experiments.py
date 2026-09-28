"""The synthetic executor consumes a proposal once and preserves partial evidence."""

import asyncio
import time
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from argos_studio.acquisition import Acquisition
from argos_studio.app import Settings, create_app
from argos_studio.core import Store
from argos_studio.experiments import ExperimentRunner, SyntheticProtocol
from argos_studio.investigation import investigate

FAST = SyntheticProtocol(
    phase_duration_s=1.2, dropout_at_s=0.3, dropout_duration_s=0.4, max_wall_duration_s=5
)


def reference(store, source="simulation"):
    sid = store.create_session("Synthetic reference", "Inspect reception", source=source)["id"]
    for t in [0, 0.05, 0.55, 0.6]:
        store.append_sample(
            sid,
            source_time_s=t,
            elapsed_s=t,
            received_at=2000 + t,
            roll_deg=0,
            pitch_deg=0,
            gyro_x_deg_s=0,
        )
    store.finish_session(sid, elapsed_s=0.6)
    return sid, investigate(store, sid)


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, argos_root=None, argos_python=None, experiment_protocol=FAST)


def prepare(client, *, source="simulation"):
    sid, report = reference(client.app.state.store, source)
    response = client.post(
        f"/api/sessions/{sid}/investigations/{report['id']}/experiments", json={}
    )
    assert response.status_code == 201, response.text
    return response.json(), report


def wait_for(client, eid, predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get(f"/api/experiments/{eid}").json()
        if predicate(data):
            return data
        time.sleep(0.02)
    pytest.fail(f"Timed out: {data}")


def test_complete_two_real_synthetic_captures_export_and_restart(settings):
    with TestClient(create_app(settings)) as client:
        experiment, report = prepare(client)
        eid = experiment["id"]
        prefix = f"/api/experiments/{eid}"
        assert experiment["status"] == "proposed"
        assert experiment["plan"] == asdict(FAST)
        assert len(client.get("/api/sessions").json()) == 1
        assert experiment["control_session_id"] is None
        assert client.get(prefix + "/export").status_code == 409
        assert client.post(prefix + "/start", json={}).status_code == 202
        assert client.post(prefix + "/start", json={}).status_code == 409
        completed = wait_for(client, eid, lambda item: item["status"] != "running")
        assert completed["status"] == "completed", completed
        assert completed["result"]["outcome"] == "supported", completed["result"]
        assert completed["result"]["reference"]["report_id"] == report["id"]
        assert completed["result"]["control"]["gap_count"] == 0
        assert completed["result"]["perturbed"]["gap_count"] == 1
        assert completed["result"]["intervention"]["gap"]["duration_s"] >= 0.4
        for role in ("control", "perturbed"):
            sid = completed[f"{role}_session_id"]
            snapshot = client.get(f"/api/sessions/{sid}").json()
            assert snapshot["session"]["source"] == "simulation"
            assert snapshot["session"]["metadata"]["experiment_id"] == eid
            assert snapshot["session"]["metadata"]["experiment_role"] == role
            assert snapshot["session"]["status"] == "completed"
            assert not snapshot["live"]["connected"]
        exported = client.get(prefix + "/export")
        assert exported.json() == completed
        assert "attachment" in exported.headers["content-disposition"]
        assert client.post(prefix + "/cancel", json={}).json() == completed
        assert client.post(prefix + "/start", json={}).status_code == 409
        assert client.get("/api/health").json()["experiment"]["active_id"] is None
    with TestClient(create_app(settings)) as client:
        assert client.get(prefix).json() == completed
        assert client.get(prefix + "/export").json() == completed
        assert client.get(
            "/api/experiments", params={"session_id": experiment["origin_session_id"]}
        ).json() == [completed]
        assert len(client.get("/api/sessions").json()) == 3


def test_cancel_proposal_has_no_acquisition_and_is_not_replayable(settings):
    with TestClient(create_app(settings)) as client:
        exp, _ = prepare(client)
        prefix = f"/api/experiments/{exp['id']}"
        cancelled = client.post(prefix + "/cancel", json={}).json()
        assert cancelled["status"] == "cancelled"
        assert cancelled["started_at"] is None
        assert len(client.get("/api/sessions").json()) == 1
        assert client.post(prefix + "/start", json={}).status_code == 409


@pytest.mark.parametrize("second_phase", [False, True])
def test_cancel_active_capture_and_child_stop_preserve_partial_samples(settings, second_phase):
    with TestClient(create_app(settings)) as client:
        exp, _ = prepare(client)
        prefix = f"/api/experiments/{exp['id']}"
        client.post(prefix + "/start", json={})
        role = "perturbed" if second_phase else "control"
        running = wait_for(client, exp["id"], lambda item: item[f"{role}_session_id"] is not None)
        sid = running[f"{role}_session_id"]
        assert client.post(f"/api/sessions/{sid}/dropout", json={}).status_code == 409
        assert client.post("/api/sessions", json={"name": "Concurrent"}).status_code == 409
        response = client.post(
            f"/api/sessions/{sid}/stop" if second_phase else prefix + "/cancel", json={}
        )
        assert response.status_code == 200, response.text
        cancelled = client.get(prefix).json()
        assert cancelled["status"] == "cancelled"
        assert cancelled["result"] is None
        snap = client.get(f"/api/sessions/{sid}").json()
        assert snap["session"]["status"] == "interrupted"
        assert snap["samples"]
        if not second_phase:
            assert cancelled["perturbed_session_id"] is None
        count = len(snap["samples"])
        time.sleep(0.08)
        assert len(client.get(f"/api/sessions/{sid}").json()["samples"]) == count
        assert client.post(prefix + "/start", json={}).status_code == 409
        assert client.post("/api/sessions", json={"name": "Following capture"}).status_code == 201


def test_shutdown_and_crash_recovery_never_resume_an_experiment(settings):
    with TestClient(create_app(settings)) as client:
        exp, _ = prepare(client)
        client.post(f"/api/experiments/{exp['id']}/start", json={})
        active = wait_for(client, exp["id"], lambda item: item["control_session_id"] is not None)
    with TestClient(create_app(settings)) as client:
        assert client.get(f"/api/experiments/{exp['id']}").json()["status"] == "interrupted"
        assert (
            client.get(f"/api/sessions/{active['control_session_id']}").json()["session"]["status"]
            == "interrupted"
        )
        pending, _ = prepare(client)
        # Persist exactly the state a lost process could leave between captures.
        store = client.app.state.store
        store.claim_experiment(pending["id"])
        child = store.create_experiment_session(pending["id"], "control", "Lost capture", "", {})
    with TestClient(create_app(settings)) as client:
        recovered = client.get(f"/api/experiments/{pending['id']}").json()
        assert recovered["status"] == "interrupted"
        assert recovered["perturbed_session_id"] is None
        assert (
            client.get(f"/api/sessions/{child['id']}").json()["session"]["status"] == "interrupted"
        )
        assert client.get("/api/health").json()["experiment"]["active_id"] is None


def test_http_boundary_unsupported_sources_and_modified_protocol_are_rejected(settings):
    with TestClient(create_app(settings)) as client:
        exp, report = prepare(client)
        path = f"/api/sessions/{exp['origin_session_id']}/investigations/{report['id']}/experiments"
        assert client.post(path, json={"source": "mavlink-udp"}).status_code == 422
        assert (
            client.post(path, json={}, headers={"origin": "https://unrelated.example"}).status_code
            == 403
        )
        assert (
            client.post(f"/api/experiments/{exp['id']}/start", json={"duration_s": 100}).status_code
            == 422
        )
        sid, native = reference(client.app.state.store, "argos-recording")
        assert (
            client.post(
                f"/api/sessions/{sid}/investigations/{native['id']}/experiments", json={}
            ).status_code
            == 400
        )
        assert (
            client.post(
                f"/api/sessions/{sid}/investigations/{report['id']}/experiments", json={}
            ).status_code
            == 404
        )
        assert client.get("/api/experiments/missing").status_code == 404
        assert client.post("/api/experiments/missing/start", json={}).status_code == 404
        changed = {**asdict(FAST), "dropout_duration_s": 100}
        invalid = client.app.state.store.create_experiment(
            exp["origin_session_id"], report["id"], changed
        )
        assert client.post(f"/api/experiments/{invalid['id']}/start", json={}).status_code == 409
        assert client.get(f"/api/experiments/{invalid['id']}").json()["status"] == "proposed"


def test_expired_proposal_and_session_capacity_do_not_start_sources(settings, monkeypatch):
    with TestClient(create_app(settings)) as client:
        exp, report = prepare(client)
        store = client.app.state.store
        expired = store.create_experiment(
            exp["origin_session_id"], report["id"], asdict(FAST), ttl_s=0.001
        )
        time.sleep(0.005)
        assert client.post(f"/api/experiments/{expired['id']}/start", json={}).status_code == 409
        assert client.get(f"/api/experiments/{expired['id']}").json()["status"] == "expired"
        monkeypatch.setattr(client.app.state.experiments, "max_sessions", 2)
        assert client.post(f"/api/experiments/{exp['id']}/start", json={}).status_code == 409
        assert len(client.get("/api/sessions").json()) == 1


def runner_for(store, protocol=FAST):
    runtime = Acquisition(store, period_s=0.05, max_duration_s=180, dropout_duration_s=2)
    return ExperimentRunner(store, runtime, asyncio.Lock(), protocol=protocol)


def test_immediate_cancel_and_concurrent_launch_only_consume_one_proposal(tmp_path):
    async def scenario():
        store = Store(tmp_path / "immediate.sqlite3")
        sid, report = reference(store)
        runner = runner_for(store)
        first, second = [runner.propose(sid, report["id"]) for _ in range(2)]
        await runner.start(first["id"])
        # No event loop yield between start and cancellation: the task may never enter _run.
        assert (await runner.cancel(first["id"]))["status"] == "cancelled"
        assert len(store.list_sessions()) == 1
        third = runner.propose(sid, report["id"])
        results = await asyncio.gather(
            runner.start(second["id"]), runner.start(third["id"]), return_exceptions=True
        )
        assert sum(isinstance(result, ValueError) for result in results) == 1
        await runner.cancel(runner.experiment_id)
        assert not runner.runtime.active

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_point", ["sample", "start_event"])
def test_source_failure_does_not_leave_live_or_unlinked_sessions(
    tmp_path, monkeypatch, failure_point
):
    async def scenario():
        store = Store(tmp_path / "failure.sqlite3")
        sid, report = reference(store)
        runner = runner_for(store)
        exp = runner.propose(sid, report["id"])

        def fail(*args, **kwargs):
            raise RuntimeError("Injected persistence failure")

        method = "append_sample" if failure_point == "sample" else "add_event"
        monkeypatch.setattr(store, method, fail)
        await runner.start(exp["id"])
        await runner.task
        failed = store.get_experiment(exp["id"])
        assert failed["status"] == "failed"
        assert failed["control_session_id"] is not None
        assert failed["perturbed_session_id"] is None
        assert all(item["status"] != "live" for item in store.list_sessions())
        assert not runner.runtime.active

    asyncio.run(scenario())


def test_deadline_stops_a_source_even_when_orchestration_stalls(tmp_path, monkeypatch):
    async def scenario():
        store = Store(tmp_path / "timeout.sqlite3")
        sid, report = reference(store)
        protocol = SyntheticProtocol(
            phase_duration_s=0.8, dropout_at_s=0.2, dropout_duration_s=0.3, max_wall_duration_s=1.7
        )
        runner = runner_for(store, protocol)
        exp = runner.propose(sid, report["id"])

        async def stalled(eid, role):
            await runner.source.start(
                "Timeout fixture", "", experiment_id=eid, experiment_role=role
            )
            await asyncio.Event().wait()

        monkeypatch.setattr(runner, "_phase", stalled)
        await runner.start(exp["id"])
        await asyncio.wait_for(runner.task, timeout=3)
        result = store.get_experiment(exp["id"])
        assert result["status"] == "failed"
        assert "Délai" in result["error"]
        assert not runner.runtime.active
        assert result["perturbed_session_id"] is None

    asyncio.run(scenario())


def test_failed_stop_event_cannot_keep_the_generator_running(tmp_path, monkeypatch):
    async def scenario():
        store = Store(tmp_path / "failed-stop.sqlite3")
        sid, report = reference(store)
        runner = runner_for(store)
        exp = runner.propose(sid, report["id"])
        await runner.start(exp["id"])
        while runner.source.last_received is None:
            await asyncio.sleep(0.01)
        add_event = store.add_event

        def fail_stop(session_id, kind, text, at_s):
            if kind == "source_stopped":
                raise RuntimeError("Injected stop-event failure")
            return add_event(session_id, kind, text, at_s)

        monkeypatch.setattr(store, "add_event", fail_stop)
        result = await runner.cancel(exp["id"])
        assert result["status"] == "failed"
        child = result["control_session_id"]
        assert store.get_session(child)["status"] == "interrupted"
        assert not runner.runtime.active
        before = store.samples(child)
        await asyncio.sleep(0.08)
        assert store.samples(child) == before
        assert result["perturbed_session_id"] is None

    asyncio.run(scenario())


def test_acquisition_stays_reserved_between_the_two_phases(settings, monkeypatch):
    with TestClient(create_app(settings)) as client:
        exp, _ = prepare(client)
        runner = client.app.state.experiments
        phase = runner._phase

        async def pause_between(eid, role):
            sid = await phase(eid, role)
            if role == "control":
                await asyncio.Event().wait()
            return sid

        monkeypatch.setattr(runner, "_phase", pause_between)
        client.post(f"/api/experiments/{exp['id']}/start", json={})
        running = wait_for(client, exp["id"], lambda item: item["control_session_id"] is not None)
        control = running["control_session_id"]
        deadline = time.monotonic() + 3
        while client.get(f"/api/sessions/{control}").json()["session"]["status"] == "live":
            assert time.monotonic() < deadline
            time.sleep(0.03)
        assert client.post("/api/sessions", json={"name": "Must not interleave"}).status_code == 409
        monkeypatch.setattr(settings, "import_available", lambda: True)
        assert (
            client.post(
                "/api/import/argos",
                content=b"x",
                headers={"content-type": "application/octet-stream"},
            ).status_code
            == 409
        )
        client.post(f"/api/experiments/{exp['id']}/cancel", json={})
        assert client.post("/api/sessions", json={"name": "Now allowed"}).status_code == 201
