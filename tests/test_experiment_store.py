"""Durable experiment proposals, single execution, and atomic capture ownership."""

import copy
import json
import math
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from argos_studio.core import (
    MAX_EXPERIMENT_PLAN_BYTES,
    MAX_EXPERIMENT_RESULT_BYTES,
    MAX_EXPERIMENTS,
    Store,
)
from argos_studio.investigation import build_report


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "studio.sqlite3")


def origin(store, *, source="simulation"):
    session = store.create_session("Original observation", "Inspect receipt", source=source)
    store.append_samples(
        session["id"],
        [
            {
                "source_time_s": value,
                "elapsed_s": value,
                "received_at": value + 1000,
                "roll_deg": 0,
                "pitch_deg": 0,
                "gyro_x_deg_s": 0,
            }
            for value in (0, 0.05, 1.1)
        ],
    )
    store.finish_session(session["id"], elapsed_s=2)
    report = store.save_investigation(
        session["id"], build_report(store.investigation_input(session["id"]))
    )
    return session, report


def proposal(store, *, ttl_s=600):
    session, report = origin(store)
    return store.create_experiment(
        session["id"], report["id"], {"kind": "synthetic_dropout", "duration_s": 6}, ttl_s
    )


def owned_capture(store, experiment, role="control"):
    return store.create_experiment_session(experiment["id"], role, role, "Synthetic comparison", {})


def test_proposal_is_persistent_immutable_input_and_creates_no_capture(store):
    session, report = origin(store)
    plan = {"kind": "synthetic_dropout", "parameters": {"duration_s": 6}}
    with patch("argos_studio.core.time.time", return_value=1000):
        saved = store.create_experiment(session["id"], report["id"], plan)
    original = copy.deepcopy(saved)
    plan["parameters"]["duration_s"] = 99
    saved["plan"]["kind"] = "changed"
    reopened = Store(store.path)
    assert reopened.get_experiment(saved["id"]) == original
    assert original["status"] == "proposed"
    assert original["expires_at"] == 1600
    assert original["started_at"] is original["ended_at"] is None
    assert original["control_session_id"] is original["perturbed_session_id"] is None
    assert original["result"] is original["error"] is None
    assert len(reopened.list_sessions()) == 1


@pytest.mark.parametrize("source", ["argos-recording", "mavlink-udp"])
def test_non_synthetic_origin_is_rejected_even_if_snapshot_claims_synthetic(store, source):
    session, report = origin(store, source=source)
    with store._connection() as connection:
        payload = json.loads(connection.execute("SELECT report FROM investigations").fetchone()[0])
        payload["snapshot"]["source"] = "simulation"
        connection.execute("UPDATE investigations SET report=?", (json.dumps(payload),))
    with pytest.raises(ValueError, match="synthetic"):
        store.create_experiment(session["id"], report["id"], {})
    assert store.list_experiments() == []


def test_non_synthetic_snapshot_and_cross_session_report_are_rejected(store):
    session, report = origin(store)
    another, other_report = origin(store)
    with pytest.raises(KeyError):
        store.create_experiment(session["id"], other_report["id"], {})
    with store._connection() as connection:
        payload = json.loads(
            connection.execute(
                "SELECT report FROM investigations WHERE id=?", (report["id"],)
            ).fetchone()[0]
        )
        payload["snapshot"].pop("source")
        connection.execute(
            "UPDATE investigations SET report=? WHERE id=?", (json.dumps(payload), report["id"])
        )
    with pytest.raises(ValueError, match="synthetic"):
        store.create_experiment(session["id"], report["id"], {})
    assert store.list_experiments(another["id"]) == []


@pytest.mark.parametrize("ttl", [0, -1, 3601, True, "600", math.inf, math.nan])
def test_proposal_rejects_unbounded_or_invalid_expiry(store, ttl):
    session, report = origin(store)
    with pytest.raises(ValueError):
        store.create_experiment(session["id"], report["id"], {}, ttl)
    assert store.list_experiments() == []


