"""Durable agent scope, bounded evidence traces, and terminal lifecycle."""

import copy
import json
import math
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from argos_studio.core import (
    MAX_AGENT_CONTEXT_BYTES,
    MAX_AGENT_RUNS,
    MAX_AGENT_STEP_BYTES,
    MAX_AGENT_STEPS,
    MAX_AGENT_USAGE_BYTES,
    Store,
)
from argos_studio.investigation import build_report


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "studio.sqlite3")


def session(store, source="simulation"):
    return store.create_session("Observation", "Inspect receipt", source=source, status="completed")


def run(store, origin=None, **overrides):
    origin = origin or session(store)
    arguments = {
        "prompt": "Inspect the selected window",
        "provider": "scripted-test",
        "model": "deterministic-fixture",
        **overrides,
    }
    return store.create_agent_run(origin["id"], **arguments)


def exact_json_object(size):
    value = {"text": ""}
    value["text"] = "x" * (size - len(json.dumps(value).encode("utf-8")))
    return value


@pytest.mark.parametrize("source", ["simulation", "argos-recording", "mavlink-udp"])
def test_run_retains_scope_and_exact_trace_across_reopen_without_touching_capture(store, source):
    origin = session(store, source)
    before = store.investigation_input(origin["id"])
    context = {"window_s": {"start_s": 0, "end_s": 2}, "read_only": True}
    with patch("argos_studio.core.time.time", return_value=1000):
        saved = run(store, origin, context=context)
    frozen = copy.deepcopy(saved)
    context["window_s"]["end_s"] = 99
    saved["context"]["window_s"]["start_s"] = 50
    assert Store(store.path).get_agent_run(origin["id"], saved["id"]) == frozen
    assert frozen["status"] == "running"
    assert frozen["created_at"] == 1000
    assert frozen["steps"] == []
    assert frozen["ended_at"] is frozen["answer"] is frozen["error"] is frozen["usage"] is None
    payload = {"name": "read_session", "result": {"label": "évidence", "measurements": [None, 1.5]}}
    with patch("argos_studio.core.time.time", return_value=1001):
        step = store.append_agent_step(origin["id"], saved["id"], "tool_result", payload)
    expected = copy.deepcopy(step)
    payload["result"]["measurements"].append(9)
    step["payload"]["name"] = "changed"
    with patch("argos_studio.core.time.time", return_value=1002):
        ended = store.finish_agent_run(
            origin["id"], saved["id"], "completed", answer="Evidence retained", usage={"tokens": 20}
        )
    assert ended["steps"] == [expected]
    assert expected["seq"] == 0
    assert expected["created_at"] == 1001
    assert ended["ended_at"] == 1002
    assert Store(store.path).get_agent_run(origin["id"], saved["id"]) == ended
    assert store.list_agent_runs(origin["id"]) == [
        {key: value for key, value in ended.items() if key != "steps"}
    ]
    assert store.investigation_input(origin["id"]) == before


@pytest.mark.parametrize(
    "overrides",
    [
        {"prompt": ""},
        {"prompt": "   "},
        {"prompt": "x" * 4001},
        {"prompt": None},
        {"provider": ""},
        {"provider": "x" * 121},
        {"provider": 1},
        {"model": " "},
        {"model": "x" * 121},
        {"model": []},
    ],
)
def test_invalid_identity_or_prompt_never_creates_a_run(store, overrides):
    origin = session(store)
    with pytest.raises(ValueError):
        run(store, origin, **overrides)
    assert store.list_agent_runs(origin["id"]) == []


def test_exact_text_limits_and_context_bound_are_accepted(store):
    origin = session(store)
    saved = run(
        store,
        origin,
        prompt="x" * 4000,
        provider="p" * 120,
        model="m" * 120,
        context=exact_json_object(MAX_AGENT_CONTEXT_BYTES),
    )
    assert len(saved["prompt"]) == 4000
    store.finish_agent_run(
        origin["id"], saved["id"], "completed", answer="a" * 16000, error="e" * 2000
    )
    for context in ({"text": "x" * MAX_AGENT_CONTEXT_BYTES}, {"text": "é" * 9000}, []):
        with pytest.raises(ValueError):
            run(store, origin, context=context)
    assert len(store.list_agent_runs(origin["id"])) == 1


