"""Incremental receipt-gap observations preserve evidence and investigation identity."""

import copy
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from argos_studio.core import (
    MAX_INVESTIGATIONS,
    MAX_OBSERVATIONS,
    OBSERVATION_GAP_THRESHOLD_S,
    OBSERVATION_RULE_VERSION,
    Store,
)
from argos_studio.investigation import build_report


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "studio.sqlite3")


def measurement(elapsed, *, source_time=None):
    return {
        "source_time_s": elapsed + 100 if source_time is None else source_time,
        "elapsed_s": elapsed,
        "received_at": 1000 + elapsed,
        "roll_deg": 3,
        "pitch_deg": None,
        "gyro_x_deg_s": None,
    }


def session(store, times=(0, 0.25, 1), *, source="simulation"):
    saved = store.create_session("Receipt evidence", "Inspect continuity", source=source)
    store.append_samples(saved["id"], [measurement(value) for value in times])
    return saved


def report_for(store, observation):
    return build_report(
        store.investigation_input(observation["session_id"]),
        start_s=observation["start_s"],
        end_s=observation["end_s"],
        context="Inspect this observed receipt interval",
    )


@pytest.mark.parametrize("source", ["simulation", "argos-recording", "mavlink-udp"])
def test_gap_proof_matches_actual_samples_and_survives_reopen(store, source):
    saved = session(store, source=source)
    result = store.scan_observations(saved["id"])
    assert result["created_count"] == 1
    assert result["scan"] == {
        "last_seq": 2,
        "last_sample_seq": 2,
        "complete": True,
        "omitted_gap_count": 0,
    }
    observation = result["items"][0]
    samples = store.samples(saved["id"])
    assert observation["kind"] == "receipt_gap"
    assert observation["rule_version"] == OBSERVATION_RULE_VERSION
    assert observation["source"] == source
    assert observation["disposition"] == "open"
    assert observation["investigation_id"] is None
    assert (observation["before_seq"], observation["after_seq"]) == (1, 2)
    assert (observation["start_s"], observation["end_s"], observation["duration_s"]) == (
        0.25,
        1.0,
        0.75,
    )
    assert observation["evidence"] == {
        "before_sample": samples[1],
        "after_sample": samples[2],
        "source": source,
        "status_at_detection": "live",
        "threshold_s": OBSERVATION_GAP_THRESHOLD_S,
    }
    store.finish_session(saved["id"], elapsed_s=8)
    store.annotate(saved["id"], "Later note", 0.5)
    restored = Store(store.path)
    assert restored.get_observation(saved["id"], observation["id"]) == observation
    observation["evidence"]["before_sample"]["roll_deg"] = 999
    assert restored.get_observation(saved["id"], observation["id"])["evidence"] == {
        "before_sample": samples[1],
        "after_sample": samples[2],
        "source": source,
        "status_at_detection": "live",
        "threshold_s": OBSERVATION_GAP_THRESHOLD_S,
    }


def test_empty_single_and_equal_threshold_do_not_invent_silence_gaps(store):
    saved = session(store, times=())
    assert store.scan_observations(saved["id"])["items"] == []
    assert store.list_observations(saved["id"])["scan"]["last_seq"] == -1
    store.append_sample(saved["id"], **measurement(10))
    assert store.scan_observations(saved["id"])["items"] == []
    store.append_sample(saved["id"], **measurement(10.25, source_time=1))
    assert store.scan_observations(saved["id"])["items"] == []
    store.finish_session(saved["id"], elapsed_s=100)
    assert store.scan_observations(saved["id"])["items"] == []


def test_batches_include_previous_endpoint_and_new_live_samples(store):
    saved = session(store, times=(0, 1, 2))
    first = store.scan_observations(saved["id"], limit=1)
    assert first["items"] == []
    assert first["scan"]["last_seq"] == 0
    assert not first["scan"]["complete"]
    second = store.scan_observations(saved["id"], limit=1)
    assert second["created_count"] == 1
    assert second["items"][0]["before_seq"] == 0
    third = store.scan_observations(saved["id"], limit=1)
    assert third["created_count"] == 1
    assert third["scan"]["complete"]
    assert [item["before_seq"] for item in third["items"]] == [0, 1]
    store.append_sample(saved["id"], **measurement(3))
    assert not store.list_observations(saved["id"])["scan"]["complete"]
    last = Store(store.path).scan_observations(saved["id"], limit=1)
    assert last["created_count"] == 1
    assert last["scan"]["last_seq"] == 3
    assert last["items"][-1]["before_seq"] == 2
    assert store.scan_observations(saved["id"])["created_count"] == 0


