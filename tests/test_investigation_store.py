"""Immutable investigation history and consistent evidence captured during acquisition."""

import base64
import copy
import hashlib
import json
import math
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from argos_studio.core import MAX_INVESTIGATION_BYTES, MAX_INVESTIGATIONS, Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "studio.sqlite3")


def report(context="Inspect reception continuity"):
    return {
        "schema_version": 1,
        "kind": "reception_quality",
        "algorithm_version": "reception-quality/1",
        "context": context,
        "window_s": {"start_s": 0.0, "end_s": 2.0},
        "outcome": "observed_gap",
        "summary": "One observed receipt interval exceeds the threshold.",
        "snapshot": {"sha256": "f" * 64, "sample_count": 2},
        "findings": [{"evidence_ids": ["gap-1"], "observation": "An interval is visible."}],
        "evidence": [{"id": "gap-1", "before_seq": 0, "after_seq": 1}],
        "tools": [{"name": "receipt_intervals", "version": 1}],
        "limitations": ["Receipt intervals do not establish a physical cause."],
    }


def measurement(elapsed_s):
    return {
        "source_time_s": 100 + elapsed_s,
        "elapsed_s": elapsed_s,
        "received_at": 2000 + elapsed_s,
        "roll_deg": 3.0,
        "pitch_deg": None,
        "gyro_x_deg_s": None,
    }


def packet(elapsed_s):
    return {
        "elapsed_s": elapsed_s,
        "received_at": 2000 + elapsed_s,
        "peer_host": "127.0.0.1",
        "peer_port": 14580,
        "payload": b"\xfe\x00\x01\xff",
        "disposition": "accepted",
        "details": {"frames": [{"type": "ATTITUDE", "system_id": 1, "component_id": 1}]},
        "samples": [measurement(elapsed_s)],
    }


def test_report_is_persistent_and_detached_from_mutable_caller_data(store):
    session = store.create_session("Inspection", "Original objective")
    original = report()
    saved = store.save_investigation(session["id"], original)
    expected = copy.deepcopy(saved)
    original["findings"][0]["observation"] = "Changed by caller"
    saved["evidence"][0]["before_seq"] = 42

    reopened = Store(store.path)
    restored = reopened.get_investigation(session["id"], saved["id"])
    assert restored == expected
    assert "report" not in restored
    assert reopened.get_session(session["id"])["objective"] == "Original objective"
    corrected = reopened.save_investigation(session["id"], report("Corrected objective"))
    assert corrected["id"] != saved["id"]
    assert reopened.get_investigation(session["id"], saved["id"]) == expected


def test_list_returns_recent_summaries_without_full_evidence(store):
    session = store.create_session("Inspection", "")
    with patch("argos_studio.core.time.time", side_effect=[1000, 1001]):
        first = store.save_investigation(session["id"], report("First context"))
        second = store.save_investigation(session["id"], report("Updated context"))
    summaries = store.list_investigations(session["id"])
    assert [item["id"] for item in summaries] == [second["id"], first["id"]]
    expected_fields = {
        "id",
        "session_id",
        "created_at",
        "kind",
        "algorithm_version",
        "window_s",
        "outcome",
        "summary",
        "context",
    }
    assert set(summaries[0]) == expected_fields
    assert summaries[0]["context"] == "Updated context"
    assert summaries[1]["context"] == "First context"


def test_investigations_are_isolated_by_session_and_unknown_ids_fail(store):
    first = store.create_session("First", "", status="completed")
    second = store.create_session("Second", "", status="completed")
    saved = store.save_investigation(first["id"], report())
    assert store.list_investigations(second["id"]) == []
    for session_id, investigation_id in (
        (second["id"], saved["id"]),
        (first["id"], "missing"),
        ("missing", saved["id"]),
    ):
        with pytest.raises(KeyError):
            store.get_investigation(session_id, investigation_id)
    for operation in (
        lambda: store.save_investigation("missing", report()),
        lambda: store.list_investigations("missing"),
        lambda: store.investigation_input("missing"),
    ):
        with pytest.raises(KeyError):
            operation()


def test_report_validation_preserves_storage_and_reserved_identity(store):
    session = store.create_session("Inspection", "")
    malformed = [
        None,
        [],
        {},
        report() | {"schema_version": True},
        report() | {"schema_version": 2},
        report() | {"schema_version": "1"},
        report() | {"snapshot": []},
        report() | {"findings": {"not": "a list"}},
        report() | {"evidence": [math.nan]},
        report() | {"tools": [b"raw"]},
        report() | {"context": "\ud800"},
    ]
    malformed.extend(report() | {key: "reserved"} for key in ("id", "session_id", "created_at"))
    for invalid in malformed:
        with pytest.raises(ValueError):
            store.save_investigation(session["id"], invalid)
    assert store.list_investigations(session["id"]) == []


