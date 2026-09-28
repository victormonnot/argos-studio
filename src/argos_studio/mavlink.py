"""Receive-only, loopback UDP telemetry. This module has no vehicle send path."""

import asyncio
import math
import socket
import time
from contextlib import suppress
from importlib.metadata import version
from ipaddress import IPv4Address

from pymavlink.dialects.v20 import ardupilotmega as dialect

from .core import Store

MAX_RAW_BYTES = 10 * 1024 * 1024
MAX_DATAGRAMS = 20_000
MAX_SAMPLES = 100_000
MAX_FRAMES_PER_DATAGRAM = 128
HEARTBEAT_STALE_S = 2.5
ATTITUDE_STALE_S = 0.3


class CaptureLimit(Exception):
    """A complete datagram would exceed the bounded capture budget."""


def decode_datagram(payload: bytes) -> tuple[str, list, str | None]:
    """Require complete, CRC-valid unsigned MAVLink 1/2 frames per datagram.

    Parser state is never shared across datagrams or senders. CRC validation
    establishes integrity, not sender authentication. Signed traffic requires
    an explicit signing configuration and is unsupported in this profile.
    """
    if dialect.MAVLINK_IGNORE_CRC:
        raise ValueError("MAV_IGNORE_CRC doit être désactivé pour recevoir de la télémétrie.")
    if not payload:
        return "invalid", [], "empty_datagram"
    parser = dialect.MAVLink(None)
    parser.robust_parsing = True
    try:
        messages = parser.parse_buffer(payload) or []
    except (dialect.MAVError, ValueError, IndexError):
        return "invalid", [], "decoder_error"
    if (
        not messages
        or parser.buf_len()
        or len(messages) > MAX_FRAMES_PER_DATAGRAM
        or any(
            message.get_type() == "BAD_DATA" or message.get_type().startswith("UNKNOWN")
            for message in messages
        )
        or sum(len(message.get_msgbuf()) for message in messages) != len(payload)
    ):
        return "invalid", [], "invalid_or_incomplete_frame"
    # get_signed() reports signature verification, not the presence of its wire flag.
    if any(message.get_msgbuf()[0] == 0xFD and message.get_msgbuf()[2] & 1 for message in messages):
        return "signed", [], "signing_not_configured"
    for message in messages:
        if message.get_type() == "ATTITUDE":
            if not all(
                math.isfinite(getattr(message, key))
                for key in ("roll", "pitch", "yaw", "rollspeed", "pitchspeed", "yawspeed")
            ):
                return "invalid", [], "nonfinite_attitude"
    return "accepted", messages, None