def test_plan_requires_bounded_finite_json_with_string_keys(store):
    session, report = origin(store)
    cycle = {}
    cycle["cycle"] = cycle
    for invalid in (
        None,
        [],
        {"x": math.nan},
        {"x": b"bytes"},
        {1: "numeric"},
        {"x": {False: 1}},
        cycle,
    ):
        with pytest.raises(ValueError):
            store.create_experiment(session["id"], report["id"], invalid)
    exact = {"context": ""}
    overhead = len(json.dumps(exact).encode("utf-8"))
    exact["context"] = "x" * (MAX_EXPERIMENT_PLAN_BYTES - overhead)
    store.create_experiment(session["id"], report["id"], exact)
    exact["context"] += "x"
    with pytest.raises(ValueError, match="64 KiB"):
        store.create_experiment(session["id"], report["id"], exact)
    with pytest.raises(ValueError, match="64 KiB"):
        store.create_experiment(
            session["id"], report["id"], {"context": "é" * (MAX_EXPERIMENT_PLAN_BYTES // 2)}
        )
    assert len(store.list_experiments()) == 1


def test_proposal_limit_is_per_origin_and_atomic_under_concurrent_writers(store):
    session, report = origin(store)
    for _ in range(MAX_EXPERIMENTS - 1):
        store.create_experiment(session["id"], report["id"], {})
    barrier = threading.Barrier(2)

    def create_last():
        barrier.wait(timeout=5)
        try:
            return store.create_experiment(session["id"], report["id"], {})
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: create_last(), range(2)))
    assert sum(item is not None for item in results) == 1
    assert len(store.list_experiments(session["id"])) == MAX_EXPERIMENTS
    second = proposal(store)
    assert len(store.list_experiments()) == MAX_EXPERIMENTS + 1
    assert store.list_experiments(second["origin_session_id"]) == [second]


def test_expired_claim_persists_expiry_before_rejecting_and_cannot_replay(store):
    with patch("argos_studio.core.time.time", return_value=1000):
        experiment = proposal(store, ttl_s=10)
    with patch("argos_studio.core.time.time", return_value=1010):
        with pytest.raises(ValueError, match="expired"):
            store.claim_experiment(experiment["id"])
    expired = Store(store.path).get_experiment(experiment["id"])
    assert expired["status"] == "expired"
    assert expired["ended_at"] == 1010
    assert expired["started_at"] is None
    with pytest.raises(ValueError, match="once"):
        store.claim_experiment(experiment["id"])


@pytest.mark.parametrize("same_proposal", [False, True])
def test_only_one_concurrent_claim_wins(store, same_proposal):
    first = proposal(store)
    second = first if same_proposal else proposal(store)
    barrier = threading.Barrier(2)

    def claim(experiment):
        barrier.wait(timeout=5)
        try:
            return store.claim_experiment(experiment["id"])
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, (first, second)))
    assert sum(item is not None for item in results) == 1
    assert len([item for item in store.list_experiments() if item["status"] == "running"]) == 1
    assert all(item["started_at"] is not None for item in results if item)


def test_claim_requires_no_live_acquisition_and_space_for_both_captures(store):
    experiment = proposal(store)
    live = store.create_session("Unrelated live source", "", source="mavlink-udp")
    with pytest.raises(ValueError, match="live"):
        store.claim_experiment(experiment["id"])
    store.finish_session(live["id"])
    with pytest.raises(ValueError, match="two additional"):
        store.claim_experiment(experiment["id"], max_sessions=3)
    assert store.get_experiment(experiment["id"])["status"] == "proposed"
    assert store.claim_experiment(experiment["id"], max_sessions=4)["status"] == "running"
    with pytest.raises(ValueError, match="reserved"):
        store.create_session("Unrelated source", "")


