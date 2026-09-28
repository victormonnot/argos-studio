import asyncio
import base64
import math
import socket
import time

import pytest
from fastapi.testclient import TestClient
from pymavlink.dialects.v20 import ardupilotmega as mav

from argos_studio.app import Settings, create_app
from argos_studio.core import Store
from argos_studio.mavlink import MavlinkReceiver, decode_datagram


def frame(
    kind="attitude", *, system=1, component=1, v1=False, signed=False, boot_ms=1200, roll=0.2
):
    encoder = mav.MAVLink(None, srcSystem=system, srcComponent=component)
    encoder.seq = 23
    if signed:
        encoder.signing.secret_key = bytes(range(32))
        encoder.signing.sign_outgoing = True
        encoder.signing.link_id = 7
        encoder.signing.timestamp = 42
    message = (
        mav.MAVLink_heartbeat_message(2, 3, 0, 0, 3, 3)
        if kind == "heartbeat"
        else mav.MAVLink_attitude_message(boot_ms, roll, -0.1, 0, 0.3, 0, 0)
    )
    return message.pack(encoder, force_mavlink1=v1)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


async def until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        await asyncio.sleep(0.01)
    pytest.fail("Timed out waiting for UDP receipt state")


@pytest.mark.parametrize("v1", [False, True])
def test_decodes_complete_mavlink_versions_and_multiple_frames(v1):
    payload = frame("heartbeat", v1=v1) + frame(v1=v1)
    disposition, messages, reason = decode_datagram(payload)
    assert disposition == "accepted"
    assert reason is None
    assert [message.get_type() for message in messages] == ["HEARTBEAT", "ATTITUDE"]
    assert messages[1].get_srcSystem() == 1
    assert messages[1].get_srcComponent() == 1
    assert math.isclose(messages[1].roll, 0.2, rel_tol=1e-6)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"0 ",
        frame()[:-1],
        frame() + frame()[:8],
        frame()[:-2] + b"\x00\x00",
        frame(roll=math.nan),
    ],
)
def test_invalid_datagram_cannot_yield_partial_measurements(payload):
    disposition, messages, reason = decode_datagram(payload)
    assert disposition == "invalid"
    assert not messages
    assert reason


def test_unknown_message_is_not_crc_validated_or_accepted():
    payload = bytearray(frame())
    payload[7:10] = b"\xff\xff\xff"
    assert decode_datagram(payload)[0] == "invalid"


def test_signed_wire_frame_requires_explicit_signing_support():
    payload = frame(signed=True)
    assert payload[2] & 1
    assert decode_datagram(payload)[0] == "signed"


def test_crc_bypass_environment_is_refused(monkeypatch):
    monkeypatch.setattr(mav, "MAVLINK_IGNORE_CRC", "0")
    with pytest.raises(ValueError, match="MAV_IGNORE_CRC"):
        decode_datagram(frame())


def test_excessive_frame_count_is_rejected_as_one_datagram():
    assert decode_datagram(frame("heartbeat") * 129)[0] == "invalid"


def test_nonloopback_peer_cannot_claim_or_stop_receiver(tmp_path):
    async def exercise():
        store = Store(tmp_path / "peer.sqlite3")
        receiver = MavlinkReceiver(store)
        session = await receiver.start("Loopback only", "", listen_port=free_port())
        receiver._record(session["id"], frame(), ("192.0.2.1", 14580), time.monotonic())
        assert receiver.active
        assert receiver.peer is None
        assert store.capture_summary(session["id"])["datagram_count"] == 0
        await receiver.stop(session["id"])

    asyncio.run(exercise())


