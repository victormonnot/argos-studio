"""Run a bounded passive-reception smoke check in an isolated loopback namespace.

The simulator binary must already be installed. This tool starts one disarmed
SITL instance, observes its telemetry, stops that owned process, and verifies
that retained measurements become stale before exporting the finished session.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import fcntl
import hashlib
import json
import os
import platform
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

SITL_SHA256 = "7862662092edc2861fc03da3d6fb2f0136d1670e563ca324eb52c1a324d1e14b"
SITL_VERSION = "ArduCopter 4.6.3"
SITL_COMMIT = "3fc7011a7d3dc047cbb17d8bd98ee94577d144c6"
DEFAULT_PARAMETERS = """FRAME_CLASS 1
FRAME_TYPE 0
SERIAL0_PROTOCOL -1
SERIAL1_PROTOCOL 2
SERIAL2_PROTOCOL -1
SR0_EXTRA1 20
SR0_EXT_STAT 0
SR0_EXTRA2 0
SR0_EXTRA3 0
SR0_POSITION 0
SR0_RAW_CTRL 0
SR0_RAW_SENS 0
SR0_RC_CHAN 0
SR0_PARAMS 0
LOG_BACKEND_TYPE 0
"""


def require_loopback_namespace() -> None:
    """The pinned simulator opens an auxiliary wildcard RC UDP socket."""
    if sys.platform != "linux" or {name for _, name in socket.if_nameindex()} != {"lo"}:
        raise ValueError("Run this check inside a network namespace with only loopback.")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        request = struct.pack("16sH14s", b"lo", 0, b"")
        flags = struct.unpack("16sH14s", fcntl.ioctl(probe.fileno(), 0x8913, request))[1]
    if flags & 0x9 != 0x9:
        raise ValueError("Enable the namespace loopback interface before running this check.")


def check_binary(path: Path) -> Path:
    if platform.machine().lower() not in {"x86_64", "amd64"}:
        raise ValueError("The pinned simulator requires Linux x86_64.")
    binary = path.resolve(strict=True)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("Provide an executable regular SITL binary.")
    with binary.open("rb") as stream:
        content = stream.read(16 * 1024 * 1024 + 1)
    if hashlib.sha256(content).hexdigest() != SITL_SHA256:
        raise ValueError("The simulator does not match the pinned ArduCopter 4.6.3 SHA-256.")
    return binary


async def stop_owned_process(process: subprocess.Popen) -> dict:
    escalated = False
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.to_thread(process.wait, timeout=2)
        except subprocess.TimeoutExpired:
            escalated = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await asyncio.to_thread(process.wait, timeout=1)
    return {"returncode": process.returncode, "escalated_to_kill": escalated}


async def run_smoke(binary: Path, output: Path) -> dict:
    # Validate isolation and executable before creating files or launching a process.
    require_loopback_namespace()
    binary = check_binary(binary)
    from argos_studio.core import Store
    from argos_studio.mavlink import MavlinkReceiver

    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    workdir = output / "simulator"
    workdir.mkdir()
    defaults = workdir / "profile.parm"
    defaults.write_text(DEFAULT_PARAMETERS, encoding="ascii")
    store = Store(output / "studio.sqlite3")
    receiver = MavlinkReceiver(store, max_duration_s=30)
    # The namespace is isolated; a temporary socket selects an unused local port.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    session = await receiver.start(
        "Passive SITL reception",
        "Observe native autopilot telemetry, stop its source and verify stale reception.",
        listen_port=port,
        system_id=1,
        component_id=1,
    )
    session_id = session["id"]
    arguments = [
        str(binary),
        "--model",
        "quad",
        "--speedup",
        "1",
        "--wipe",
        "--home",
        "-35.363261,149.165230,584,353",
        "--serial1",
        f"udpclient:127.0.0.1:{port}",
        "--defaults",
        str(defaults),
        "--rc-in-port",
        "0",
        "--sim-address",
        "127.0.0.1",
        "--sim-port-in",
        "0",
        "--sim-port-out",
        "0",
        "--irlock-port",
        "0",
    ]
    for serial in (0, 2, 5, 6, 7, 8):
        arguments.extend((f"--serial{serial}", "none"))
    process = None
    with (output / "simulator.log").open("xb") as log:
        try:
            process = subprocess.Popen(
                arguments,
                cwd=workdir,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("The owned simulator exited before telemetry was ready.")
                fresh = receiver.live_state(session_id)
                samples = store.samples(session_id)
                if fresh["freshness"] == "fresh" and fresh.get("heartbeat") and len(samples) >= 10:
                    break
                await asyncio.sleep(0.05)
            else:
                raise RuntimeError("No fresh source 1/1 HEARTBEAT and ATTITUDE within 15 seconds.")
            if fresh["heartbeat"]["base_mode"] & 128:
                raise RuntimeError("The simulator unexpectedly reported armed state.")
            if fresh["peer"]["host"] != "127.0.0.1":
                raise RuntimeError("The source peer was not loopback.")
            startup_sample_count = len(samples)
            # Retain a measured interval after startup, without sending protocol traffic.
            await asyncio.sleep(1)
            shutdown = await stop_owned_process(process)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                stale = receiver.live_state(session_id)
                if (
                    stale["freshness"] == "stale"
                    and stale["connection_status"] == "stale"
                    and stale["heartbeat_age_s"] is not None
                    and stale["heartbeat_age_s"] > 2.5
                ):
                    break
                await asyncio.sleep(0.05)
            else:
                raise RuntimeError("Retained telemetry did not become stale after source exit.")
            before_stop = store.samples(session_id)
            await receiver.stop(session_id)
        finally:
            if process is not None:
                await stop_owned_process(process)
            await receiver.stop(session_id)

    reopened = Store(output / "studio.sqlite3")
    exported = reopened.snapshot(session_id)
    retained = exported["samples"]
    if retained != before_stop:
        raise RuntimeError("Measurements changed while stopping or reopening the receiver.")
    if exported["session"]["status"] != "completed":
        raise RuntimeError("The explicitly stopped session did not finish normally.")
    if len(retained) <= startup_sample_count:
        raise RuntimeError("No continued acquisition was observed after startup.")
    if exported["analysis"]["sample_count"] != len(retained):
        raise RuntimeError("Export analysis and retained measurements disagree.")
    datagrams = reopened.datagrams(session_id)
    if sum(item["sample_count"] for item in datagrams) != len(retained):
        raise RuntimeError("Raw datagram references and retained measurements disagree.")
    raw_bytes = sum(len(base64.b64decode(item["raw_base64"])) for item in datagrams)
    if exported["capture"]["raw_bytes"] != raw_bytes:
        raise RuntimeError("Export capture counts and retained bytes disagree.")
    (output / "capture.json").write_text(
        json.dumps({"schema_version": 1, "datagrams": datagrams}, indent=2) + "\n",
        encoding="utf-8",
    )
    (output / "session.json").write_text(
        json.dumps({"schema_version": 1, **exported}, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    result = {
        "environment": "simulation",
        "simulation": {"version": SITL_VERSION, "sha256": SITL_SHA256, "commit": SITL_COMMIT},
        "scope": "Passive ground telemetry; no MAVLink commands, arming or physical hardware.",
        "session_id": session_id,
        "sample_count": len(retained),
        "capture": exported["capture"],
        "fresh_state": fresh,
        "stale_state": stale,
        "shutdown": shutdown,
        "argv": arguments,
        "defaults_sha256": hashlib.sha256(DEFAULT_PARAMETERS.encode("ascii")).hexdigest(),
        "durable_export": "session.json",
        "raw_export": "capture.json",
    }
    (output / "result.json").write_text(
        json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="A new evidence directory")
    arguments = parser.parse_args()
    try:
        result = asyncio.run(run_smoke(arguments.binary, arguments.output))
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, f"SITL check failed: {exc}\n")
    print(json.dumps({"session_id": result["session_id"], "sample_count": result["sample_count"]}))


if __name__ == "__main__":
    main()