def test_json_validation_rejects_nonfinite_implicit_conversion_and_cycles(store):
    saved = run(store)
    cycle = {}
    cycle["cycle"] = cycle
    invalid = (
        None,
        [],
        {"value": math.nan},
        {"value": math.inf},
        {"value": -math.inf},
        {"value": b"bytes"},
        {"value": (1, 2)},
        {1: "numeric"},
        {"nested": [{False: "boolean key"}]},
        cycle,
    )
    for value in invalid:
        with pytest.raises(ValueError):
            store.append_agent_step(saved["session_id"], saved["id"], "tool_result", value)
    assert store.get_agent_run(saved["session_id"], saved["id"])["steps"] == []


def test_trace_size_is_bounded_by_utf8_bytes_and_step_kinds_are_explicit(store):
    saved = run(store)
    for kind in ("shell", "model_response", "", None, []):
        with pytest.raises(ValueError, match="kind"):
            store.append_agent_step(saved["session_id"], saved["id"], kind, {})
    for payload in ({"text": "x" * MAX_AGENT_STEP_BYTES}, {"text": "é" * 33000}):
        with pytest.raises(ValueError, match="64 KiB"):
            store.append_agent_step(saved["session_id"], saved["id"], "tool_result", payload)
    exact = exact_json_object(MAX_AGENT_STEP_BYTES)
    assert (
        store.append_agent_step(saved["session_id"], saved["id"], "tool_result", exact)["payload"]
        == exact
    )
    for kind in ("tool_call", "provider_usage"):
        store.append_agent_step(saved["session_id"], saved["id"], kind, {})
    assert len(store.get_agent_run(saved["session_id"], saved["id"])["steps"]) == 3


def test_step_count_limit_is_atomic_under_concurrent_writers(store):
    saved = run(store)
    for index in range(MAX_AGENT_STEPS - 1):
        store.append_agent_step(saved["session_id"], saved["id"], "tool_call", {"index": index})
    barrier = threading.Barrier(2)

    def append(index):
        barrier.wait(timeout=5)
        try:
            return store.append_agent_step(
                saved["session_id"], saved["id"], "tool_result", {"index": index}
            )
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(append, (100, 101)))
    assert sum(result is not None for result in results) == 1
    steps = store.get_agent_run(saved["session_id"], saved["id"])["steps"]
    assert [step["seq"] for step in steps] == list(range(MAX_AGENT_STEPS))


@pytest.mark.parametrize("same_session", [False, True])
def test_only_one_concurrent_global_run_is_created(store, same_session):
    first = session(store)
    second = first if same_session else session(store)
    barrier = threading.Barrier(2)

    def create(origin):
        barrier.wait(timeout=5)
        try:
            return run(store, origin)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create, (first, second)))
    accepted = [result for result in results if result is not None]
    assert len(accepted) == 1
    saved = accepted[0]
    store.finish_agent_run(saved["session_id"], saved["id"], "cancelled")
    assert run(store, first)["status"] == "running"


def test_run_count_limit_is_per_session_even_for_terminal_runs(store):
    origin = session(store)
    for _ in range(MAX_AGENT_RUNS):
        saved = run(store, origin)
        store.finish_agent_run(origin["id"], saved["id"], "cancelled")
    with pytest.raises(ValueError, match="at most 50"):
        run(store, origin)
    assert len(store.list_agent_runs(origin["id"])) == MAX_AGENT_RUNS
    assert run(store)["status"] == "running"


@pytest.mark.parametrize("status", ["completed", "cancelled", "failed", "interrupted", "limited"])
def test_first_terminal_outcome_and_its_trace_are_immutable(store, status):
    saved = run(store)
    store.append_agent_step(saved["session_id"], saved["id"], "tool_call", {"name": "read_session"})
    terminal = store.finish_agent_run(
        saved["session_id"],
        saved["id"],
        status,
        answer="Retained",
        error="Reason",
        usage={"calls": 1},
    )
    assert terminal["status"] == status
    assert terminal["ended_at"] is not None
    assert (
        store.finish_agent_run(
            saved["session_id"], saved["id"], "failed", answer="Changed", error="Changed"
        )
        == terminal
    )
    with pytest.raises(ValueError, match="running"):
        store.append_agent_step(saved["session_id"], saved["id"], "tool_result", {})
    assert store.recover_agent_runs() == 0
    assert Store(store.path).get_agent_run(saved["session_id"], saved["id"]) == terminal