def test_live_receipts_raw_evidence_peer_pinning_gap_replay_and_no_reply(tmp_path):
    async def exercise():
        store = Store(tmp_path / "live.sqlite3")
        receiver = MavlinkReceiver(store)
        port = free_port()
        session = await receiver.start("Passive source", "Observe continuity", listen_port=port)
        session_id = session["id"]
        assert receiver.live_state(session_id)["connection_status"] == "waiting"
        assert receiver.live_state(session_id)["heartbeat_freshness"] == "empty"
        assert not receiver.live_state(session_id)["connected"]
        with (
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender,
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as stranger,
        ):
            sender.bind(("127.0.0.1", 0))
            stranger.bind(("127.0.0.2", 0))
            address = ("127.0.0.1", port)
            sender.sendto(b"0 ", address)
            sender.sendto(frame(system=2), address)
            sender.sendto(frame("heartbeat") + frame(), address)
            await until(lambda: store.get_session(session_id)["sample_count"] == 1)
            live = receiver.live_state(session_id)
            assert live["freshness"] == "fresh"
            assert live["heartbeat"]["autopilot"] == 3
            assert live["heartbeat_freshness"] == "fresh"
            assert live["peer"]["port"] == sender.getsockname()[1]
            stranger.sendto(frame(roll=2), address)
            sender.sendto(frame(component=2), address)
            sender.sendto(frame(signed=True), address)
            await until(lambda: store.capture_summary(session_id)["datagram_count"] == 6)
            await until(lambda: receiver.live_state(session_id)["freshness"] == "stale")
            # Source boot time can move backward without changing host receipt chronology.
            sender.sendto(frame(boot_ms=5), address)
            await until(lambda: store.get_session(session_id)["sample_count"] == 2)
            sender.settimeout(0.05)
            with pytest.raises(TimeoutError):
                sender.recvfrom(4096)
            result = await receiver.stop(session_id)
            assert result["status"] == "completed"
            assert not receiver.live_state(session_id)["listening"]
        saved = Store(tmp_path / "live.sqlite3").snapshot(session_id)
        assert len(saved["samples"]) == 2
        assert saved["samples"][0]["source_time_s"] == 1.2
        assert saved["samples"][1]["source_time_s"] == 0.005
        assert saved["analysis"]["gaps"][0]["duration_s"] >= 0.3
        dispositions = saved["capture"]["dispositions"]
        assert dispositions == {
            "accepted": 2,
            "foreign_peer": 1,
            "foreign_source": 2,
            "invalid": 1,
            "signed": 1,
        }
        raw = store.datagrams(session_id)
        assert base64.b64decode(raw[2]["raw_base64"]) == frame("heartbeat") + frame()
        assert raw[2]["sample_start_seq"] == 0
        assert raw[2]["sample_count"] == 1
        assert {event["kind"] for event in saved["events"]} >= {
            "peer_identified",
            "reception_stale",
            "reception_resumed",
            "source_stopped",
        }
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as rebound:
            rebound.bind(("127.0.0.1", port))

    asyncio.run(exercise())


def test_occupied_port_fails_without_creating_session(tmp_path):
    async def exercise():
        store = Store(tmp_path / "busy.sqlite3")
        receiver = MavlinkReceiver(store)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            with pytest.raises(ValueError, match="port UDP"):
                await receiver.start("Busy", "", listen_port=occupied.getsockname()[1])
        assert store.list_sessions() == []

    asyncio.run(exercise())


def test_immediate_stop_releases_port_and_next_start(tmp_path):
    async def exercise():
        receiver = MavlinkReceiver(Store(tmp_path / "stop.sqlite3"))
        port = free_port()
        session = await receiver.start("Immediate", "", listen_port=port)
        assert (await receiver.stop(session["id"]))["status"] == "completed"
        session = await receiver.start("Again", "", listen_port=port)
        await receiver.stop(session["id"])

    asyncio.run(exercise())


