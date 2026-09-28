"""Persistent session data and deterministic, clock-aware telemetry analysis."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import sqlite3
import statistics
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from ipaddress import AddressValueError, IPv4Address
from itertools import pairwise
from pathlib import Path
from typing import Any
from uuid import uuid4

SOURCES = {"simulation", "argos-recording", "mavlink-udp"}
STATUSES = {"live", "completed", "interrupted"}
DATAGRAM_DISPOSITIONS = {"accepted", "invalid", "signed", "foreign_source", "foreign_peer"}
MAX_INVESTIGATIONS = 50
MAX_INVESTIGATION_BYTES = 4 * 1024 * 1024
SAMPLE_FIELDS = (
    "source_time_s",
    "elapsed_s",
    "received_at",
    "roll_deg",
    "pitch_deg",
    "gyro_x_deg_s",
)


def _number(value: Any, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        value = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(value) or (minimum is not None and value < minimum):
        raise ValueError(f"{name} must be finite and at least {minimum}")
    return value


def _text(value: Any, name: str, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{name} must be text of at most {limit} characters")
    value = value.strip()
    if not empty and not value:
        raise ValueError(f"{name} must not be empty")
    return value


def _json_object(value: dict[str, Any] | None, name: str, *, max_bytes: int = 65536) -> str:
    if value is not None and not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    try:
        encoded = json.dumps(value or {}, allow_nan=False, ensure_ascii=False)
        size = len(encoded.encode("utf-8"))
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise ValueError(f"{name} must contain finite JSON data") from exc
    if size > max_bytes:
        raise ValueError(f"{name} exceeds {max_bytes // 1024} KiB")
    return encoded


class Store:
    """A local SQLite store with one connection per operation.

    ``elapsed_s`` is receipt time relative to the session on a monotonic clock.
    Other timestamp fields retain their source clock; metadata identifies it.
    Neither UTC receipt times nor source clocks are assumed to be synchronized.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) == ":memory:":
            raise ValueError("Store needs a file path for durable storage")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            self._initialize_schema(connection)

    @staticmethod
    def _initialize_schema(connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > 2:
            raise ValueError(f"Database schema version {version} requires a newer ARGOS Studio")
        if version == 2:
            return
        if version == 1:
            Store._migrate_investigations(connection)
            return
        # Build the replacement before dropping the old table. Renaming the old
        # table first would rewrite the foreign keys in samples and events.
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing = connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='sessions'"
            ).fetchone()
            connection.execute(
                """
                CREATE TABLE sessions_v1 (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    source TEXT NOT NULL
                        CHECK(source IN ('simulation','argos-recording','mavlink-udp')),
                    status TEXT NOT NULL CHECK(status IN ('live','completed','interrupted')),
                    metadata TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    ended_at REAL,
                    elapsed_s REAL NOT NULL DEFAULT 0 CHECK(elapsed_s >= 0)
                )
                """
            )
            if existing:
                connection.execute(
                    "INSERT INTO sessions_v1 "
                    "(id,name,objective,source,status,metadata,created_at,ended_at,elapsed_s) "
                    "SELECT id,name,objective,source,status,metadata,created_at,ended_at,elapsed_s "
                    "FROM sessions"
                )
                connection.execute("DROP TABLE sessions")
            connection.execute("ALTER TABLE sessions_v1 RENAME TO sessions")
            connection.execute(
                "CREATE UNIQUE INDEX one_live_acquisition ON sessions((1)) "
                "WHERE status='live' AND source IN ('simulation','mavlink-udp')"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS samples (
                    id INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    seq INTEGER NOT NULL,
                    source_time_s REAL NOT NULL,
                    elapsed_s REAL NOT NULL,
                    received_at REAL NOT NULL,
                    roll_deg REAL,
                    pitch_deg REAL,
                    gyro_x_deg_s REAL,
                    UNIQUE(session_id, seq)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS sample_window ON samples(session_id, elapsed_s)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    kind TEXT NOT NULL,
                    at_s REAL NOT NULL,
                    text TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE datagrams (
                    id INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    seq INTEGER NOT NULL,
                    elapsed_s REAL NOT NULL,
                    received_at REAL NOT NULL,
                    peer_host TEXT NOT NULL,
                    peer_port INTEGER NOT NULL,
                    payload BLOB NOT NULL,
                    disposition TEXT NOT NULL,
                    details TEXT NOT NULL,
                    sample_start_seq INTEGER,
                    sample_count INTEGER NOT NULL,
                    UNIQUE(session_id, seq),
                    FOREIGN KEY (session_id,sample_start_seq) REFERENCES samples(session_id,seq)
                )
                """
            )
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("Database migration found inconsistent foreign keys")
            connection.execute("PRAGMA user_version=1")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys=ON")
        Store._migrate_investigations(connection)

    @staticmethod
    def _migrate_investigations(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "CREATE TABLE investigations ("
                "id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), "
                "created_at REAL NOT NULL, report TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX investigation_session ON investigations(session_id,created_at DESC)"
            )
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("Database migration found inconsistent foreign keys")
            connection.execute("PRAGMA user_version=2")
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _require_session(connection: sqlite3.Connection, session_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown session: {session_id}")
        return row

    @staticmethod
    def _session(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["metadata"] = json.loads(result["metadata"])
        return result

    @staticmethod
    def _session_values(
        name: str, objective: str, source: str, metadata: dict[str, Any] | None, status: str
    ) -> tuple:
        name = _text(name, "name", 200)
        objective = _text(objective, "objective", 4000, empty=True)
        if source not in SOURCES or status not in STATUSES:
            raise ValueError("Unsupported session source or status")
        encoded = _json_object(metadata, "metadata")
        now = time.time()
        return (
            str(uuid4()),
            name,
            objective,
            source,
            status,
            encoded,
            now,
            None if status == "live" else now,
        )

    @staticmethod
    def _insert_session(connection: sqlite3.Connection, values: tuple) -> None:
        try:
            connection.execute(
                "INSERT INTO sessions "
                "(id,name,objective,source,status,metadata,created_at,ended_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                values,
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("An acquisition session is already live") from exc

    def create_session(
        self,
        name: str,
        objective: str,
        source: str = "simulation",
        metadata: dict[str, Any] | None = None,
        status: str = "live",
    ) -> dict[str, Any]:
        values = self._session_values(name, objective, source, metadata, status)
        with self._connection() as connection:
            self._insert_session(connection, values)
        return self.get_session(values[0])

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT sessions.*, (SELECT COUNT(*) FROM samples WHERE session_id=sessions.id) "
                "AS sample_count FROM sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown session: {session_id}")
            return self._session(row)

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT sessions.*, (SELECT COUNT(*) FROM samples WHERE session_id=sessions.id) "
                "AS sample_count FROM sessions ORDER BY created_at DESC, id"
            )
            return [self._session(row) for row in rows]

    def investigation_input(self, session_id: str) -> dict[str, Any]:
        """Freeze complete evidence for an investigation without exposing raw bytes."""
        with self._connection() as connection:
            connection.execute("BEGIN")
            session = self._session(self._require_session(connection, session_id))
            samples = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM samples WHERE session_id=? ORDER BY seq", (session_id,)
                )
            ]
            events = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM events WHERE session_id=? ORDER BY at_s,id", (session_id,)
                )
            ]
            datagrams = []
            for row in connection.execute(
                "SELECT * FROM datagrams WHERE session_id=? ORDER BY seq", (session_id,)
            ):
                item = dict(row)
                payload = item.pop("payload")
                item["payload_sha256"] = hashlib.sha256(payload).hexdigest()
                item["raw_bytes"] = len(payload)
                item["details"] = json.loads(item["details"])
                datagrams.append(item)
        session["sample_count"] = len(samples)
        return {"session": session, "samples": samples, "events": events, "datagrams": datagrams}

    @staticmethod
    def _investigation_report(report: dict[str, Any]) -> str:
        if not isinstance(report, dict):
            raise ValueError("report must be an object")
        if {"id", "session_id", "created_at"}.intersection(report):
            raise ValueError("report contains reserved investigation fields")
        if type(report.get("schema_version")) is not int or report["schema_version"] != 1:
            raise ValueError("Unsupported investigation report schema_version")
        fields = {
            "kind": str,
            "algorithm_version": str,
            "context": str,
            "window_s": dict,
            "outcome": str,
            "summary": str,
            "snapshot": dict,
            "findings": list,
            "evidence": list,
            "tools": list,
            "limitations": list,
        }
        for field, expected_type in fields.items():
            if not isinstance(report.get(field), expected_type):
                raise ValueError(f"report.{field} must be a {expected_type.__name__}")
        return _json_object(report, "report", max_bytes=MAX_INVESTIGATION_BYTES)

    def save_investigation(self, session_id: str, report: dict[str, Any]) -> dict[str, Any]:
        """Append an immutable report; a changed context requires a new report."""
        encoded = self._investigation_report(report)
        investigation_id, created_at = str(uuid4()), time.time()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_session(connection, session_id)
            count = connection.execute(
                "SELECT COUNT(*) FROM investigations WHERE session_id=?", (session_id,)
            ).fetchone()[0]
            if count >= MAX_INVESTIGATIONS:
                raise ValueError(
                    f"A session may contain at most {MAX_INVESTIGATIONS} investigations"
                )
            connection.execute(
                "INSERT INTO investigations (id,session_id,created_at,report) VALUES (?,?,?,?)",
                (investigation_id, session_id, created_at, encoded),
            )
        return {
            "id": investigation_id,
            "session_id": session_id,
            "created_at": created_at,
            **json.loads(encoded),
        }

    def list_investigations(self, session_id: str) -> list[dict[str, Any]]:
        summary_fields = (
            "kind",
            "algorithm_version",
            "window_s",
            "outcome",
            "summary",
            "context",
        )
        with self._connection() as connection:
            self._require_session(connection, session_id)
            result = []
            for row in connection.execute(
                "SELECT * FROM investigations WHERE session_id=? ORDER BY created_at DESC,id DESC",
                (session_id,),
            ):
                item = dict(row)
                report = json.loads(item.pop("report"))
                result.append({**item, **{field: report[field] for field in summary_fields}})
            return result

    def get_investigation(self, session_id: str, investigation_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            self._require_session(connection, session_id)
            row = connection.execute(
                "SELECT * FROM investigations WHERE session_id=? AND id=?",
                (session_id, investigation_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown investigation: {investigation_id}")
            result = dict(row)
            report = json.loads(result.pop("report"))
            return {**result, **report}

    def snapshot(
        self,
        session_id: str,
        start_s: float | None = None,
        end_s: float | None = None,
    ) -> dict[str, Any]:
        """Read consistent session evidence, including every sample in the window.

        Sample ingestion is bounded by the application; snapshots never truncate
        records. The session count and events always describe the whole session.
        """
        start, end = self._bounds(start_s, end_s)
        with self._connection() as connection:
            connection.execute("BEGIN")
            session = self._session(self._require_session(connection, session_id))
            rows = connection.execute(
                "SELECT * FROM samples WHERE session_id=? ORDER BY seq", (session_id,)
            ).fetchall()
            events = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM events WHERE session_id=? ORDER BY at_s,id", (session_id,)
                )
            ]
            capture = (
                self._capture_summary(connection, session_id)
                if session["source"] == "mavlink-udp"
                else None
            )
        session["sample_count"] = len(rows)
        analysis = self._analyze_rows(session, rows, start, end, threshold=0.25)
        window = analysis["window_s"]
        selected = [
            dict(row) for row in rows if window["start_s"] <= row["elapsed_s"] <= window["end_s"]
        ]
        return {
            "session": session,
            "samples": selected,
            "events": events,
            "analysis": analysis,
            "capture": capture,
        }

    def append_datagram(
        self,
        session_id: str,
        *,
        elapsed_s: float,
        received_at: float,
        peer_host: str,
        peer_port: int,
        payload: bytes,
        disposition: str,
        details: dict[str, Any] | None = None,
        samples: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        """Atomically persist a UDP receipt and the measurements derived from it."""
        elapsed = _number(elapsed_s, "elapsed_s", minimum=0)
        received = _number(received_at, "received_at", minimum=0)
        if not isinstance(peer_host, str):
            raise ValueError("Datagram evidence requires an IPv4 loopback peer")
        try:
            address = IPv4Address(peer_host)
        except AddressValueError as exc:
            raise ValueError("Datagram evidence requires an IPv4 loopback peer") from exc
        if not address.is_loopback or str(address) != peer_host:
            raise ValueError("Datagram evidence requires a canonical IPv4 loopback peer")
        if (
            isinstance(peer_port, bool)
            or not isinstance(peer_port, int)
            or not 1 <= peer_port <= 65535
        ):
            raise ValueError("peer_port must be an integer between 1 and 65535")
        if not isinstance(payload, bytes) or len(payload) > 65535:
            raise ValueError("payload must contain at most 65535 bytes")
        if not isinstance(disposition, str) or disposition not in DATAGRAM_DISPOSITIONS:
            raise ValueError("Unsupported datagram disposition")
        encoded = _json_object(details, "details")
        values = [self._sample_values(sample) for sample in samples]
        if any(value[1] != elapsed or value[2] != received for value in values):
            raise ValueError("Derived samples must retain the datagram receipt timestamps")
        if values and disposition != "accepted":
            raise ValueError("Only accepted datagrams may produce samples")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = self._require_session(connection, session_id)
            if session["source"] != "mavlink-udp" or session["status"] != "live":
                raise ValueError("Datagrams require a live MAVLink UDP session")
            previous = connection.execute(
                "SELECT seq,elapsed_s FROM datagrams WHERE session_id=? ORDER BY seq DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            seq = previous["seq"] + 1 if previous else 0
            if previous is not None and elapsed < previous["elapsed_s"]:
                raise ValueError("Datagrams must be in nondecreasing receipt order")
            self._insert_samples(connection, session_id, values)
            start_seq = None
            if values:
                last_seq = connection.execute(
                    "SELECT MAX(seq) FROM samples WHERE session_id=?", (session_id,)
                ).fetchone()[0]
                start_seq = last_seq - len(values) + 1
            connection.execute(
                "INSERT INTO datagrams (session_id,seq,elapsed_s,received_at,peer_host,peer_port,"
                "payload,disposition,details,sample_start_seq,sample_count) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    seq,
                    elapsed,
                    received,
                    peer_host,
                    peer_port,
                    payload,
                    disposition,
                    encoded,
                    start_seq,
                    len(values),
                ),
            )
            connection.execute(
                "UPDATE sessions SET elapsed_s=MAX(elapsed_s,?) WHERE id=?", (elapsed, session_id)
            )
        return {"seq": seq, "sample_start_seq": start_seq, "sample_count": len(values)}

    def datagrams(self, session_id: str) -> list[dict[str, Any]]:
        """Return raw evidence as JSON-safe base64 without truncating the capture."""
        with self._connection() as connection:
            self._require_session(connection, session_id)
            rows = connection.execute(
                "SELECT * FROM datagrams WHERE session_id=? ORDER BY seq", (session_id,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["raw_base64"] = base64.b64encode(item.pop("payload")).decode("ascii")
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    @staticmethod
    def _capture_summary(connection: sqlite3.Connection, session_id: str) -> dict[str, Any]:
        rows = connection.execute(
            "SELECT disposition,COUNT(*) AS count,SUM(LENGTH(payload)) AS raw_bytes "
            "FROM datagrams WHERE session_id=? GROUP BY disposition",
            (session_id,),
        ).fetchall()
        return {
            "datagram_count": sum(row["count"] for row in rows),
            "raw_bytes": sum(row["raw_bytes"] for row in rows),
            "dispositions": {row["disposition"]: row["count"] for row in rows},
        }

    def capture_summary(self, session_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            self._require_session(connection, session_id)
            return self._capture_summary(connection, session_id)

    @staticmethod
    def _sample_values(sample: dict[str, Any]) -> tuple[float | None, ...]:
        if not isinstance(sample, dict) or any(field not in sample for field in SAMPLE_FIELDS):
            raise ValueError(f"Each sample requires {', '.join(SAMPLE_FIELDS)}")
        return tuple(
            None
            if index >= 3 and sample[field] is None
            else _number(sample[field], field, minimum=0 if index < 3 else None)
            for index, field in enumerate(SAMPLE_FIELDS)
        )

    @staticmethod
    def _insert_samples(
        connection: sqlite3.Connection, session_id: str, values: list[tuple]
    ) -> None:
        previous = connection.execute(
            "SELECT seq,elapsed_s FROM samples WHERE session_id=? ORDER BY seq DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        seq = previous["seq"] + 1 if previous else 0
        elapsed = previous["elapsed_s"] if previous else 0.0
        for value in values:
            if value[1] < elapsed:
                raise ValueError("elapsed_s must be in nondecreasing receipt order")
            elapsed = value[1]
        connection.executemany(
            "INSERT INTO samples (session_id,seq,source_time_s,elapsed_s,received_at,"
            "roll_deg,pitch_deg,gyro_x_deg_s) VALUES (?,?,?,?,?,?,?,?)",
            [(session_id, seq + index, *value) for index, value in enumerate(values)],
        )
        if values:
            connection.execute(
                "UPDATE sessions SET elapsed_s=MAX(elapsed_s,?) WHERE id=?",
                (elapsed, session_id),
            )

    def append_samples(
        self, session_id: str, samples: Iterable[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        values = [self._sample_values(sample) for sample in samples]
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = self._require_session(connection, session_id)
            if session["status"] != "live":
                raise ValueError("Cannot append to a finished session")
            self._insert_samples(connection, session_id, values)
            rows = connection.execute(
                "SELECT * FROM samples WHERE session_id=? ORDER BY seq DESC LIMIT ?",
                (session_id, len(values)),
            ).fetchall()
            return [dict(row) for row in reversed(rows)]

    def append_sample(
        self,
        session_id: str,
        source_time_s: float,
        elapsed_s: float,
        received_at: float,
        roll_deg: float | None,
        pitch_deg: float | None,
        gyro_x_deg_s: float | None,
    ) -> dict[str, Any]:
        sample = dict(
            zip(
                SAMPLE_FIELDS,
                (source_time_s, elapsed_s, received_at, roll_deg, pitch_deg, gyro_x_deg_s),
                strict=True,
            )
        )
        return self.append_samples(session_id, [sample])[0]

    def import_session(
        self,
        name: str,
        objective: str,
        samples: Iterable[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
        source: str = "argos-recording",
        duration_s: float | None = None,
    ) -> dict[str, Any]:
        """Validate and store an entire recording in one transaction."""
        values = self._session_values(name, objective, source, metadata, "completed")
        sample_values = [self._sample_values(sample) for sample in samples]
        if not sample_values:
            raise ValueError("A recording must contain at least one sample")
        duration = None if duration_s is None else _number(duration_s, "duration_s", minimum=0)
        if duration is not None and duration < max(sample[1] for sample in sample_values):
            raise ValueError("Recording duration cannot precede its samples")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._insert_session(connection, values)
            self._insert_samples(connection, values[0], sample_values)
            if duration is not None:
                connection.execute(
                    "UPDATE sessions SET elapsed_s=? WHERE id=?", (duration, values[0])
                )
        return self.get_session(values[0])

    @staticmethod
    def _bounds(start_s: float | None, end_s: float | None) -> tuple[float | None, float | None]:
        start = None if start_s is None else _number(start_s, "start_s", minimum=0)
        end = None if end_s is None else _number(end_s, "end_s", minimum=0)
        if start is not None and end is not None and end < start:
            raise ValueError("end_s must not precede start_s")
        return start, end

    def samples(
        self,
        session_id: str,
        start_s: float | None = None,
        end_s: float | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        start, end = self._bounds(start_s, end_s)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100000:
            raise ValueError("limit must be an integer between 1 and 100000")
        conditions, arguments = ["session_id=?"], [session_id]
        if start is not None:
            conditions.append("elapsed_s>=?")
            arguments.append(start)
        if end is not None:
            conditions.append("elapsed_s<=?")
            arguments.append(end)
        arguments.append(limit)
        with self._connection() as connection:
            self._require_session(connection, session_id)
            rows = connection.execute(
                "SELECT * FROM samples WHERE " + " AND ".join(conditions) + " ORDER BY seq LIMIT ?",
                arguments,
            )
            return [dict(row) for row in rows]

    def annotate(self, session_id: str, text: str, at_s: float) -> dict[str, Any]:
        return self.add_event(session_id, "annotation", text, at_s)

    def add_event(self, session_id: str, kind: str, text: str, at_s: float) -> dict[str, Any]:
        kind = _text(kind, "kind", 80)
        text = _text(text, "text", 4000)
        at_s = _number(at_s, "at_s", minimum=0)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = self._require_session(connection, session_id)
            if session["status"] != "live" and at_s > session["elapsed_s"]:
                raise ValueError("An event must fall within the finished session")
            cursor = connection.execute(
                "INSERT INTO events (session_id,kind,at_s,text,created_at) VALUES (?,?,?,?,?)",
                (session_id, kind, at_s, text, time.time()),
            )
            connection.execute(
                "UPDATE sessions SET elapsed_s=MAX(elapsed_s,?) WHERE id=?",
                (at_s, session_id),
            )
            return dict(
                connection.execute(
                    "SELECT * FROM events WHERE id=?", (cursor.lastrowid,)
                ).fetchone()
            )

    def events(self, session_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            self._require_session(connection, session_id)
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM events WHERE session_id=? ORDER BY at_s,id",
                    (session_id,),
                )
            ]

    def finish_session(
        self, session_id: str, status: str = "completed", elapsed_s: float | None = None
    ) -> dict[str, Any]:
        if status not in {"completed", "interrupted"}:
            raise ValueError("A finished session must be completed or interrupted")
        elapsed = None if elapsed_s is None else _number(elapsed_s, "elapsed_s", minimum=0)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = self._require_session(connection, session_id)
            if elapsed is not None and elapsed < session["elapsed_s"]:
                raise ValueError("Session duration cannot precede its samples or events")
            if session["status"] == "live":
                connection.execute(
                    "UPDATE sessions SET status=?,ended_at=?,elapsed_s=? WHERE id=?",
                    (
                        status,
                        time.time(),
                        session["elapsed_s"] if elapsed is None else elapsed,
                        session_id,
                    ),
                )
        return self.get_session(session_id)

    def recover_interrupted(self) -> int:
        """Mark sessions left live by a prior process without inventing their stop time."""
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE sessions SET status='interrupted' WHERE status='live'"
            )
            return cursor.rowcount

    def analyze(
        self,
        session_id: str,
        start_s: float | None = None,
        end_s: float | None = None,
        gap_threshold_s: float = 0.25,
    ) -> dict[str, Any]:
        start, end = self._bounds(start_s, end_s)
        threshold = _number(gap_threshold_s, "gap_threshold_s", minimum=0)
        if threshold == 0:
            raise ValueError("gap_threshold_s must be positive")
        with self._connection() as connection:
            # Keep session duration and records consistent during concurrent acquisition.
            connection.execute("BEGIN")
            session = self._require_session(connection, session_id)
            rows = connection.execute(
                "SELECT seq,elapsed_s,roll_deg FROM samples WHERE session_id=? ORDER BY seq",
                (session_id,),
            ).fetchall()
        return self._analyze_rows(session, rows, start, end, threshold)

    @staticmethod
    def _analyze_rows(
        session: sqlite3.Row | dict[str, Any],
        rows: list[sqlite3.Row],
        start: float | None,
        end: float | None,
        threshold: float,
    ) -> dict[str, Any]:
        start = 0.0 if start is None else start
        end = max(start, session["elapsed_s"]) if end is None else end
        selected = [row for row in rows if start <= row["elapsed_s"] <= end]
        intervals = [right["elapsed_s"] - left["elapsed_s"] for left, right in pairwise(selected)]
        gaps = []
        for left, right in pairwise(rows):
            interval = right["elapsed_s"] - left["elapsed_s"]
            overlap = min(end, right["elapsed_s"]) - max(start, left["elapsed_s"])
            if interval > threshold and overlap > 0:
                gaps.append(
                    {
                        "before_seq": left["seq"],
                        "after_seq": right["seq"],
                        "start_s": left["elapsed_s"],
                        "end_s": right["elapsed_s"],
                        "duration_s": interval,
                        "window_overlap_s": overlap,
                    }
                )
        rolls = [row["roll_deg"] for row in selected if row["roll_deg"] is not None]
        limitations = [
            "Receipt intervals use elapsed_s; source and receipt clocks "
            "are not assumed synchronized.",
            "An interval without samples does not identify a physical cause or prove packet loss.",
            "Leading and trailing silence cannot be bounded by two samples "
            "and is not counted as a gap.",
            "Gaps retain their full adjacent-sample interval "
            "even when the selected window clips it.",
        ]
        if session["source"] == "simulation":
            limitations.append(
                "Measurements are synthetic simulation data; no hardware was measured."
            )
        if not rolls:
            limitations.append("No roll measurements are available in this window.")
        if gaps:
            conclusion = (
                f"{len(gaps)} observed receipt interval(s) exceed {threshold:g} s; "
                "inspect the referenced sample pairs. The cause remains undetermined."
            )
        elif len(selected) < 2:
            conclusion = (
                "Fewer than two samples in this window; receipt continuity cannot be assessed."
            )
        else:
            conclusion = (
                f"No observed adjacent-sample interval exceeds {threshold:g} s in this window."
            )
        return {
            "session_id": session["id"],
            "sample_count": len(selected),
            "window_s": {"start_s": start, "end_s": end},
            "duration_s": end - start,
            "observed_duration_s": selected[-1]["elapsed_s"] - selected[0]["elapsed_s"]
            if selected
            else 0.0,
            "gap_threshold_s": threshold,
            "gaps": gaps,
            "max_gap_s": max((gap["duration_s"] for gap in gaps), default=0.0),
            "max_interval_s": max(intervals, default=None),
            "median_interval_s": statistics.median(intervals) if intervals else None,
            "roll_min_deg": min(rolls) if rolls else None,
            "roll_max_deg": max(rolls) if rolls else None,
            "conclusion": conclusion,
            "limitations": limitations,
        }