@pytest.mark.parametrize("status", ["running", "proposed", "expired", "", None, []])
def test_invalid_terminal_status_keeps_run_active(store, status):
    saved = run(store)
    with pytest.raises(ValueError, match="status"):
        store.finish_agent_run(saved["session_id"], saved["id"], status)
    assert store.get_agent_run(saved["session_id"], saved["id"]) == saved


def test_terminal_payload_validation_does_not_partially_finish(store):
    saved = run(store)
    for values in (
        {"answer": "a" * 16001},
        {"answer": []},
        {"error": "e" * 2001},
        {"error": False},
        {"usage": []},
        {"usage": {"tokens": math.inf}},
        {"usage": {"tokens": {1: 4}}},
        {"usage": {"text": "x" * MAX_AGENT_USAGE_BYTES}},
    ):
        with pytest.raises(ValueError):
            store.finish_agent_run(saved["session_id"], saved["id"], "completed", **values)
        assert store.get_agent_run(saved["session_id"], saved["id"]) == saved
    usage = exact_json_object(MAX_AGENT_USAGE_BYTES)
    assert (
        store.finish_agent_run(saved["session_id"], saved["id"], "completed", usage=usage)["usage"]
        == usage
    )


def test_concurrent_completion_and_cancellation_retain_one_terminal_result(store):
    saved = run(store)
    barrier = threading.Barrier(2)

    def finish(status):
        barrier.wait(timeout=5)
        return store.finish_agent_run(
            saved["session_id"], saved["id"], status, answer=status, usage={"winner": status}
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(finish, ("completed", "cancelled")))
    assert results[0] == results[1]
    retained = store.get_agent_run(saved["session_id"], saved["id"])
    assert retained == results[0]
    assert retained["status"] == retained["answer"] == retained["usage"]["winner"]


def test_recovery_retains_partial_trace_without_inventing_end_time_or_resuming(store):
    saved = run(store)
    step = store.append_agent_step(saved["session_id"], saved["id"], "tool_call", {"name": "read"})
    reopened = Store(store.path)
    assert reopened.get_agent_run(saved["session_id"], saved["id"])["status"] == "running"
    assert reopened.recover_agent_runs() == 1
    assert reopened.recover_agent_runs() == 0
    recovered = reopened.get_agent_run(saved["session_id"], saved["id"])
    assert recovered["status"] == "interrupted"
    assert recovered["ended_at"] is None
    assert recovered["steps"] == [step]
    assert recovered["error"]
    assert run(reopened)["status"] == "running"


def test_unknown_ids_and_other_session_cannot_read_or_mutate_a_run(store):
    saved = run(store)
    other = session(store)
    for origin in (other["id"], "missing"):
        for operation in (
            lambda origin=origin: store.get_agent_run(origin, saved["id"]),
            lambda origin=origin: store.append_agent_step(origin, saved["id"], "tool_call", {}),
            lambda origin=origin: store.finish_agent_run(origin, saved["id"], "cancelled"),
        ):
            with pytest.raises(KeyError):
                operation()
    for operation in (
        lambda: store.create_agent_run("missing", "Inspect", "test", "fixture"),
        lambda: store.list_agent_runs("missing"),
        lambda: store.get_agent_run(saved["session_id"], "missing"),
        lambda: store.append_agent_step(saved["session_id"], "missing", "tool_call", {}),
        lambda: store.finish_agent_run(saved["session_id"], "missing", "cancelled"),
    ):
        with pytest.raises(KeyError):
            operation()
    assert store.list_agent_runs(other["id"]) == []
    assert store.get_agent_run(saved["session_id"], saved["id"]) == saved


