"""Local observations and explicit agent handoff use real storage, never a paid model."""

import asyncio
import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from argos_studio.agent_provider import AgentConfig, Reply
from argos_studio.app import Settings, create_app
from argos_studio.core import OBSERVATION_SCAN_BATCH, Store
from argos_studio.investigation import fingerprint
from argos_studio.observations import ObservationMonitor


class OfflineProvider:
    """Test double that records requests without credentials or network access."""

    provider = "test-double"
    model = "observation-fixture"
    sends_data_off_machine = False

    def __init__(self):
        self.calls = []

    async def respond(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return Reply([], [], "Intervalle synthétique consulté.", {"output_tokens": 3})

    async def close(self):
        pass


class InvestigatingProvider(OfflineProvider):
    """Requests one real local investigation before returning a scripted conclusion."""

    def __init__(self, bounds=None):
        super().__init__()
        self.bounds = bounds or {"start_s": None, "end_s": None}

    async def respond(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if len(self.calls) == 1:
            item = {
                "type": "function_call",
                "call_id": "inspect-reception-fixture",
                "name": "investigate_reception",
                "arguments": json.dumps({**self.bounds, "context": "Offline tool fixture"}),
            }
            return Reply([item], [item], "", {"output_tokens": 5})
        return Reply([], [], "Rapport local conservé.", {"output_tokens": 3})


@pytest.fixture
def settings(tmp_path):
    return Settings(
        data_dir=tmp_path,
        argos_root=None,
        argos_python=None,
        agent_config=AgentConfig(),
        observation_interval_s=0.03,
    )


def sample(at_s):
    return {
        "source_time_s": at_s + 10,
        "elapsed_s": at_s,
        "received_at": 1000 + at_s,
        "roll_deg": at_s,
        "pitch_deg": 0,
        "gyro_x_deg_s": None,
    }


def recording(store, *, source="simulation", times=(0.5, 0.55, 1.5, 1.55)):
    return store.import_session(
        "Observation test fixture",
        "Inspect recorded receipt intervals",
        [sample(at_s) for at_s in times],
        source=source,
        duration_s=max(times) + 0.5,
    )["id"]


def api_wait(client, url, predicate, *, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(url)
        assert response.status_code == 200, response.text
        result = response.json()
        if predicate(result):
            return result
        time.sleep(0.01)
    pytest.fail(f"Expected persisted observation state did not arrive at {url}: {result}")


def discovered(client, sid):
    listing = api_wait(
        client,
        f"/api/sessions/{sid}/observations",
        lambda result: result["scan"]["complete"] and bool(result["items"]),
    )
    return listing["items"][0]


def test_background_watch_discovers_every_source_without_model_or_acquisition(settings):
    provider = OfflineProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        store = client.app.state.store
        snapshots = {}
        for source in ("simulation", "argos-recording", "mavlink-udp"):
            sid = recording(store, source=source)
            snapshots[sid] = store.snapshot(sid)
        for sid, snapshot in snapshots.items():
            observation = discovered(client, sid)
            assert observation["source"] == snapshot["session"]["source"]
            assert observation["kind"] == "receipt_gap"
            assert observation["start_s"] == 0.55
            assert observation["end_s"] == 1.5
            assert observation["duration_s"] == pytest.approx(0.95)
            assert observation["before_seq"] == 1
            assert observation["after_seq"] == 2
            assert observation["disposition"] == "open"
            assert observation["investigation_id"] is None
            assert store.snapshot(sid) == snapshot
            assert store.list_agent_runs(sid) == []
            assert store.list_investigations(sid) == []
        assert provider.calls == []
        assert not client.app.state.runtime.active
        assert not client.app.state.experiments.active
        assert store.list_experiments() == []


def test_local_investigation_is_idempotent_concurrent_and_fingerprinted(settings):
    provider = OfflineProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        store = client.app.state.store
        sid = recording(store)
        observation = discovered(client, sid)
        detail = f"/api/sessions/{sid}/observations/{observation['id']}"
        evidence_hash = fingerprint(store.investigation_input(sid))

        def investigate():
            response = client.post(detail + "/investigate", json={})
            assert response.status_code == 200, response.text
            return response.json()

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: investigate(), range(4)))
        assert len({item["investigation_id"] for item in results}) == 1
        linked = results[0]
        assert linked["investigation_id"] is not None
        assert client.get(detail).json() == linked
        reports = store.list_investigations(sid)
        assert len(reports) == 1
        report = store.get_investigation(sid, linked["investigation_id"])
        assert report["window_s"] == {"start_s": 0.55, "end_s": 1.5}
        assert report["snapshot"]["sha256"] == evidence_hash
        assert report["snapshot"]["sample_count"] == 4
        assert report["snapshot"]["source"] == "simulation"
        store.annotate(sid, "Context added after the retained investigation", 1)
        assert investigate() == linked
        assert store.get_investigation(sid, linked["investigation_id"]) == report
        assert provider.calls == []
        assert store.list_agent_runs(sid) == []


def test_observation_disposition_and_report_survive_restart_without_rediscovery(settings):
    with TestClient(create_app(settings)) as client:
        sid = recording(client.app.state.store)
        observation = discovered(client, sid)
        prefix = f"/api/sessions/{sid}/observations"
        detail = f"{prefix}/{observation['id']}"
        report_link = client.post(detail + "/investigate", json={}).json()["investigation_id"]
        dismissed = client.post(detail + "/dismiss", json={})
        assert dismissed.status_code == 200
        assert dismissed.json()["disposition"] == "dismissed"
        assert dismissed.json()["investigation_id"] == report_link
    with TestClient(create_app(settings)) as reopened:
        listing = api_wait(reopened, prefix, lambda result: result["scan"]["complete"])
        assert listing["items"] == [dismissed.json()]
        assert reopened.get(detail).json() == dismissed.json()
        opened = reopened.post(detail + "/reopen", json={})
        assert opened.status_code == 200
        assert opened.json()["disposition"] == "open"
        assert opened.json()["investigation_id"] == report_link
        assert reopened.post(detail + "/reopen", json={}).json() == opened.json()
        assert len(reopened.app.state.store.list_investigations(sid)) == 1


@pytest.mark.parametrize("action", ["investigate", "dismiss", "reopen"])
def test_observation_actions_enforce_session_scope_and_json_boundaries(settings, action):
    with TestClient(create_app(settings)) as client:
        store = client.app.state.store
        sid, other = recording(store), recording(store)
        observation = discovered(client, sid)
        own = f"/api/sessions/{sid}/observations/{observation['id']}"
        foreign = f"/api/sessions/{other}/observations/{observation['id']}"
        assert client.get(foreign).status_code == 404
        assert client.post(foreign + f"/{action}", json={}).status_code == 404
        assert client.get("/api/sessions/missing/observations").status_code == 404
        assert client.get(own + "-missing").status_code == 404
        assert client.post(own + f"/{action}", json={"command": "start"}).status_code == 422
        assert client.post(own + f"/{action}", content="{}").status_code == 415
        assert (
            client.post(
                own + f"/{action}",
                json={},
                headers={"Origin": "https://unrelated.example"},
            ).status_code
            == 403
        )
        assert client.get(own).json() == observation
        assert store.list_investigations(sid) == []
        assert store.list_investigations(other) == []


@pytest.mark.parametrize("dismissed", [False, True])
def test_agent_handoff_is_explicit_and_keeps_exact_observation_context(settings, dismissed):
    provider = OfflineProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        store = client.app.state.store
        sid = recording(store)
        observation = discovered(client, sid)
        if dismissed:
            url = f"/api/sessions/{sid}/observations/{observation['id']}/dismiss"
            assert client.post(url, json={}).status_code == 200
        assert provider.calls == []
        prefix = f"/api/sessions/{sid}/agent-runs"
        response = client.post(
            prefix,
            json={"prompt": "Explain this recorded interval", "observation_id": observation["id"]},
        )
        assert response.status_code == 202, response.text
        run = api_wait(
            client,
            f"{prefix}/{response.json()['id']}",
            lambda result: result["status"] != "running",
        )
        assert run["status"] == "completed"
        assert run["context"]["observation_id"] == observation["id"]
        assert run["context"]["window_s"] == {"start_s": 0.55, "end_s": 1.5}
        assert len(provider.calls) == 1
        messages = provider.calls[0]["messages"]
        assert observation["id"] in json.dumps(messages)
        assert observation["id"] not in provider.calls[0]["instructions"]
        initial = json.loads(messages[0]["content"])
        assert initial["window_s"] == run["context"]["window_s"]
        assert store.list_experiments() == []


@pytest.mark.parametrize(
    ("bounds", "expected_status"),
    [
        ({"start_s": 0}, 409),
        ({"end_s": 1.55}, 409),
        ({"start_s": 0.6, "end_s": 1.5}, 409),
        ({"start_s": 1.5, "end_s": 0.55}, 409),
        ({"start_s": False}, 422),
        ({"end_s": True}, 422),
        ({"end_s": "1.5"}, 422),
    ],
)
def test_agent_handoff_rejects_a_window_that_differs_from_observation(
    settings, bounds, expected_status
):
    provider = OfflineProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sid = recording(client.app.state.store)
        observation = discovered(client, sid)
        prefix = f"/api/sessions/{sid}/agent-runs"
        response = client.post(
            prefix,
            json={"prompt": "Inspect", "observation_id": observation["id"], **bounds},
        )
        assert response.status_code == expected_status, response.text
        assert client.get(prefix).json() == []
        assert provider.calls == []


def test_agent_handoff_cannot_reference_another_sessions_observation(settings):
    provider = OfflineProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        store = client.app.state.store
        sid, other = recording(store), recording(store)
        observation = discovered(client, sid)
        prefix = f"/api/sessions/{other}/agent-runs"
        response = client.post(
            prefix, json={"prompt": "Inspect", "observation_id": observation["id"]}
        )
        assert response.status_code == 404, response.text
        assert client.get(prefix).json() == []
        assert provider.calls == []


@pytest.mark.parametrize("existing_report", [False, True])
def test_agent_investigation_links_observation_and_reuses_its_frozen_report(
    settings, existing_report
):
    provider = InvestigatingProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        store = client.app.state.store
        sid = recording(store)
        observation = discovered(client, sid)
        detail = f"/api/sessions/{sid}/observations/{observation['id']}"
        if existing_report:
            linked = client.post(detail + "/investigate", json={}).json()
            retained = store.get_investigation(sid, linked["investigation_id"])
            store.annotate(sid, "New context should not replace the linked report", 1)
        prefix = f"/api/sessions/{sid}/agent-runs"
        response = client.post(
            prefix,
            json={
                "prompt": "Investigate the interval with the local tool",
                "observation_id": observation["id"],
            },
        )
        assert response.status_code == 202, response.text
        run = api_wait(
            client,
            f"{prefix}/{response.json()['id']}",
            lambda result: result["status"] != "running",
        )
        assert run["status"] == "completed"
        result = next(
            step["payload"]["result"]
            for step in run["steps"]
            if step["kind"] == "tool_result" and step["payload"]["name"] == "investigate_reception"
        )
        assert result["observation_id"] == observation["id"]
        assert result["report_previously_linked"] is existing_report
        assert client.get(detail).json()["investigation_id"] == result["id"]
        report = store.get_investigation(sid, result["id"])
        assert report["window_s"] == {"start_s": 0.55, "end_s": 1.5}
        assert len(store.list_investigations(sid)) == 1
        if existing_report:
            assert report == retained
        else:
            assert report["snapshot"]["sha256"] == fingerprint(store.investigation_input(sid))
        assert json.loads(provider.calls[1]["messages"][-1]["output"]) == result


def test_agent_can_investigate_a_wider_window_without_linking_the_wrong_report(settings):
    provider = InvestigatingProvider({"start_s": 0, "end_s": 1.55})
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        store = client.app.state.store
        sid = recording(store)
        observation = discovered(client, sid)
        prefix = f"/api/sessions/{sid}/agent-runs"
        response = client.post(
            prefix,
            json={"prompt": "Examine the broader context", "observation_id": observation["id"]},
        )
        assert response.status_code == 202, response.text
        run = api_wait(
            client,
            f"{prefix}/{response.json()['id']}",
            lambda result: result["status"] != "running",
        )
        assert run["status"] == "completed"
        reports = store.list_investigations(sid)
        assert len(reports) == 1
        assert reports[0]["window_s"] == {"start_s": 0, "end_s": 1.55}
        assert store.get_observation(sid, observation["id"])["investigation_id"] is None
        assert run["context"]["window_s"] == {"start_s": 0.55, "end_s": 1.5}


@pytest.mark.parametrize("times", [(0.5,), (0.5, 0.55), (0.5, 0.75)])
def test_watch_does_not_turn_unbracketed_silence_into_an_observation(settings, times):
    with TestClient(create_app(settings)) as client:
        sid = recording(client.app.state.store, times=times)
        listing = api_wait(
            client,
            f"/api/sessions/{sid}/observations",
            lambda result: result["scan"]["complete"],
        )
        assert listing["items"] == []
        assert listing["scan"]["omitted_gap_count"] == 0
        assert client.app.state.store.get_session(sid)["elapsed_s"] > max(times)


def test_monitor_skips_complete_unchanged_sessions_and_resumes_after_append(tmp_path, monkeypatch):
    store = Store(tmp_path / "studio.sqlite3")
    sid = store.create_session("Live append fixture", "Observe the next receipt")["id"]
    original = store.scan_observations
    calls = []

    def counted(session_id, **kwargs):
        calls.append(session_id)
        return original(session_id, **kwargs)

    monkeypatch.setattr(store, "scan_observations", counted)
    monitor = ObservationMonitor(store)

    async def scenario():
        assert await monitor.scan_once() is True
        assert await monitor.scan_once() is True
        assert calls == [sid]
        store.append_samples(sid, [sample(0), sample(0.05)])
        assert await monitor.scan_once() is True
        assert await monitor.scan_once() is True
        assert calls == [sid, sid]
        assert store.list_observations(sid)["items"] == []
        store.append_sample(sid, **sample(1))
        assert await monitor.scan_once() is True
        assert calls == [sid, sid, sid]
        saved = store.list_observations(sid)
        assert saved["scan"]["complete"]
        assert len(saved["items"]) == 1
        assert saved["items"][0]["before_seq"] == 1
        assert saved["items"][0]["after_seq"] == 2
        store.finish_session(sid, elapsed_s=1.5)
        assert await monitor.scan_once() is True
        assert calls == [sid, sid, sid]
        assert store.list_observations(sid) == saved
        await monitor.close()

    asyncio.run(scenario())


def test_monitor_drains_incomplete_backlog_despite_unchanged_sample_count(tmp_path, monkeypatch):
    store = Store(tmp_path / "studio.sqlite3")
    backlog = recording(
        store,
        times=tuple(
            seq * 0.01 + (1 if seq >= OBSERVATION_SCAN_BATCH else 0)
            for seq in range(OBSERVATION_SCAN_BATCH + 3)
        ),
    )
    small = recording(store, times=(0, 0.05))
    original = store.scan_observations
    calls = []

    def counted(session_id, **kwargs):
        calls.append(session_id)
        return original(session_id, **kwargs)

    monkeypatch.setattr(store, "scan_observations", counted)
    monitor = ObservationMonitor(store)

    async def scenario():
        assert await monitor.scan_once() is True
        first = store.list_observations(backlog)
        assert first["scan"]["last_seq"] == OBSERVATION_SCAN_BATCH - 1
        assert first["scan"]["complete"] is False
        assert first["items"] == []
        assert calls.count(backlog) == calls.count(small) == 1
        assert await monitor.scan_once() is True
        completed = store.list_observations(backlog)
        assert completed["scan"]["complete"] is True
        assert calls.count(backlog) == 2
        assert calls.count(small) == 1
        assert len(completed["items"]) == 1
        assert completed["items"][0]["before_seq"] == OBSERVATION_SCAN_BATCH - 1
        assert completed["items"][0]["after_seq"] == OBSERVATION_SCAN_BATCH
        assert completed["items"][0]["duration_s"] == pytest.approx(1.01)
        assert await monitor.scan_once() is True
        assert calls.count(backlog) == 2
        assert calls.count(small) == 1
        await monitor.close()

    asyncio.run(scenario())


def test_monitor_health_recovers_from_scan_failure_without_disclosing_exception(
    tmp_path, monkeypatch
):
    store = Store(tmp_path / "studio.sqlite3")
    sid = recording(store)
    monitor = ObservationMonitor(store, interval_s=0.03)
    original = store.scan_observations

    def unavailable(*args, **kwargs):
        raise RuntimeError("Database unavailable at /private/path with sk-private-secret")

    async def scenario():
        monkeypatch.setattr(store, "scan_observations", unavailable)
        assert await monitor.scan_once() is False
        failed = monitor.health()
        assert failed["available"] is False
        assert failed["last_error"]
        assert "private" not in json.dumps(failed)
        monkeypatch.setattr(store, "scan_observations", original)
        assert await monitor.scan_once() is True
        assert monitor.health()["last_error"] is None
        assert monitor.health()["last_scan_at"] is not None
        assert len(store.list_observations(sid)["items"]) == 1
        await monitor.close()

    asyncio.run(scenario())


def test_monitor_close_drains_pending_scan_before_releasing_storage(tmp_path, monkeypatch):
    store = Store(tmp_path / "studio.sqlite3")
    sid = recording(store)
    original = store.scan_observations
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(3), "Test did not release the in-flight database scan"
        result = original(*args, **kwargs)
        finished.set()
        return result

    monkeypatch.setattr(store, "scan_observations", delayed)
    monitor = ObservationMonitor(store, interval_s=0.03)

    async def scenario():
        monitor.start()
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            closing = asyncio.create_task(monitor.close())
            await asyncio.sleep(0.03)
            assert not closing.done()
            assert not finished.is_set()
            release.set()
            await asyncio.wait_for(closing, 2)
            assert finished.is_set()
            assert monitor.health()["available"] is False
            assert len(store.list_observations(sid)["items"]) == 1
        finally:
            release.set()
            await monitor.close()

    asyncio.run(scenario())