def test_capture_creation_is_bound_to_running_role_and_control_completion(store):
    experiment = proposal(store)
    with pytest.raises(ValueError, match="running"):
        owned_capture(store, experiment)
    store.claim_experiment(experiment["id"])
    with pytest.raises(ValueError, match="control capture"):
        owned_capture(store, experiment, "perturbed")
    control = store.create_experiment_session(
        experiment["id"], "control", "Control", "Synthetic", {"experiment_id": "forged", "x": 3}
    )
    assert control["source"] == "simulation"
    assert control["metadata"] == {
        "experiment_id": experiment["id"],
        "experiment_role": "control",
        "x": 3,
    }
    assert store.get_experiment(experiment["id"])["control_session_id"] == control["id"]
    with pytest.raises(ValueError, match="already has"):
        owned_capture(store, experiment)
    with pytest.raises(ValueError, match="control capture"):
        owned_capture(store, experiment, "perturbed")
    store.finish_session(control["id"])
    perturbed = owned_capture(store, experiment, "perturbed")
    assert store.get_experiment(experiment["id"])["perturbed_session_id"] == perturbed["id"]
    assert len(store.list_sessions()) == 3


def test_capture_link_failure_rolls_back_the_new_session(store):
    experiment = proposal(store)
    store.claim_experiment(experiment["id"])
    with store._connection() as connection:
        connection.execute(
            "CREATE TRIGGER reject_experiment_link BEFORE UPDATE OF control_session_id "
            "ON experiments BEGIN SELECT RAISE(ABORT,'injected link failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected link failure"):
        owned_capture(store, experiment)
    assert len(store.list_sessions()) == 1
    assert store.get_experiment(experiment["id"])["control_session_id"] is None


def test_finish_requires_both_completed_captures_and_persists_first_result(store):
    experiment = proposal(store)
    store.claim_experiment(experiment["id"])
    with pytest.raises(ValueError, match="comparison result"):
        store.finish_experiment(experiment["id"], "completed")
    with pytest.raises(ValueError, match="Both"):
        store.finish_experiment(experiment["id"], "completed", result={})
    control = owned_capture(store, experiment)
    store.finish_session(control["id"])
    perturbed = owned_capture(store, experiment, "perturbed")
    with pytest.raises(ValueError, match="Both"):
        store.finish_experiment(experiment["id"], "completed", result={})
    store.finish_session(perturbed["id"])
    result = {"outcome": "observed", "evidence": [{"session_id": perturbed["id"]}]}
    completed = store.finish_experiment(experiment["id"], "completed", result=result)
    expected = copy.deepcopy(completed)
    result["outcome"] = "changed"
    completed["result"]["outcome"] = "changed again"
    assert Store(store.path).get_experiment(experiment["id"]) == expected
    assert store.finish_experiment(experiment["id"], "failed", error="late") == expected
    with pytest.raises(ValueError, match="once"):
        store.claim_experiment(experiment["id"])
    with pytest.raises(ValueError, match="running"):
        owned_capture(store, experiment)


def test_result_json_limit_is_checked_before_terminal_transition(store):
    experiment = proposal(store)
    store.claim_experiment(experiment["id"])
    for invalid in ([], {"x": math.inf}, {1: "numeric"}, {"x": "x" * MAX_EXPERIMENT_RESULT_BYTES}):
        with pytest.raises(ValueError):
            store.finish_experiment(experiment["id"], "failed", result=invalid)
    assert store.get_experiment(experiment["id"])["status"] == "running"
    result = {"detail": ""}
    overhead = len(json.dumps(result).encode("utf-8"))
    result["detail"] = "x" * (MAX_EXPERIMENT_RESULT_BYTES - overhead)
    assert store.finish_experiment(experiment["id"], "failed", result=result)["result"] == result


def test_cancel_unstarted_proposal_is_terminal_and_preserves_origin(store):
    experiment = proposal(store)
    before = store.investigation_input(experiment["origin_session_id"])
    cancelled = store.finish_experiment(experiment["id"], "cancelled")
    assert cancelled["status"] == "cancelled"
    assert cancelled["started_at"] is None
    assert cancelled["ended_at"] is not None
    assert store.finish_experiment(experiment["id"], "cancelled") == cancelled
    assert store.investigation_input(experiment["origin_session_id"]) == before
    assert len(store.list_sessions()) == 1