def test_schema_enforces_foreign_keys_running_uniqueness_and_trace_identity(store):
    saved = run(store)
    other = session(store)
    step = store.append_agent_step(saved["session_id"], saved["id"], "tool_call", {})
    with store._connection() as connection:
        statements = (
            ("UPDATE agent_runs SET session_id='missing' WHERE id=?", (saved["id"],)),
            ("DELETE FROM sessions WHERE id=?", (saved["session_id"],)),
            ("DELETE FROM agent_runs WHERE id=?", (saved["id"],)),
            ("UPDATE agent_steps SET run_id='missing' WHERE run_id=?", (saved["id"],)),
            ("UPDATE agent_steps SET kind='shell' WHERE run_id=?", (saved["id"],)),
            (
                "INSERT INTO agent_steps VALUES (?,?,?,?,?)",
                (saved["id"], step["seq"], "tool_call", 100, "{}"),
            ),
            (
                "INSERT INTO agent_runs "
                "(id,session_id,prompt,provider,model,context,status,created_at) "
                "VALUES ('second',?,'Inspect','test','fixture','{}','running',100)",
                (other["id"],),
            ),
        )
        for sql, parameters in statements:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql, parameters)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_run_detail_reads_status_and_steps_from_one_snapshot(store):
    saved = run(store)
    reader = Store(store.path)
    captured = threading.Event()
    finished = threading.Event()
    original = reader._require_agent_run

    def pause_after_run(connection, session_id, run_id):
        result = original(connection, session_id, run_id)
        captured.set()
        assert finished.wait(timeout=5)
        return result

    with (
        patch.object(reader, "_require_agent_run", side_effect=pause_after_run),
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        pending = executor.submit(reader.get_agent_run, saved["session_id"], saved["id"])
        try:
            assert captured.wait(timeout=5)
            store.append_agent_step(saved["session_id"], saved["id"], "tool_result", {})
            store.finish_agent_run(saved["session_id"], saved["id"], "completed", answer="Complete")
        finally:
            finished.set()
        assert pending.result(timeout=5) == saved
    current = store.get_agent_run(saved["session_id"], saved["id"])
    assert current["status"] == "completed"
    assert len(current["steps"]) == 1


def test_version_three_migration_preserves_every_existing_record(store):
    origin = store.create_session("Existing", "Retain evidence")
    store.append_sample(
        origin["id"],
        source_time_s=20,
        elapsed_s=1,
        received_at=1000,
        roll_deg=4,
        pitch_deg=None,
        gyro_x_deg_s=None,
    )
    store.annotate(origin["id"], "Retained note", 1)
    store.finish_session(origin["id"], elapsed_s=2)
    report = store.save_investigation(
        origin["id"], build_report(store.investigation_input(origin["id"]))
    )
    experiment = store.create_experiment(origin["id"], report["id"], {"kind": "test"})
    store.finish_experiment(experiment["id"], "cancelled")
    udp = store.create_session("Stored synthetic UDP", "Preserve raw bytes", source="mavlink-udp")
    store.append_datagram(
        udp["id"],
        elapsed_s=1,
        received_at=1001,
        peer_host="127.0.0.1",
        peer_port=14580,
        payload=b"synthetic migration fixture",
        disposition="accepted",
        details={"frames": [{"type": "ATTITUDE"}]},
        samples=[
            {
                "elapsed_s": 1,
                "received_at": 1001,
                "source_time_s": 50,
                "roll_deg": None,
                "pitch_deg": None,
                "gyro_x_deg_s": None,
            }
        ],
    )
    store.finish_session(udp["id"])
    with store._connection() as connection:
        connection.execute("DROP TABLE observations")
        connection.execute("DROP TABLE observation_scans")
        connection.execute("DROP TABLE agent_steps")
        connection.execute("DROP TABLE agent_runs")
        connection.execute("PRAGMA user_version=3")
        before = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in (
                "sessions",
                "samples",
                "events",
                "datagrams",
                "investigations",
                "experiments",
            )
        }
    migrated = Store(store.path)
    with migrated._connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        for table, expected in before.items():
            assert [
                tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")
            ] == expected
    saved = run(migrated, origin)
    assert Store(store.path).get_agent_run(origin["id"], saved["id"]) == saved


def test_agent_migration_failure_rolls_back_schema_version_and_new_objects(store):
    origin = session(store)
    with store._connection() as connection:
        connection.execute("DROP TABLE observations")
        connection.execute("DROP TABLE observation_scans")
        connection.execute("DROP TABLE agent_steps")
        connection.execute("DROP TABLE agent_runs")
        connection.execute("PRAGMA user_version=3")
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO events(session_id,kind,at_s,text,created_at) "
            "VALUES ('missing','annotation',0,'Orphan',0)"
        )
    with pytest.raises(ValueError, match="foreign keys"):
        Store(store.path)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute("SELECT id FROM sessions").fetchone()[0] == origin["id"]
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name IN "
                "('agent_steps','agent_runs','agent_session','one_running_agent')"
            ).fetchall()
            == []
        )