def test_observation_pairs_agree_with_existing_replay_analysis(store):
    saved = session(store, times=(0, 0.1, 0.1, 0.35, 0.600001, 4, 4.05))
    result = store.scan_observations(saved["id"])
    analysis = store.analyze(saved["id"])
    fields = ("before_seq", "after_seq", "start_s", "end_s", "duration_s")
    assert [{key: item[key] for key in fields} for item in result["items"]] == [
        {key: gap[key] for key in fields} for gap in analysis["gaps"]
    ]


def test_cap_continues_scan_and_counts_omitted_gaps_only_once(store):
    saved = session(store, times=range(MAX_OBSERVATIONS + 6))
    result = store.scan_observations(saved["id"])
    assert len(result["items"]) == MAX_OBSERVATIONS
    assert result["scan"]["omitted_gap_count"] == 5
    assert result["scan"]["complete"]
    observation = result["items"][0]
    store.set_observation_disposition(saved["id"], observation["id"], "dismissed")
    store.append_sample(saved["id"], **measurement(MAX_OBSERVATIONS + 6))
    result = Store(store.path).scan_observations(saved["id"])
    assert len(result["items"]) == MAX_OBSERVATIONS
    assert result["items"][0]["disposition"] == "dismissed"
    assert result["scan"]["omitted_gap_count"] == 6
    assert store.scan_observations(saved["id"])["scan"]["omitted_gap_count"] == 6


def test_scan_and_cursor_rollback_together_on_failed_insert(store):
    saved = session(store, times=(0, 1, 2))
    with store._connection() as connection:
        connection.execute(
            "CREATE TRIGGER fail_second_observation BEFORE INSERT ON observations "
            "WHEN NEW.before_seq=1 BEGIN SELECT RAISE(ABORT, 'injected write failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        store.scan_observations(saved["id"])
    assert store.list_observations(saved["id"]) == {
        "items": [],
        "scan": {"last_seq": -1, "last_sample_seq": 2, "complete": False, "omitted_gap_count": 0},
    }
    with store._connection() as connection:
        connection.execute("DROP TRIGGER fail_second_observation")
    assert store.scan_observations(saved["id"])["created_count"] == 2


def test_concurrent_scans_count_each_gap_once(store):
    saved = session(store, times=range(30))
    barrier = threading.Barrier(2)

    def scan():
        barrier.wait(timeout=5)
        return store.scan_observations(saved["id"])

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result(timeout=5) for future in [executor.submit(scan) for _ in range(2)]]
    assert sum(result["created_count"] for result in results) == MAX_OBSERVATIONS
    assert results[0]["scan"] == results[1]["scan"]
    assert results[0]["scan"]["omitted_gap_count"] == 9


def test_list_is_a_coherent_snapshot_during_live_write(store):
    saved = session(store)
    old = store.scan_observations(saved["id"])
    old.pop("created_count")
    reader = Store(store.path)
    captured, updated = threading.Event(), threading.Event()
    original = reader._require_session

    def pause(connection, session_id):
        result = original(connection, session_id)
        captured.set()
        assert updated.wait(timeout=5)
        return result

    with patch.object(reader, "_require_session", side_effect=pause), ThreadPoolExecutor() as pool:
        result = pool.submit(reader.list_observations, saved["id"])
        try:
            assert captured.wait(timeout=5)
            store.append_sample(saved["id"], **measurement(2))
            store.scan_observations(saved["id"])
        finally:
            updated.set()
        assert result.result(timeout=5) == old
    assert len(store.list_observations(saved["id"])["items"]) == 2