def test_restart_retains_partial_capture_marks_interrupted_and_never_resumes(store):
    with patch("argos_studio.core.time.time", return_value=1000):
        running = proposal(store)
        expires = proposal(store, ttl_s=10)
        waiting = proposal(store, ttl_s=600)
        store.claim_experiment(running["id"])
        control = owned_capture(store, running)
    reopened = Store(store.path)
    assert reopened.get_experiment(running["id"])["status"] == "running"
    assert reopened.recover_interrupted() == 1
    with patch("argos_studio.core.time.time", return_value=1010):
        assert reopened.recover_experiments() == 2
        assert reopened.recover_experiments() == 0
    restored = reopened.get_experiment(running["id"])
    assert restored["status"] == "interrupted"
    assert restored["ended_at"] is None  # The process's actual end was not observed.
    assert restored["control_session_id"] == control["id"]
    assert restored["perturbed_session_id"] is None
    assert restored["error"]
    assert reopened.get_session(control["id"])["status"] == "interrupted"
    assert reopened.get_experiment(expires["id"])["status"] == "expired"
    assert reopened.get_experiment(waiting["id"])["status"] == "proposed"
    with pytest.raises(ValueError, match="once"):
        reopened.claim_experiment(running["id"])


def test_storage_foreign_keys_bind_origin_report_and_owned_capture(store):
    experiment = proposal(store)
    other = proposal(store)
    with store._connection() as connection:
        for field, value in (
            ("origin_session_id", other["origin_session_id"]),
            ("investigation_id", other["investigation_id"]),
            ("control_session_id", "missing"),
            ("perturbed_session_id", "missing"),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    f"UPDATE experiments SET {field}=? WHERE id=?", (value, experiment["id"])
                )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM investigations WHERE id=?", (experiment["investigation_id"],)
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_unknown_experiment_and_origin_raise_without_creating_data(store):
    session, report = origin(store)
    for operation in (
        lambda: store.create_experiment("missing", report["id"], {}),
        lambda: store.create_experiment(session["id"], "missing", {}),
        lambda: store.get_experiment("missing"),
        lambda: store.list_experiments("missing"),
        lambda: store.claim_experiment("missing"),
        lambda: store.create_experiment_session("missing", "control", "X", ""),
        lambda: store.finish_experiment("missing", "cancelled"),
    ):
        with pytest.raises(KeyError):
            operation()
    assert store.list_experiments() == []


def test_version_two_migration_preserves_every_existing_record(store):
    session, report = origin(store)
    store.annotate(session["id"], "Retained note", 1)
    with store._connection() as connection:
        connection.execute("DROP TABLE agent_steps")
        connection.execute("DROP TABLE agent_runs")
        connection.execute("DROP TABLE experiments")
        connection.execute("DROP INDEX investigation_identity")
        connection.execute("PRAGMA user_version=2")
        before = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("sessions", "samples", "events", "datagrams", "investigations")
        }
    migrated = Store(store.path)
    with migrated._connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        for table, expected in before.items():
            assert [
                tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")
            ] == expected
    saved = migrated.create_experiment(session["id"], report["id"], {})
    assert Store(store.path).get_experiment(saved["id"]) == saved


def test_failed_experiment_migration_rolls_back_version_and_new_objects(store):
    session, _ = origin(store)
    before = store.samples(session["id"])
    with store._connection() as connection:
        connection.execute("DROP TABLE agent_steps")
        connection.execute("DROP TABLE agent_runs")
        connection.execute("DROP TABLE experiments")
        connection.execute("DROP INDEX investigation_identity")
        connection.execute("PRAGMA user_version=2")
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO events(session_id,kind,at_s,text,created_at) "
            "VALUES('missing','annotation',0,'orphan',0)"
        )
    with pytest.raises(ValueError, match="foreign keys"):
        Store(store.path)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name IN ('experiments','investigation_identity')"
            ).fetchall()
            == []
        )
    assert store.samples(session["id"]) == before


def test_future_schema_version_is_not_modified(store):
    with store._connection() as connection:
        connection.execute("PRAGMA user_version=5")
    with pytest.raises(ValueError, match="newer"):
        Store(store.path)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