def test_report_limit_counts_utf8_bytes_and_accepts_exact_boundary(store):
    session = store.create_session("Inspection", "")
    exact = report() | {"summary": ""}
    overhead = len(json.dumps(exact, allow_nan=False, ensure_ascii=False).encode("utf-8"))
    exact["summary"] = "x" * (MAX_INVESTIGATION_BYTES - overhead)
    store.save_investigation(session["id"], exact)
    exact["summary"] += "x"
    with pytest.raises(ValueError, match="4096 KiB"):
        store.save_investigation(session["id"], exact)
    exact["summary"] = "é" * (MAX_INVESTIGATION_BYTES // 2)
    with pytest.raises(ValueError, match="4096 KiB"):
        store.save_investigation(session["id"], exact)
    assert len(store.list_investigations(session["id"])) == 1


def test_fifty_report_limit_is_atomic_under_concurrent_writers_and_per_session(store):
    session = store.create_session("Inspection", "", status="completed")
    for _ in range(MAX_INVESTIGATIONS - 1):
        store.save_investigation(session["id"], report())
    barrier = threading.Barrier(2)

    def save_last():
        barrier.wait(timeout=5)
        try:
            return store.save_investigation(session["id"], report())
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(save_last) for _ in range(2)]
        results = [future.result(timeout=5) for future in futures]
    assert sum(result is not None for result in results) == 1
    assert len(store.list_investigations(session["id"])) == MAX_INVESTIGATIONS
    other = store.create_session("Other", "", status="completed")
    assert store.save_investigation(other["id"], report())["session_id"] == other["id"]


def test_evidence_input_has_all_samples_and_events_without_query_truncation(store):
    session = store.create_session("Synthetic", "Inspect")
    store.append_samples(session["id"], [measurement(index / 20) for index in range(5010)])
    annotation = store.annotate(session["id"], "Recorded observation", 3)
    evidence = store.investigation_input(session["id"])
    assert evidence["session"]["sample_count"] == 5010
    assert len(evidence["samples"]) == 5010
    assert evidence["samples"][-1]["seq"] == 5009
    assert evidence["events"] == [annotation]
    assert evidence["datagrams"] == []


def test_evidence_input_exposes_raw_hashes_and_metadata_without_payloads(store):
    session = store.create_session("UDP", "Inspect", source="mavlink-udp")
    store.append_datagram(session["id"], **packet(1))
    empty = packet(2) | {"payload": b"", "disposition": "invalid", "samples": []}
    store.append_datagram(session["id"], **empty)
    evidence = store.investigation_input(session["id"])
    raw = store.datagrams(session["id"])
    for item, datagram in zip(evidence["datagrams"], raw, strict=True):
        payload = base64.b64decode(datagram.pop("raw_base64"))
        assert item == datagram | {
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "raw_bytes": len(payload),
        }
        assert "payload" not in item
        assert "raw_base64" not in item
    json.dumps(evidence, allow_nan=False)


def test_evidence_input_is_consistent_when_acquisition_writes_during_read(store):
    session = store.create_session("UDP", "Inspect", source="mavlink-udp")
    store.append_datagram(session["id"], **packet(0))
    original_note = store.annotate(session["id"], "Original note", 0)
    reader = Store(store.path)
    read_started, write_finished = threading.Event(), threading.Event()
    original_require = reader._require_session

    def pause_after_session(connection, session_id):
        row = original_require(connection, session_id)
        read_started.set()
        assert write_finished.wait(timeout=5)
        return row

    with (
        patch.object(reader, "_require_session", side_effect=pause_after_session),
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        future = executor.submit(reader.investigation_input, session["id"])
        try:
            assert read_started.wait(timeout=5)
            store.append_datagram(session["id"], **packet(1))
            store.annotate(session["id"], "New note", 1)
            store.finish_session(session["id"], elapsed_s=2)
        finally:
            write_finished.set()
        frozen = future.result(timeout=5)
    assert frozen["session"]["status"] == "live"
    assert frozen["session"]["elapsed_s"] == 0
    assert frozen["session"]["sample_count"] == len(frozen["samples"]) == 1
    assert len(frozen["datagrams"]) == 1
    assert frozen["events"] == [original_note]
    current = store.investigation_input(session["id"])
    assert current["session"]["status"] == "completed"
    assert current["session"]["sample_count"] == len(current["datagrams"]) == 2


def test_version_one_migration_preserves_capture_and_adds_report_storage(store):
    session = store.create_session("UDP", "Inspect", source="mavlink-udp")
    store.append_datagram(session["id"], **packet(1))
    store.annotate(session["id"], "Before migration", 1)
    store.finish_session(session["id"], status="interrupted", elapsed_s=2)
    with store._connection() as connection:
        # Remove only v2 objects to recreate the exact prior version's schema.
        connection.execute("DROP TABLE investigations")
        connection.execute("PRAGMA user_version=1")
        before = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("sessions", "samples", "events", "datagrams")
        }
    migrated = Store(store.path)
    with migrated._connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        for table, rows in before.items():
            assert [
                tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")
            ] == rows
    saved = migrated.save_investigation(session["id"], report())
    assert Store(store.path).get_investigation(session["id"], saved["id"]) == saved


def test_additive_migration_failure_keeps_version_one_and_original_capture(store):
    session = store.create_session("UDP", "Inspect", source="mavlink-udp")
    store.append_datagram(session["id"], **packet(1))
    before = store.datagrams(session["id"])
    with store._connection() as connection:
        connection.execute("DROP TABLE investigations")
        connection.execute("PRAGMA user_version=1")
    # A preexisting orphan should be reported without committing the new table.
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO events (session_id,kind,at_s,text,created_at) "
            "VALUES ('missing','annotation',0,'Orphan',0)"
        )
    with pytest.raises(ValueError, match="foreign keys"):
        Store(store.path)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            connection.execute("SELECT 1 FROM sqlite_schema WHERE name='investigations'").fetchone()
            is None
        )
    assert store.datagrams(session["id"]) == before