def test_disposition_is_reversible_idempotent_and_does_not_erase_evidence(store):
    saved = session(store)
    observation = store.scan_observations(saved["id"])["items"][0]
    dismissed = store.set_observation_disposition(saved["id"], observation["id"], "dismissed")
    assert dismissed == {**observation, "disposition": "dismissed"}
    assert (
        store.set_observation_disposition(saved["id"], observation["id"], "dismissed") == dismissed
    )
    assert Store(store.path).scan_observations(saved["id"])["items"] == [dismissed]
    assert store.set_observation_disposition(saved["id"], observation["id"], "open") == observation


@pytest.mark.parametrize("limit", [0, -1, 2001, 1.0, True, "10", None])
def test_scan_limits_are_validated_before_any_write(store, limit):
    saved = session(store)
    with pytest.raises(ValueError, match="limit"):
        store.scan_observations(saved["id"], limit=limit)
    assert store.list_observations(saved["id"])["scan"]["last_seq"] == -1


def test_session_isolation_and_unknown_ids(store):
    first = session(store)
    observation = store.scan_observations(first["id"])["items"][0]
    store.finish_session(first["id"])
    other = session(store)
    for operation in (
        lambda: store.scan_observations("missing"),
        lambda: store.list_observations("missing"),
        lambda: store.get_observation(first["id"], "missing"),
        lambda: store.get_observation(other["id"], observation["id"]),
        lambda: store.set_observation_disposition(other["id"], observation["id"], "dismissed"),
        lambda: store.save_investigation(
            other["id"], report_for(store, observation), observation_id=observation["id"]
        ),
    ):
        with pytest.raises(KeyError):
            operation()
    with pytest.raises(ValueError, match="disposition"):
        store.set_observation_disposition(first["id"], observation["id"], "done")
    assert store.get_observation(first["id"], observation["id"]) == observation


def test_linked_report_is_saved_once_and_keeps_dismissal(store):
    saved = session(store)
    observation = store.scan_observations(saved["id"])["items"][0]
    store.set_observation_disposition(saved["id"], observation["id"], "dismissed")
    report = report_for(store, observation)
    linked = store.save_investigation(saved["id"], report, observation_id=observation["id"])
    assert store.get_observation(saved["id"], observation["id"]) == {
        **observation,
        "disposition": "dismissed",
        "investigation_id": linked["id"],
    }
    store.append_sample(saved["id"], **measurement(2))
    repeat = Store(store.path).save_investigation(
        saved["id"], report_for(store, observation), observation_id=observation["id"]
    )
    assert repeat == linked
    assert len(store.list_investigations(saved["id"])) == 1
    assert "report" not in repeat


def test_concurrent_report_creation_returns_one_persisted_link(store):
    saved = session(store)
    observation = store.scan_observations(saved["id"])["items"][0]
    report = report_for(store, observation)
    barrier = threading.Barrier(2)

    def save():
        barrier.wait(timeout=5)
        return store.save_investigation(saved["id"], report, observation_id=observation["id"])

    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = [executor.submit(save) for _ in range(2)]
        results = [future.result(timeout=5) for future in pending]
    assert results[0] == results[1]
    assert len(store.list_investigations(saved["id"])) == 1


@pytest.mark.parametrize(
    "change",
    [
        lambda report: report["window_s"].update(start_s=0),
        lambda report: report["window_s"].update(end_s=2),
        lambda report: report["window_s"].update(start_s=False),
        lambda report: report["snapshot"].update(source="mavlink-udp"),
        lambda report: report["snapshot"]["session"].update(id="other-session"),
        lambda report: report["snapshot"]["session"].update(source="argos-recording"),
        lambda report: report["snapshot"].update(session=None),
        lambda report: report.update(kind="arbitrary_report"),
    ],
)
def test_report_link_rejects_wrong_window_or_provenance_without_half_save(store, change):
    saved = session(store)
    observation = store.scan_observations(saved["id"])["items"][0]
    report = copy.deepcopy(report_for(store, observation))
    change(report)
    with pytest.raises(ValueError):
        store.save_investigation(saved["id"], report, observation_id=observation["id"])
    assert store.get_observation(saved["id"], observation["id"])["investigation_id"] is None
    assert store.list_investigations(saved["id"]) == []