@pytest.mark.parametrize("limit", ["bytes", "datagrams", "duration", "samples"])
def test_capture_limits_stop_and_release_socket(tmp_path, limit):
    async def exercise():
        store = Store(tmp_path / "bounded.sqlite3")
        receiver = MavlinkReceiver(
            store,
            max_raw_bytes=1 if limit == "bytes" else 1000,
            max_datagrams=1 if limit == "datagrams" else 10,
            max_samples=1 if limit == "samples" else 10,
            max_duration_s=0.15 if limit == "duration" else 2,
        )
        port = free_port()
        session = await receiver.start("Bounded", "", listen_port=port)
        if limit != "duration":
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                sender.sendto(frame(), ("127.0.0.1", port))
                sender.sendto(frame(), ("127.0.0.1", port))
        await until(lambda: not receiver.active)
        assert store.get_session(session["id"])["status"] == "completed"
        assert store.capture_summary(session["id"])["raw_bytes"] <= receiver.max_raw_bytes
        kinds = {event["kind"] for event in store.events(session["id"])}
        assert ("duration_limit" if limit == "duration" else "capture_limit") in kinds
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as rebound:
            rebound.bind(("127.0.0.1", port))

    asyncio.run(exercise())


def test_runtime_failure_is_terminal_and_releases_socket(tmp_path, monkeypatch):
    async def exercise():
        store = Store(tmp_path / "error.sqlite3")
        receiver = MavlinkReceiver(store)
        port = free_port()
        session = await receiver.start("Failure", "", listen_port=port)

        def fail(*args, **kwargs):
            raise RuntimeError("storage failure")

        monkeypatch.setattr(store, "append_datagram", fail)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(frame(), ("127.0.0.1", port))
        await until(lambda: not receiver.active)
        assert store.get_session(session["id"])["status"] == "interrupted"
        assert any(event["kind"] == "source_error" for event in store.events(session["id"]))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as rebound:
            rebound.bind(("127.0.0.1", port))

    asyncio.run(exercise())


def test_http_acquisition_boundary_capture_export_and_restart(tmp_path):
    settings = Settings(data_dir=tmp_path, argos_root=None, argos_python=None)
    with TestClient(create_app(settings)) as client:
        port = free_port()
        response = client.post(
            "/api/sessions",
            json={"name": "MAVLink HTTP", "source": "mavlink-udp", "listen_port": port},
        )
        assert response.status_code == 201, response.text
        session_id = response.json()["id"]
        prefix = f"/api/sessions/{session_id}"
        assert client.post("/api/sessions", json={"name": "Synthetic conflict"}).status_code == 400
        assert client.post(prefix + "/dropout", json={}).status_code == 400
        assert client.get(prefix + "/capture").status_code == 409
        payload = frame("heartbeat") + frame()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(payload, ("127.0.0.1", port))
            deadline = time.monotonic() + 2
            while client.get(prefix).json()["session"]["sample_count"] < 1:
                assert time.monotonic() < deadline
                time.sleep(0.02)
        assert client.post(prefix + "/stop", json={}).status_code == 200
        capture = client.get(prefix + "/capture").json()
        assert base64.b64decode(capture["datagrams"][0]["raw_base64"]) == payload
        assert capture["session"]["metadata"]["read_only"] is True
        assert client.get(prefix + "/export").json()["capture"]["datagram_count"] == 1
    with TestClient(create_app(settings)) as reopened:
        snapshot = reopened.get(prefix).json()
        assert snapshot["live"]["freshness"] == "offline"
        assert snapshot["session"]["sample_count"] == 1
        assert reopened.get(prefix + "/capture").json() == capture


@pytest.mark.parametrize(
    "options",
    [
        {"listen_port": 0},
        {"listen_port": 65536},
        {"listen_port": True},
        {"system_id": 0},
        {"component_id": 256},
        {"listen_host": "0.0.0.0"},
    ],
)
def test_http_rejects_invalid_or_nonlocal_configuration(tmp_path, options):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        response = client.post(
            "/api/sessions", json={"name": "Rejected", "source": "mavlink-udp", **options}
        )
        assert response.status_code == 422
        assert client.get("/api/sessions").json() == []
