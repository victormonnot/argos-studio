"""Isolated, read-only adapter for a configured ARGOS source checkout.

This file is executed with that checkout's Python interpreter. It deliberately
uses only the passive recording decoder; no live link or console is created.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import sys
from collections.abc import Mapping
from pathlib import Path

MAX_RECORDING_BYTES = 10 * 1024 * 1024
MAX_EVENTS = 100_000


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def convert_recording(recording, raw: bytes) -> dict:
    """Map validated receptions while preserving both independent clocks."""
    attitude = [event for event in recording.events if event.type_name == "ATTITUDE"]
    if not attitude:
        raise ValueError("NO_ATTITUDE")
    sources = {(event.system, event.component) for event in attitude}
    if len(sources) != 1:
        raise ValueError("MULTIPLE_SOURCES")
    samples = []
    for event in attitude:
        fields = event.fields
        names = ("roll", "pitch", "yaw", "rollspeed")
        values = [fields.get(name) for name in names]
        boot_ms = fields.get("time_boot_ms")
        if (
            any(type(value) not in (int, float) or not math.isfinite(value) for value in values)
            or type(boot_ms) is not int
            or not 0 <= boot_ms <= 0xFFFFFFFF
        ):
            raise ValueError("INVALID_ATTITUDE")
        samples.append(
            {
                "source_time_s": boot_ms / 1000,
                "elapsed_s": event.received_at - recording.started_at,
                "received_at": event.received_at,
                "roll_deg": math.degrees(fields["roll"]),
                "pitch_deg": math.degrees(fields["pitch"]),
                "yaw_deg": math.degrees(fields["yaw"]),
                "gyro_x_deg_s": math.degrees(fields["rollspeed"]),
            }
        )
    context = _plain(recording.context) if recording.context is not None else None
    configuration = context.get("configuration") if isinstance(context, dict) else None
    declared_environment = (
        configuration.get("environment") if isinstance(configuration, dict) else None
    )
    # Preserve external declarations, and never infer hardware origin from MAVLink.
    environment = (
        declared_environment if declared_environment in ("simulation", "real") else "unknown"
    )
    system, component = next(iter(sources))
    header = json.loads(raw.split(b"\n", 1)[0])
    return {
        "name": "ARGOS recorded session",
        "source": "argos-recording",
        "duration_s": recording.ended_at - recording.started_at,
        "samples": samples,
        "metadata": {
            "format": "argos.mavlink.rx",
            "recording_version": header["version"],
            "recorded_codec_version": recording.codec_version,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "environment": environment,
            "provenance": (
                "Imported recording; environment is declared by its capture context, "
                "not independently verified."
            ),
            "received_at_clock": "source_local_monotonic",
            "source_time_clock": "vehicle_boot_uint32_ms",
            "elapsed_origin": "recording.started_at",
            "clock_note": (
                "Vehicle and receiver clocks are independent. "
                "Receipt times are not Unix timestamps or end-to-end latency."
            ),
            "recording_started_at": recording.started_at,
            "recording_ended_at": recording.ended_at,
            "native_context": context,
            "captured_at_utc": context.get("captured_at_utc")
            if isinstance(context, dict)
            else None,
            "vehicle": {"system_id": system, "component_id": component},
            "native_event_count": len(recording.events),
            "attitude_sample_count": len(samples),
            "end_reason": recording.end_reason,
            "end_detail": recording.end_detail,
            "scope": (
                "ATTITUDE measurements only; other messages and visual sidecars are not imported."
            ),
        },
    }


def main() -> int:
    if len(sys.argv) != 2:
        sys.stderr.write("INVALID_INPUT")
        return 1
    root = Path(sys.argv[1])
    if not root.is_absolute() or not (root / "argos/backends/mavlink/recording.py").is_file():
        sys.stderr.write("DEPENDENCY_UNAVAILABLE")
        return 1
    sys.path.insert(0, str(root))
    try:
        from argos.backends.mavlink.recording import read_recording

        raw = sys.stdin.buffer.read(MAX_RECORDING_BYTES + 1)
        if not raw or len(raw) > MAX_RECORDING_BYTES:
            raise ValueError("INVALID_INPUT")
        recording = read_recording(io.BytesIO(raw), max_events=MAX_EVENTS)
        output = convert_recording(recording, raw)
        from importlib.metadata import version

        output["metadata"]["decoder_codec_version"] = version("pymavlink")
        sys.stdout.write(json.dumps(output, allow_nan=False, separators=(",", ":")))
        return 0
    except ImportError:
        sys.stderr.write("DEPENDENCY_UNAVAILABLE")
    except Exception as exc:
        code = str(exc)
        sys.stderr.write(
            code
            if code in {"MULTIPLE_SOURCES", "NO_ATTITUDE", "INVALID_ATTITUDE"}
            else "INVALID_RECORDING"
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
