"""An explicitly synthetic telemetry source, with no vehicle transport or command path."""

import asyncio
import math
import time
from contextlib import suppress

from .core import Store


class Simulator:
    def __init__(
        self,
        store: Store,
        *,
        period_s: float = 0.05,
        max_duration_s: float = 180,
        dropout_duration_s: float = 2,
    ) -> None:
        self.store = store
        self.period_s = period_s
        self.max_duration_s = max_duration_s
        self.dropout_duration_s = dropout_duration_s
        self.session_id: str | None = None
        self.task: asyncio.Task | None = None
        self.started = 0.0
        self.last_received: float | None = None
        self.pause_until = 0.0
        self.stop_status: str | None = None
        self.lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        return self.task is not None and not self.task.done()

    def elapsed(self) -> float:
        return min(max(0.0, time.monotonic() - self.started), self.max_duration_s)

    async def start(self, name: str, objective: str) -> dict:
        async with self.lock:
            if self.active:
                raise ValueError("Une session de simulation est déjà en cours.")
            session = self.store.create_session(
                name,
                objective,
                metadata={
                    "environment": "simulation",
                    "generator": "attitude-sine-v1",
                    "vehicle": "Synthetic attitude source / 01",
                    "sample_rate_hz": 1 / self.period_s,
                    "received_at_clock": "utc_epoch",
                    "source_time_clock": "simulator_elapsed",
                    "elapsed_clock": "host_monotonic",
                    "max_duration_s": self.max_duration_s,
                    "description": (
                        "Deterministic synthetic angles; not a flight dynamics simulator."
                    ),
                },
            )
            self.session_id = session["id"]
            self.started = time.monotonic()
            self.last_received = None
            self.pause_until = 0.0
            self.stop_status = None
            self.store.add_event(session["id"], "source_started", "Source synthétique démarrée.", 0)
            self.task = asyncio.create_task(self._run(session["id"]))
            return session

    async def _run(self, session_id: str) -> None:
        status = "interrupted"
        try:
            while self.elapsed() < self.max_duration_s:
                now = time.monotonic()
                t = self.elapsed()
                if now >= self.pause_until:
                    if self.pause_until:
                        self.store.add_event(
                            session_id,
                            "dropout_ended",
                            "Réception synthétique rétablie.",
                            t,
                        )
                        self.pause_until = 0.0
                    self.store.append_sample(
                        session_id,
                        source_time_s=t,
                        elapsed_s=t,
                        received_at=time.time(),
                        roll_deg=24 * math.sin(0.85 * t) + 1.2 * math.sin(4.1 * t),
                        pitch_deg=8 * math.sin(0.47 * t),
                        gyro_x_deg_s=20.4 * math.cos(0.85 * t) + 4.92 * math.cos(4.1 * t),
                    )
                    self.last_received = now
                # Align to the source cadence; storage time must not accumulate
                # into every period. Missed ticks remain missing observations.
                next_tick = (
                    self.started
                    + (math.floor((time.monotonic() - self.started) / self.period_s) + 1)
                    * self.period_s
                )
                await asyncio.sleep(max(0, next_tick - time.monotonic()))
            self.store.add_event(
                session_id,
                "duration_limit",
                "Limite de durée atteinte ; acquisition arrêtée.",
                self.max_duration_s,
            )
            status = "completed"
        except asyncio.CancelledError:
            raise
        except Exception:
            self.store.add_event(
                session_id,
                "source_error",
                "Acquisition interrompue par une erreur interne.",
                self.elapsed(),
            )
            raise
        finally:
            elapsed = max(self.elapsed(), self.store.get_session(session_id)["elapsed_s"])
            self.store.finish_session(
                session_id, status=self.stop_status or status, elapsed_s=elapsed
            )

    async def stop(self, session_id: str, *, status: str = "completed") -> dict:
        async with self.lock:
            session = self.store.get_session(session_id)
            if session_id != self.session_id or not self.active:
                if session["status"] == "live":
                    raise ValueError("Cette source n’est pas pilotée par ce processus.")
                return session
            elapsed = self.elapsed()
            self.stop_status = status
            self.store.add_event(
                session_id,
                "source_stopped",
                "Acquisition arrêtée ; données conservées.",
                elapsed,
            )
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.pause_until = 0.0
            # Cancellation can happen before _run enters its try/finally block.
            elapsed = max(self.elapsed(), self.store.get_session(session_id)["elapsed_s"])
            return self.store.finish_session(session_id, status=status, elapsed_s=elapsed)

    async def dropout(self, session_id: str) -> dict:
        async with self.lock:
            session = self.store.get_session(session_id)
            if (
                session["source"] != "simulation"
                or session_id != self.session_id
                or not self.active
            ):
                raise ValueError("La coupure exige une simulation active dans cette session.")
            now = time.monotonic()
            if self.pause_until > now:
                raise ValueError("Une coupure est déjà en cours.")
            if self.max_duration_s - self.elapsed() < self.dropout_duration_s + self.period_s * 2:
                raise ValueError("Durée restante insuffisante pour observer la reprise.")
            self.pause_until = now + self.dropout_duration_s
            self.store.add_event(
                session_id,
                "dropout_started",
                f"Réception synthétique suspendue pendant {self.dropout_duration_s:g} s.",
                self.elapsed(),
            )
            return {"status": "scheduled", "duration_s": self.dropout_duration_s}

    def live_state(self, session_id: str) -> dict:
        running = session_id == self.session_id and self.active
        age = (
            None
            if not running or self.last_received is None
            else (time.monotonic() - self.last_received)
        )
        return {
            "connected": running,
            "age_s": age,
            "freshness": (
                "offline"
                if not running
                else "empty"
                if age is None
                else "fresh"
                if age < 0.3
                else "stale"
            ),
            "last_sample_elapsed_s": (
                self.last_received - self.started if running and self.last_received else None
            ),
            "elapsed_s": self.elapsed() if running else None,
            "gap_active": running and time.monotonic() < self.pause_until,
        }
