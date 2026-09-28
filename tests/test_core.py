"""Persistence, evidence boundaries, and atomicity of session storage."""

import base64
import json
import math
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from argos_studio.core import Store

LEGACY_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, objective TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('simulation','argos-recording')),
    status TEXT NOT NULL CHECK(status IN ('live','completed','interrupted')),
    metadata TEXT NOT NULL, created_at REAL NOT NULL, ended_at REAL,
    elapsed_s REAL NOT NULL DEFAULT 0 CHECK(elapsed_s >= 0)
);
CREATE UNIQUE INDEX one_live_simulation ON sessions(source)
    WHERE source='simulation' AND status='live';
CREATE TABLE samples (
    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
    seq INTEGER NOT NULL, source_time_s REAL NOT NULL, elapsed_s REAL NOT NULL,
    received_at REAL NOT NULL, roll_deg REAL, pitch_deg REAL, gyro_x_deg_s REAL,
    UNIQUE(session_id,seq)
);
CREATE INDEX sample_window ON samples(session_id,elapsed_s);
CREATE TABLE events (
    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
    kind TEXT NOT NULL, at_s REAL NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL
);
"""


def sample(elapsed_s=0.0, roll_deg=1.0, **overrides):
    result = {
        "source_time_s": elapsed_s + 100,
        "elapsed_s": elapsed_s,
        "received_at": 2000 + elapsed_s,
        "roll_deg": roll_deg,
        "pitch_deg": -0.2,
        "gyro_x_deg_s": 2.0,
    }
    result.update(overrides)
    return result


def datagram(elapsed_s=0.0, **overrides):
    result = {
        "elapsed_s": elapsed_s,
        "received_at": 2000 + elapsed_s,
        "peer_host": "127.0.0.1",
        "peer_port": 14580,
        "payload": b"\xfe\x00\xff\x01",
        "disposition": "accepted",
        "details": {"frames": [{"type": "ATTITUDE", "system_id": 1, "component_id": 1}]},
    }
    result.update(overrides)
    return result


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "nested" / "sessions.sqlite3"
        self.store = Store(self.path)

    def create(self):
        return self.store.create_session("Bench observation", "Inspect receipt continuity")

    def test_legacy_migration_preserves_rows_references_and_session_statuses(self):
        path = Path(self.directory.name) / "legacy.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.executescript(LEGACY_SCHEMA)
            connection.executemany(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        "recorded",
                        "Recording",
                        "Observe",
                        "argos-recording",
                        "completed",
                        '{"clock":"monotonic"}',
                        1,
                        3,
                        2,
                    ),
                    (
                        "interrupted",
                        "Simulation",
                        "Observe",
                        "simulation",
                        "interrupted",
                        "{}",
                        4,
                        None,
                        2,
                    ),
                    ("live", "Active", "Observe", "simulation", "live", "{}", 5, None, 1),
                ],
            )
            connection.execute(
                "INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?)",
                (20, "recorded", 0, 112, 1, 42.75, 12, None, 0),
            )
            connection.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?)",
                (31, "interrupted", "annotation", 2, "Existing note", 5),
            )
            before = {
                table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                for table in ("sessions", "samples", "events")
            }
        migrated = Store(path)
        with migrated._connection() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            for table, expected in before.items():
                actual = connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                self.assertEqual([tuple(row) for row in actual], expected)
            for table in ("samples", "events"):
                references = connection.execute(f"PRAGMA foreign_key_list({table})").fetchall()
                self.assertEqual(references[0]["table"], "sessions")
        self.assertEqual(migrated.get_session("interrupted")["status"], "interrupted")
        self.assertEqual(migrated.get_session("live")["status"], "live")
        self.assertEqual(migrated.recover_interrupted(), 1)
        migrated.create_session("UDP", "Observe", source="mavlink-udp")
        self.assertEqual(Store(path).get_session("recorded")["sample_count"], 1)

    def test_failed_legacy_migration_rolls_back_schema_and_rows(self):
        path = Path(self.directory.name) / "inconsistent-legacy.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.executescript(LEGACY_SCHEMA)
            connection.execute("INSERT INTO events VALUES (1,'missing','annotation',0,'Orphan',1)")
        with self.assertRaisesRegex(ValueError, "foreign keys"):
            Store(path)
        with sqlite3.connect(path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
            sql = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE name='sessions'"
            ).fetchone()[0]
            self.assertNotIn("mavlink-udp", sql)
            self.assertIsNone(
                connection.execute("SELECT 1 FROM sqlite_schema WHERE name='datagrams'").fetchone()
            )

    def test_simulation_and_mavlink_compete_for_one_active_acquisition(self):
        barrier = threading.Barrier(2)

        def start(source):
            barrier.wait(timeout=5)
            try:
                return self.store.create_session("Source", "Observe", source=source)
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(start, ["simulation", "mavlink-udp"]))
        accepted = [result for result in results if result is not None]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(self.store.list_sessions()), 1)
        self.store.finish_session(accepted[0]["id"])
        other = "mavlink-udp" if accepted[0]["source"] == "simulation" else "simulation"
        self.assertEqual(self.store.create_session("Next", "", source=other)["source"], other)

    def test_datagrams_preserve_raw_bytes_clocks_and_sample_links_after_reopen(self):
        session = self.store.create_session("UDP", "Inspect", source="mavlink-udp")
        first = self.store.append_datagram(
            session["id"], **datagram(1, samples=[sample(1), sample(1, roll_deg=5)])
        )
        second = self.store.append_datagram(session["id"], **datagram(2, samples=[sample(2)]))
        self.assertEqual(first, {"seq": 0, "sample_start_seq": 0, "sample_count": 2})
        self.assertEqual(second, {"seq": 1, "sample_start_seq": 2, "sample_count": 1})
        reopened = Store(self.path)
        rows = reopened.datagrams(session["id"])
        self.assertEqual([row["seq"] for row in rows], [0, 1])
        self.assertEqual(base64.b64decode(rows[0]["raw_base64"]), datagram()["payload"])
        self.assertNotIn("payload", rows[0])
        self.assertEqual(rows[0]["details"], datagram()["details"])
        self.assertEqual(rows[0]["elapsed_s"], 1)
        self.assertEqual(rows[0]["received_at"], 2001)
        self.assertEqual(rows[0]["peer_host"], "127.0.0.1")
        self.assertEqual(rows[0]["peer_port"], 14580)
        self.assertEqual(len(reopened.samples(session["id"])), 3)
        json.dumps(rows, allow_nan=False)
        expected = {"datagram_count": 2, "raw_bytes": 8, "dispositions": {"accepted": 2}}
        self.assertEqual(reopened.capture_summary(session["id"]), expected)
        self.assertEqual(reopened.snapshot(session["id"])["capture"], expected)

    def test_invalid_and_empty_datagrams_preserve_receipt_without_invented_samples(self):
        session = self.store.create_session("UDP", "Inspect", source="mavlink-udp")
        for index, disposition in enumerate(
            ("invalid", "signed", "foreign_source", "foreign_peer"), start=1
        ):
            result = self.store.append_datagram(
                session["id"], **datagram(index, payload=b"", disposition=disposition)
            )
            self.assertEqual(result["sample_count"], 0)
            self.assertIsNone(result["sample_start_seq"])
        self.assertEqual(self.store.get_session(session["id"])["elapsed_s"], 4)
        self.assertEqual(self.store.samples(session["id"]), [])
        self.assertEqual(self.store.analyze(session["id"])["duration_s"], 4)
        summary = self.store.capture_summary(session["id"])
        self.assertEqual(summary["datagram_count"], 4)
        self.assertEqual(summary["raw_bytes"], 0)
        self.assertEqual(summary["dispositions"]["invalid"], 1)

    def test_loopback_alias_evidence_retains_actual_peer_address(self):
        session = self.store.create_session("UDP", "Inspect", source="mavlink-udp")
        self.store.append_datagram(session["id"], **datagram(0, samples=[sample(0)]))
        self.store.append_datagram(
            session["id"],
            **datagram(1, peer_host="127.0.0.2", disposition="foreign_peer"),
        )
        rows = self.store.datagrams(session["id"])
        self.assertEqual([row["peer_host"] for row in rows], ["127.0.0.1", "127.0.0.2"])
        self.assertEqual(rows[1]["disposition"], "foreign_peer")
        self.assertEqual(rows[1]["sample_count"], 0)
        self.assertEqual(self.store.get_session(session["id"])["sample_count"], 1)

    def test_failed_datagram_write_rolls_back_derived_samples_and_duration(self):
        session = self.store.create_session("UDP", "Inspect", source="mavlink-udp")
        with self.store._connection() as connection:
            connection.execute(
                "CREATE TRIGGER reject_datagram BEFORE INSERT ON datagrams "
                "BEGIN SELECT RAISE(ABORT, 'test write failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.append_datagram(session["id"], **datagram(1, samples=[sample(1)]))
        self.assertEqual(self.store.samples(session["id"]), [])
        self.assertEqual(self.store.datagrams(session["id"]), [])
        self.assertEqual(self.store.get_session(session["id"])["elapsed_s"], 0)
        with self.store._connection() as connection:
            connection.execute("DROP TRIGGER reject_datagram")
        result = self.store.append_datagram(session["id"], **datagram(1, samples=[sample(1)]))
        self.assertEqual(result["sample_start_seq"], 0)
        self.assertEqual(result["seq"], 0)

    def test_datagram_validation_and_session_boundaries(self):
        simulation = self.create()
        with self.assertRaises(ValueError):
            self.store.append_datagram(simulation["id"], **datagram())
        self.assertIsNone(self.store.snapshot(simulation["id"])["capture"])
        self.store.finish_session(simulation["id"])
        session = self.store.create_session("UDP", "Inspect", source="mavlink-udp")
        invalid = [
            {"elapsed_s": -1},
            {"elapsed_s": math.nan},
            {"received_at": math.inf},
            {"peer_host": "0.0.0.0"},
            {"peer_host": "localhost"},
            {"peer_host": "128.0.0.1"},
            {"peer_host": "::1"},
            {"peer_host": "127.0.0.256"},
            {"peer_host": "127.1"},
            {"peer_host": "127.000.0.1"},
            {"peer_host": 2130706433},
            {"peer_port": 0},
            {"peer_port": 65536},
            {"peer_port": True},
            {"payload": "bytes"},
            {"payload": b"x" * 65536},
            {"disposition": "unknown"},
            {"details": {"bad": math.nan}},
            {"details": {"large": "x" * 65536}},
            {"samples": [sample(1)]},
            {"samples": [sample(received_at=4000)]},
            {"disposition": "invalid", "samples": [sample()]},
        ]
        for overrides in invalid:
            with self.subTest(fields=list(overrides)), self.assertRaises(ValueError):
                self.store.append_datagram(session["id"], **(datagram() | overrides))
        self.assertEqual(self.store.capture_summary(session["id"])["datagram_count"], 0)
        self.store.append_datagram(session["id"], **datagram(2))
        with self.assertRaisesRegex(ValueError, "receipt order"):
            self.store.append_datagram(session["id"], **datagram(1))
        self.store.finish_session(session["id"])
        with self.assertRaises(ValueError):
            self.store.append_datagram(session["id"], **datagram(3))

    def test_session_samples_and_annotations_survive_reopen(self):
        session = self.store.create_session(
            "Tilt",
            "Inspect a step",
            metadata={"received_at_clock": "UTC", "vehicle": "synthetic"},
        )
        first = self.store.append_sample(session["id"], **sample(0.1))
        last = self.store.append_sample(session["id"], **sample(0.15, roll_deg=-7.5))
        event = self.store.annotate(session["id"], "Observed tilt", at_s=0.12)
        self.store.finish_session(session["id"], elapsed_s=0.3)

        reopened = Store(self.path)
        saved = reopened.get_session(session["id"])
        self.assertEqual(saved["sample_count"], 2)
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["elapsed_s"], 0.3)
        self.assertEqual(saved["metadata"]["received_at_clock"], "UTC")
        self.assertIsNotNone(saved["ended_at"])
        self.assertEqual(reopened.samples(session["id"]), [first, last])
        self.assertEqual(reopened.events(session["id"]), [event])
        self.assertEqual(event["kind"], "annotation")
        self.assertEqual([first["seq"], last["seq"]], [0, 1])
        self.assertEqual(reopened.list_sessions()[0], saved)

    def test_only_one_live_simulation_and_recovery_preserves_unknown_stop_time(self):
        first = self.create()
        self.store.append_sample(first["id"], **sample(2))
        with self.assertRaisesRegex(ValueError, "already live"):
            self.create()
        reopened = Store(self.path)
        self.assertEqual(reopened.get_session(first["id"])["status"], "live")
        self.assertEqual(reopened.recover_interrupted(), 1)
        self.assertEqual(reopened.recover_interrupted(), 0)
        recovered = reopened.get_session(first["id"])
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(recovered["elapsed_s"], 2)
        self.assertIsNone(recovered["ended_at"])
        self.create()

    def test_gap_evidence_uses_elapsed_not_unsynchronized_clocks(self):
        session = self.create()
        self.store.append_samples(
            session["id"],
            [
                sample(0, roll_deg=1, source_time_s=800, received_at=10000),
                sample(0.05, roll_deg=-4, source_time_s=1, received_at=9999),
                sample(0.1, roll_deg=3, source_time_s=1.1, received_at=500),
                sample(1.1, roll_deg=10, source_time_s=1.2, received_at=100),
                sample(1.15, roll_deg=2, source_time_s=1.25, received_at=0),
            ],
        )
        analysis = self.store.analyze(session["id"])
        self.assertEqual(analysis["sample_count"], 5)
        self.assertAlmostEqual(analysis["median_interval_s"], 0.05)
        self.assertEqual(analysis["roll_min_deg"], -4)
        self.assertEqual(analysis["roll_max_deg"], 10)
        self.assertEqual(
            analysis["gaps"],
            [
                {
                    "before_seq": 2,
                    "after_seq": 3,
                    "start_s": 0.1,
                    "end_s": 1.1,
                    "duration_s": 1,
                    "window_overlap_s": 1,
                }
            ],
        )
        self.assertEqual(analysis["max_gap_s"], 1)
        self.assertIn("cause remains undetermined", analysis["conclusion"])

    def test_gap_crossing_selection_keeps_real_evidence_and_overlap(self):
        session = self.create()
        self.store.append_samples(session["id"], [sample(1), sample(3)])
        analysis = self.store.analyze(session["id"], start_s=1.5, end_s=2)
        self.assertEqual(analysis["sample_count"], 0)
        self.assertEqual(analysis["duration_s"], 0.5)
        self.assertEqual(analysis["observed_duration_s"], 0)
        self.assertEqual(
            analysis["gaps"][0],
            {
                "before_seq": 0,
                "after_seq": 1,
                "start_s": 1,
                "end_s": 3,
                "duration_s": 2,
                "window_overlap_s": 0.5,
            },
        )
        before = self.store.analyze(session["id"], start_s=0, end_s=1)
        after = self.store.analyze(session["id"], start_s=3, end_s=6)
        self.assertEqual(before["gaps"], [])
        self.assertEqual(after["gaps"], [])

    def test_no_inferred_first_or_final_gap_and_stop_time_retained(self):
        session = self.create()
        self.store.append_samples(session["id"], [sample(4), sample(4.05)])
        self.store.finish_session(session["id"], elapsed_s=8)
        analysis = self.store.analyze(session["id"])
        self.assertEqual(analysis["gaps"], [])
        self.assertEqual(analysis["duration_s"], 8)
        self.assertAlmostEqual(analysis["observed_duration_s"], 0.05)
        self.assertEqual(analysis["window_s"], {"start_s": 0, "end_s": 8})

    def test_empty_single_null_and_zero_width_windows(self):
        session = self.create()
        empty = self.store.analyze(session["id"])
        self.assertEqual(empty["sample_count"], 0)
        self.assertIsNone(empty["median_interval_s"])
        self.assertIsNone(empty["roll_min_deg"])
        self.store.append_sample(session["id"], **sample(2, roll_deg=None))
        single = self.store.analyze(session["id"], start_s=2, end_s=2)
        self.assertEqual(single["duration_s"], 0)
        self.assertEqual(single["sample_count"], 1)
        self.assertIsNone(single["roll_max_deg"])
        self.assertEqual(single["gaps"], [])
        beyond = self.store.analyze(session["id"], start_s=5)
        self.assertEqual(beyond["sample_count"], 0)
        self.assertEqual(beyond["window_s"], {"start_s": 5, "end_s": 5})

    def test_sample_window_bounds_are_inclusive_and_query_limit_does_not_limit_analysis(self):
        session = self.create()
        self.store.append_samples(session["id"], [sample(index / 20) for index in range(5010)])
        self.assertEqual(len(self.store.samples(session["id"])), 5000)
        self.assertEqual(self.store.analyze(session["id"])["sample_count"], 5010)
        self.assertEqual(len(self.store.snapshot(session["id"])["samples"]), 5010)
        selected = self.store.samples(session["id"], start_s=1, end_s=1.1)
        self.assertEqual([item["seq"] for item in selected], [20, 21, 22])
        self.assertEqual(len(self.store.samples(session["id"], limit=2)), 2)

    def test_snapshot_window_matches_analysis_and_retains_global_context(self):
        session = self.create()
        self.store.append_samples(session["id"], [sample(0), sample(1), sample(2), sample(3)])
        event = self.store.annotate(session["id"], "Before selected window", at_s=0)
        result = self.store.snapshot(session["id"], start_s=1, end_s=2)
        self.assertEqual(result["session"]["sample_count"], 4)
        self.assertEqual([row["seq"] for row in result["samples"]], [1, 2])
        self.assertEqual(result["analysis"]["sample_count"], 2)
        self.assertEqual(result["analysis"], self.store.analyze(session["id"], 1, 2))
        self.assertEqual(result["events"], [event])

    def test_snapshot_remains_consistent_when_acquisition_writes_during_read(self):
        session = self.store.create_session("UDP", "Observe", source="mavlink-udp")
        self.store.append_datagram(session["id"], **datagram(0, samples=[sample(0)]))
        first = self.store.samples(session["id"])[0]
        first_event = self.store.annotate(session["id"], "Initial observation", at_s=0)
        reader = Store(self.path)
        read_started = threading.Event()
        writes_finished = threading.Event()
        original_require = reader._require_session

        def pause_after_session_read(connection, session_id):
            row = original_require(connection, session_id)
            read_started.set()
            if not writes_finished.wait(timeout=5):
                raise TimeoutError("Concurrent test writer did not finish")
            return row

        with (
            patch.object(reader, "_require_session", side_effect=pause_after_session_read),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            future = executor.submit(reader.snapshot, session["id"])
            try:
                self.assertTrue(read_started.wait(timeout=5))
                self.store.append_datagram(session["id"], **datagram(1, samples=[sample(1)]))
                self.store.annotate(session["id"], "Concurrent observation", at_s=1)
                self.store.finish_session(session["id"], elapsed_s=2)
            finally:
                writes_finished.set()
            result = future.result(timeout=5)

        self.assertEqual(result["session"]["status"], "live")
        self.assertEqual(result["session"]["sample_count"], 1)
        self.assertEqual(result["session"]["elapsed_s"], 0)
        self.assertEqual(result["samples"], [first])
        self.assertEqual(result["events"], [first_event])
        self.assertEqual(result["analysis"]["sample_count"], 1)
        self.assertEqual(result["analysis"]["duration_s"], 0)
        self.assertEqual(result["capture"]["datagram_count"], 1)
        self.assertEqual(result["capture"]["raw_bytes"], 4)
        current = self.store.snapshot(session["id"])
        self.assertEqual(current["session"]["status"], "completed")
        self.assertEqual(current["session"]["sample_count"], 2)
        self.assertEqual(len(current["events"]), 2)
        self.assertEqual(current["capture"]["datagram_count"], 2)
        self.assertEqual(current["capture"]["raw_bytes"], 8)

    def test_bulk_append_rolls_back_all_records_on_invalid_order(self):
        session = self.create()
        self.store.append_sample(session["id"], **sample(1))
        with self.assertRaisesRegex(ValueError, "receipt order"):
            self.store.append_samples(session["id"], [sample(2), sample(0.5)])
        self.assertEqual(self.store.get_session(session["id"])["sample_count"], 1)
        self.assertEqual(self.store.get_session(session["id"])["elapsed_s"], 1)
        appended = self.store.append_sample(session["id"], **sample(2))
        self.assertEqual(appended["seq"], 1)

    def test_import_is_atomic_and_preserves_clock_and_missing_measurements(self):
        with self.assertRaises(ValueError):
            self.store.import_session("Broken", "", [sample(2), sample(1)])
        self.assertEqual(self.store.list_sessions(), [])
        with self.assertRaises(ValueError):
            self.store.import_session("Empty", "", [])
        self.assertEqual(self.store.list_sessions(), [])
        imported = self.store.import_session(
            "Recording",
            "Inspect",
            [
                sample(0, received_at=0.25, gyro_x_deg_s=None),
                sample(0.1, received_at=0.35, gyro_x_deg_s=None),
            ],
            metadata={"received_at_clock": "argos_monotonic"},
        )
        self.assertEqual(imported["source"], "argos-recording")
        self.assertEqual(imported["status"], "completed")
        records = self.store.samples(imported["id"])
        self.assertEqual(records[0]["received_at"], 0.25)
        self.assertIsNone(records[0]["gyro_x_deg_s"])
        self.assertFalse(
            any("synthetic" in item for item in self.store.analyze(imported["id"])["limitations"])
        )

    def test_recording_duration_preserves_trailing_silence_and_is_validated_atomically(self):
        with self.assertRaises(ValueError):
            self.store.import_session("Bad duration", "", [sample(0), sample(2)], duration_s=1)
        self.assertEqual(self.store.list_sessions(), [])
        imported = self.store.import_session("Duration", "", [sample(0), sample(2)], duration_s=8)
        self.assertEqual(imported["elapsed_s"], 8)
        analysis = self.store.analyze(imported["id"])
        self.assertEqual(analysis["duration_s"], 8)
        self.assertEqual(analysis["observed_duration_s"], 2)
        self.assertEqual(len(analysis["gaps"]), 1)

    def test_finished_session_cannot_accept_samples_or_extend_annotations(self):
        session = self.create()
        self.store.append_sample(session["id"], **sample(1))
        with self.assertRaises(ValueError):
            self.store.finish_session(session["id"], elapsed_s=0.5)
        completed = self.store.finish_session(session["id"], elapsed_s=2)
        self.assertEqual(self.store.finish_session(session["id"]), completed)
        with self.assertRaises(ValueError):
            self.store.append_sample(session["id"], **sample(3))
        with self.assertRaises(ValueError):
            self.store.annotate(session["id"], "Past end", at_s=3)
        self.store.annotate(session["id"], "Replay annotation", at_s=1)

    def test_concurrent_appends_keep_unique_ordered_sequences(self):
        session = self.create()
        # Equal monotonic times can occur in batched input. They are not missing data.
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(self.store.append_sample, session["id"], **sample(1))
                for _ in range(30)
            ]
            for future in futures:
                future.result()
        samples = self.store.samples(session["id"])
        self.assertEqual([row["seq"] for row in samples], list(range(30)))
        self.assertEqual(self.store.analyze(session["id"])["gaps"], [])

    def test_database_foreign_key_constraints_enabled(self):
        with self.store._connection() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO events (session_id,kind,at_s,text,created_at) VALUES (?,?,?,?,?)",
                    ("missing", "annotation", 0, "Text", 0),
                )

    def test_unknown_sessions_raise_without_creating_data(self):
        methods = [
            lambda: self.store.get_session("missing"),
            lambda: self.store.snapshot("missing"),
            lambda: self.store.samples("missing"),
            lambda: self.store.events("missing"),
            lambda: self.store.analyze("missing"),
            lambda: self.store.annotate("missing", "Text", 0),
            lambda: self.store.finish_session("missing"),
            lambda: self.store.append_sample("missing", **sample()),
            lambda: self.store.append_datagram("missing", **datagram()),
            lambda: self.store.datagrams("missing"),
            lambda: self.store.capture_summary("missing"),
        ]
        for method in methods:
            with self.subTest(method=method), self.assertRaises(KeyError):
                method()
        self.assertEqual(self.store.list_sessions(), [])

    def test_invalid_numbers_bounds_and_metadata_rejected(self):
        session = self.create()
        for field in sample():
            for invalid in (math.nan, math.inf, -math.inf, True, "2"):
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                    values = sample()
                    values[field] = invalid
                    self.store.append_sample(session["id"], **values)
        for start, end in ((-1, 1), (2, 1), (math.nan, 2), (0, math.inf)):
            for method in (self.store.samples, self.store.analyze, self.store.snapshot):
                with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                    method(session["id"], start_s=start, end_s=end)
        for limit in (0, -1, 100001, True, 1.5):
            with self.assertRaises(ValueError):
                self.store.samples(session["id"], limit=limit)
        for threshold in (0, -1, math.inf):
            with self.assertRaises(ValueError):
                self.store.analyze(session["id"], gap_threshold_s=threshold)
        for metadata in (
            ["not an object"],
            {"bad": math.nan},
            {"bad": object()},
            {"large": "x" * 65536},
        ):
            with self.assertRaises(ValueError):
                self.store.create_session(
                    "Metadata", "", source="argos-recording", metadata=metadata
                )
        with self.assertRaises(ValueError):
            self.store.annotate(session["id"], "", 0)
        self.assertEqual(self.store.get_session(session["id"])["sample_count"], 0)


if __name__ == "__main__":
    unittest.main()