class MavlinkReceiver:
    """One explicitly selected source, pinned to its first validated UDP peer."""

    def __init__(
        self,
        store: Store,
        *,
        max_duration_s: float = 180,
        max_raw_bytes: int = MAX_RAW_BYTES,
        max_datagrams: int = MAX_DATAGRAMS,
        max_samples: int = MAX_SAMPLES,
    ):
        self.store = store
        self.max_duration_s = max_duration_s
        self.max_raw_bytes = max_raw_bytes
        self.max_datagrams = max_datagrams
        self.max_samples = max_samples
        self.session_id: str | None = None
        self.task: asyncio.Task | None = None
        self.socket: socket.socket | None = None
        self.lock = asyncio.Lock()
        self.started = 0.0
        self.system_id = 1
        self.component_id = 1
        self.peer: tuple[str, int] | None = None
        self.last_received: float | None = None
        self.last_frame: float | None = None
        self.last_heartbeat: float | None = None
        self.heartbeat: dict | None = None
        self.stop_status: str | None = None
        self.stale_reported = False
        self.raw_bytes = 0
        self.datagram_count = 0
        self.sample_count = 0

    @property
    def active(self) -> bool:
        return self.task is not None and not self.task.done()

    def elapsed(self) -> float:
        return min(max(0, time.monotonic() - self.started), self.max_duration_s)

    async def start(
        self,
        name: str,
        objective: str,
        *,
        listen_port: int = 14580,
        system_id: int = 1,
        component_id: int = 1,
    ) -> dict:
        async with self.lock:
            if self.active:
                raise ValueError("Une réception MAVLink est déjà active.")
            if type(listen_port) is not int or not 1024 <= listen_port <= 65535:
                raise ValueError("Le port UDP doit être compris entre 1024 et 65535.")
            if any(
                type(value) is not int or not 1 <= value <= 255
                for value in (system_id, component_id)
            ):
                raise ValueError("Les identifiants MAVLink doivent être compris entre 1 et 255.")
            if dialect.MAVLINK_IGNORE_CRC:
                raise ValueError(
                    "MAV_IGNORE_CRC doit être désactivé pour recevoir de la télémétrie."
                )
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                # No SO_REUSEADDR: an occupied port must fail rather than split receipts.
                sock.bind(("127.0.0.1", listen_port))
                sock.setblocking(False)
                session = self.store.create_session(
                    name,
                    objective,
                    source="mavlink-udp",
                    metadata={
                        "transport": "udp",
                        "listen_host": "127.0.0.1",
                        "listen_port": listen_port,
                        "vehicle": {"system_id": system_id, "component_id": component_id},
                        "environment": "simulation",
                        "provenance": (
                            "Local source declared as simulation; not independently authenticated."
                        ),
                        "decoder": "pymavlink",
                        "decoder_version": version("pymavlink"),
                        "dialect": "ardupilotmega",
                        "wire_profile": "unsigned MAVLink 1/2",
                        "received_at_clock": "utc_epoch",
                        "source_time_clock": "vehicle_boot_uint32_ms",
                        "elapsed_clock": "host_monotonic",
                        "read_only": True,
                        "max_duration_s": self.max_duration_s,
                        "max_raw_bytes": self.max_raw_bytes,
                        "max_datagrams": self.max_datagrams,
                        "max_samples": self.max_samples,
                        "attitude_stale_s": ATTITUDE_STALE_S,
                        "heartbeat_stale_s": HEARTBEAT_STALE_S,
                    },
                )
            except OSError as exc:
                sock.close()
                raise ValueError(
                    "Impossible d’ouvrir le port UDP local ; vérifiez qu’il est libre."
                ) from exc
            except Exception:
                sock.close()
                raise
            self.socket = sock
            self.session_id = session["id"]
            self.system_id, self.component_id = system_id, component_id
            self.started = time.monotonic()
            self.peer = None
            self.last_received = self.last_frame = self.last_heartbeat = None
            self.heartbeat = None
            self.stop_status = None
            self.stale_reported = False
            self.raw_bytes = self.datagram_count = 0
            self.sample_count = 0
            self.store.add_event(
                session["id"],
                "source_started",
                f"Écoute UDP passive sur 127.0.0.1:{listen_port} ; "
                f"cible {system_id}/{component_id}.",
                0,
            )
            self.task = asyncio.create_task(self._run(session["id"]))
            return session

    def _staleness(self, session_id: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if (
            self.last_received is not None
            and not self.stale_reported
            and now - self.last_received >= ATTITUDE_STALE_S
        ):
            self.store.add_event(
                session_id,
                "reception_stale",
                "Les mesures ATTITUDE sont périmées ; écoute toujours active.",
                min(now - self.started, self.max_duration_s),
            )
            self.stale_reported = True

    def _record(self, session_id: str, payload: bytes, peer: tuple[str, int], now: float) -> None:
        # Even a socket bound to loopback can receive a locally routed packet
        # explicitly sent from another interface address. Such peers are outside
        # this capture profile and must not terminate or claim the session.
        if not IPv4Address(peer[0]).is_loopback:
            return
        elapsed = min(now - self.started, self.max_duration_s)
        received_at = time.time()
        # A frame can arrive just after the stale deadline but before the next
        # timeout check. Record that interval before marking receipt as fresh.
        self._staleness(session_id, now)
        disposition, messages, reason = decode_datagram(payload)
        if self.peer is not None and peer != self.peer:
            disposition, messages, reason = "foreign_peer", [], "peer_changed"
        selected = [
            message
            for message in messages
            if message.get_srcSystem() == self.system_id
            and message.get_srcComponent() == self.component_id
        ]
        if disposition == "accepted" and not selected:
            disposition, reason = "foreign_source", "target_not_present"
        frames = [
            {
                "type": message.get_type(),
                "system_id": message.get_srcSystem(),
                "component_id": message.get_srcComponent(),
                "wire_seq": message.get_seq(),
                "message_id": message.get_msgId(),
                "wire_version": 2 if message.get_msgbuf()[0] == 0xFD else 1,
            }
            for message in messages
        ]
        samples = []
        heartbeat = None
        for message in selected:
            if message.get_type() == "ATTITUDE":
                samples.append(
                    {
                        "source_time_s": message.time_boot_ms / 1000,
                        "elapsed_s": elapsed,
                        "received_at": received_at,
                        "roll_deg": math.degrees(message.roll),
                        "pitch_deg": math.degrees(message.pitch),
                        "gyro_x_deg_s": math.degrees(message.rollspeed),
                    }
                )
            elif message.get_type() == "HEARTBEAT":
                heartbeat = {
                    key: getattr(message, key)
                    for key in (
                        "type",
                        "autopilot",
                        "base_mode",
                        "custom_mode",
                        "system_status",
                        "mavlink_version",
                    )
                }
        if self.sample_count + len(samples) > self.max_samples:
            raise CaptureLimit
        self.store.append_datagram(
            session_id,
            elapsed_s=elapsed,
            received_at=received_at,
            peer_host=peer[0],
            peer_port=peer[1],
            payload=payload,
            disposition=disposition,
            details={"reason": reason, "frames": frames, "heartbeat": heartbeat},
            samples=samples,
        )
        self.raw_bytes += len(payload)
        self.datagram_count += 1
        self.sample_count += len(samples)
        if disposition == "accepted":
            if self.peer is None:
                self.peer = peer
                self.store.add_event(
                    session_id,
                    "peer_identified",
                    f"Source {self.system_id}/{self.component_id} reçue depuis "
                    f"{peer[0]}:{peer[1]} ; origine non authentifiée.",
                    elapsed,
                )
            self.last_frame = now
            if heartbeat is not None:
                self.heartbeat, self.last_heartbeat = heartbeat, now
            if samples:
                self.last_received = now
                if self.stale_reported:
                    self.store.add_event(
                        session_id,
                        "reception_resumed",
                        "Réception ATTITUDE rétablie ; intervalle consultable dans les mesures.",
                        elapsed,
                    )
                    self.stale_reported = False

    async def _run(self, session_id: str) -> None:
        status = "interrupted"
        try:
            loop = asyncio.get_running_loop()
            while self.elapsed() < self.max_duration_s:
                self._staleness(session_id)
                try:
                    payload, peer = await asyncio.wait_for(
                        loop.sock_recvfrom(self.socket, 65535), timeout=0.1
                    )
                except TimeoutError:
                    continue
                now = time.monotonic()
                if now - self.started >= self.max_duration_s:
                    break
                if (
                    self.raw_bytes + len(payload) > self.max_raw_bytes
                    or self.datagram_count >= self.max_datagrams
                ):
                    self.store.add_event(
                        session_id,
                        "capture_limit",
                        "Limite de capture atteinte ; réception arrêtée avant dépassement.",
                        self.elapsed(),
                    )
                    status = "completed"
                    return
                self._record(session_id, payload, peer, now)
            self.store.add_event(
                session_id,
                "duration_limit",
                "Limite de durée atteinte ; écoute UDP arrêtée.",
                self.elapsed(),
            )
            status = "completed"
        except asyncio.CancelledError:
            raise
        except CaptureLimit:
            self.store.add_event(
                session_id,
                "capture_limit",
                "Limite de mesures atteinte ; réception arrêtée.",
                self.elapsed(),
            )
            status = "completed"
        except Exception:
            self.store.add_event(
                session_id,
                "source_error",
                "Réception interrompue par une erreur interne ; données déjà reçues conservées.",
                self.elapsed(),
            )
            # Preserve the failure as a terminal session instead of leaving a live orphan.
        finally:
            self.socket.close()
            elapsed = max(self.elapsed(), self.store.get_session(session_id)["elapsed_s"])
            self.store.finish_session(
                session_id, status=self.stop_status or status, elapsed_s=elapsed
            )

    async def stop(self, session_id: str, *, status: str = "completed") -> dict:
        async with self.lock:
            session = self.store.get_session(session_id)
            if session_id != self.session_id or not self.active:
                if session["status"] == "live":
                    raise ValueError("Cette réception n’est pas pilotée par ce processus.")
                return session
            self.stop_status = status
            self.store.add_event(
                session_id,
                "source_stopped",
                "Écoute UDP arrêtée ; aucun ordre envoyé à la source.",
                self.elapsed(),
            )
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.socket.close()
            elapsed = max(self.elapsed(), self.store.get_session(session_id)["elapsed_s"])
            return self.store.finish_session(session_id, status=status, elapsed_s=elapsed)

    def live_state(self, session_id: str) -> dict:
        owned = session_id == self.session_id
        running = owned and self.active
        now = time.monotonic()
        age = now - self.last_received if running and self.last_received is not None else None
        frame_age = now - self.last_frame if running and self.last_frame is not None else None
        heartbeat_age = (
            now - self.last_heartbeat if running and self.last_heartbeat is not None else None
        )
        return {
            "listening": running,
            "connected": running and frame_age is not None and frame_age < HEARTBEAT_STALE_S,
            "connection_status": (
                "offline"
                if not running
                else "waiting"
                if frame_age is None
                else "receiving"
                if frame_age < HEARTBEAT_STALE_S
                else "stale"
            ),
            "age_s": age,
            "freshness": (
                "offline"
                if not running
                else "empty"
                if age is None
                else "fresh"
                if age < ATTITUDE_STALE_S
                else "stale"
            ),
            "last_sample_elapsed_s": (
                self.last_received - self.started
                if owned and self.last_received is not None
                else None
            ),
            "elapsed_s": self.elapsed() if running else None,
            "gap_active": False,
            "heartbeat_age_s": heartbeat_age,
            "heartbeat_freshness": (
                "offline"
                if not running
                else "empty"
                if heartbeat_age is None
                else "fresh"
                if heartbeat_age < HEARTBEAT_STALE_S
                else "stale"
            ),
            "heartbeat": self.heartbeat if owned else None,
            "peer": {"host": self.peer[0], "port": self.peer[1]} if owned and self.peer else None,
            "system_id": self.system_id if owned else None,
            "component_id": self.component_id if owned else None,
        }