def test_report_cap_failure_leaves_observation_unlinked_but_existing_link_is_reusable(store):
    saved = session(store, times=(0, 1, 2))
    first, second = store.scan_observations(saved["id"])["items"]
    report = report_for(store, first)
    linked = store.save_investigation(saved["id"], report, observation_id=first["id"])
    for _ in range(MAX_INVESTIGATIONS - 1):
        store.save_investigation(saved["id"], report)
    with pytest.raises(ValueError, match="at most"):
        store.save_investigation(
            saved["id"], report_for(store, second), observation_id=second["id"]
        )
    assert store.get_observation(saved["id"], second["id"])["investigation_id"] is None
    assert store.save_investigation(saved["id"], report, observation_id=first["id"]) == linked
    assert len(store.list_investigations(saved["id"])) == MAX_INVESTIGATIONS


def test_failed_link_rolls_back_inserted_report(store):
    saved = session(store)
    observation = store.scan_observations(saved["id"])["items"][0]
    with store._connection() as connection:
        connection.execute(
            "CREATE TRIGGER fail_observation_link "
            "BEFORE UPDATE OF investigation_id ON observations "
            "BEGIN SELECT RAISE(ABORT,'link failed'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="link failed"):
        store.save_investigation(
            saved["id"], report_for(store, observation), observation_id=observation["id"]
        )
    assert store.list_investigations(saved["id"]) == []
    assert store.get_observation(saved["id"], observation["id"]) == observation


def test_database_constraints_reject_foreign_sample_and_report_links(store):
    saved = session(store)
    observation = store.scan_observations(saved["id"])["items"][0]
    store.finish_session(saved["id"])
    other = session(store)
    other_report = store.save_investigation(
        other["id"], build_report(store.investigation_input(other["id"]))
    )
    with store._connection() as connection:
        for field, value in (
            ("before_seq", 10),
            ("after_seq", 99),
            ("investigation_id", other_report["id"]),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    f"UPDATE observations SET {field}=? WHERE id=?", (value, observation["id"])
                )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM samples WHERE session_id=? AND seq=1", (saved["id"],))
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_version_four_migration_preserves_all_existing_records(store):
    saved = session(store)
    store.annotate(saved["id"], "Retain evidence", 0.5)
    report = store.save_investigation(
        saved["id"], build_report(store.investigation_input(saved["id"]))
    )
    store.create_experiment(saved["id"], report["id"], {"kind": "fixture"})
    run = store.create_agent_run(saved["id"], "Inspect", "test-double", "scripted-fixture")
    store.append_agent_step(saved["id"], run["id"], "tool_result", {"ok": True})
    store.finish_agent_run(saved["id"], run["id"], "completed", answer="Fixture answer")
    tables = (
        "sessions",
        "samples",
        "events",
        "datagrams",
        "investigations",
        "experiments",
        "agent_runs",
        "agent_steps",
    )
    with store._connection() as connection:
        connection.execute("DROP TABLE observations")
        connection.execute("DROP TABLE observation_scans")
        connection.execute("PRAGMA user_version=4")
        before = {
            table: [
                tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
            for table in tables
        }
    migrated = Store(store.path)
    with migrated._connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        for table in tables:
            assert [
                tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ] == before[table]
    assert migrated.scan_observations(saved["id"])["created_count"] == 1


def test_failed_migration_preserves_version_four_without_partial_tables(store):
    saved = session(store)
    with store._connection() as connection:
        connection.execute("DROP TABLE observations")
        connection.execute("DROP TABLE observation_scans")
        connection.execute("PRAGMA user_version=4")
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO events(session_id,kind,at_s,text,created_at) "
            "VALUES('missing','annotation',0,'Orphan',0)"
        )
    with pytest.raises(ValueError, match="foreign keys"):
        Store(store.path)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("SELECT id FROM sessions").fetchone()[0] == saved["id"]
        assert (
            connection.execute(
                "SELECT name FROM sqlite_schema WHERE name IN ('observations','observation_scans')"
            ).fetchall()
            == []
        )
