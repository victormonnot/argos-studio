"""Persistent session data and deterministic, clock-aware telemetry analysis."""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from itertools import pairwise
from pathlib import Path
from typing import Any
from uuid import uuid4

SOURCES = {"simulation", "argos-recording"}
STATUSES = {"live", "completed", "interrupted"}
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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    source TEXT NOT NULL CHECK(source IN ('simulation','argos-recording')),
                    status TEXT NOT NULL CHECK(status IN ('live','completed','interrupted')),
                    metadata TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    ended_at REAL,
                    elapsed_s REAL NOT NULL DEFAULT 0 CHECK(elapsed_s >= 0)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_live_simulation
                    ON sessions(source) WHERE source='simulation' AND status='live';
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
                );
                CREATE INDEX IF NOT EXISTS sample_window ON samples(session_id, elapsed_s);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    kind TEXT NOT NULL,
                    at_s REAL NOT NULL,
                    text TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )

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
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")
        try:
            encoded = json.dumps(metadata or {}, allow_nan=False, ensure_ascii=False)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError("metadata must contain finite JSON data") from exc
        if len(encoded.encode("utf-8")) > 65536:
            raise ValueError("metadata exceeds 64 KiB")
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
            raise ValueError("A simulation session is already live") from exc

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
        session["sample_count"] = len(rows)
        analysis = self._analyze_rows(session, rows, start, end, threshold=0.25)
        window = analysis["window_s"]
        selected = [
            dict(row) for row in rows if window["start_s"] <= row["elapsed_s"] <= window["end_s"]
        ]
        return {"session": session, "samples": selected, "events": events, "analysis": analysis}

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
