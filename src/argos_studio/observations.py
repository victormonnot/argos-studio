"""Quiet local discovery of evidence worth inspecting, independent of a model."""

import asyncio
import math
import threading
import time

from .core import (
    MAX_OBSERVATIONS,
    OBSERVATION_GAP_THRESHOLD_S,
    OBSERVATION_RULE_VERSION,
    Store,
)
from .investigation import build_report


def investigate_observation(store: Store, session_id: str, observation_id: str) -> dict:
    """Create and link one deterministic report, or reopen the existing report."""
    observation = store.get_observation(session_id, observation_id)
    if observation["investigation_id"] is None:
        evidence = store.investigation_input(session_id)
        report = build_report(
            evidence,
            start_s=observation["start_s"],
            end_s=observation["end_s"],
            context=(
                f"Observation locale {observation_id} : intervalle de réception entre "
                f"les mesures {observation['before_seq']} et {observation['after_seq']}. "
                "La cause reste à déterminer."
            ),
        )
        store.save_investigation(session_id, report, observation_id=observation_id)
    return store.get_observation(session_id, observation_id)


class ObservationMonitor:
    """Scan bounded batches off the acquisition loop; resume durable cursors on restart."""

    def __init__(self, store: Store, *, interval_s: float = 1):
        if (
            isinstance(interval_s, bool)
            or not isinstance(interval_s, (int, float))
            or not math.isfinite(interval_s)
            or not 0 < interval_s <= 60
        ):
            raise ValueError("Observation polling interval must be between 0 and 60 seconds")
        self.store = store
        self.interval_s = interval_s
        self.last_scan_at = None
        self.last_error = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._thread_stop = threading.Event()
        self._scanned_counts: dict[str, int] = {}

    def health(self) -> dict:
        return {
            "available": bool(self._task and not self._task.done() and not self.last_error),
            "rule_version": OBSERVATION_RULE_VERSION,
            "threshold_s": OBSERVATION_GAP_THRESHOLD_S,
            "max_per_session": MAX_OBSERVATIONS,
            "interval_s": self.interval_s,
            "last_scan_at": self.last_scan_at,
            "last_error": self.last_error,
        }

    def start(self) -> None:
        if self._task is None and not self._stop.is_set():
            self._task = asyncio.create_task(self._run())

    def _scan(self):
        for session in self.store.list_sessions():
            if self._thread_stop.is_set():
                break
            # Measurements are append-only. Completed scans of unchanged sessions
            # need no write transaction on every poll; unfinished batches resume.
            if self._scanned_counts.get(session["id"]) == session["sample_count"]:
                continue
            result = self.store.scan_observations(session["id"])
            if result["scan"]["complete"]:
                self._scanned_counts[session["id"]] = session["sample_count"]

    async def scan_once(self) -> bool:
        try:
            await asyncio.to_thread(self._scan)
        except Exception:
            self.last_error = (
                "Le repérage local est temporairement indisponible. "
                "Les observations conservées restent consultables."
            )
            return False
        self.last_scan_at = time.time()
        self.last_error = None
        return True

    async def _run(self):
        while not self._stop.is_set():
            await self.scan_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except TimeoutError:
                pass

    async def close(self) -> None:
        self._thread_stop.set()
        self._stop.set()
        if self._task is not None:
            # Do not cancel a SQLite worker while its transaction is in flight.
            # Acquisition cleanup can proceed once this bounded scan has drained.
            await asyncio.shield(self._task)
