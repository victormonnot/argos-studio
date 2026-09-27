"""Read native ARGOS recordings through an explicitly configured installation."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

MAX_RECORDING_BYTES = 10 * 1024 * 1024
IMPORT_TIMEOUT_SECONDS = 20


def read_argos_recording(data: bytes, *, python_executable: str, argos_root: str) -> dict:
    """Validate and decode a recording without opening a telemetry transport.

    Both installation paths are explicit trusted configuration. The subprocess
    borrows ARGOS's recording parser, never its console or control services.
    Native receipt times remain monotonic source-clock values, not Unix times.
    """
    if not isinstance(data, bytes) or not data:
        raise ValueError("Provide a nonempty ARGOS recording.")
    if len(data) > MAX_RECORDING_BYTES:
        raise ValueError("ARGOS recordings must not exceed 10 MiB.")
    if not python_executable or not argos_root:
        raise ValueError("Configure the ARGOS interpreter and source directory first.")
    python = Path(python_executable).expanduser()
    root = Path(argos_root).expanduser()
    if not python.is_absolute() or not root.is_absolute():
        raise ValueError("ARGOS installation paths must be absolute.")
    if not python.is_file() or not (root / "argos/backends/mavlink/recording.py").is_file():
        raise ValueError("The configured ARGOS installation is unavailable.")
    reader = Path(__file__).with_name("argos_reader.py").resolve()
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        result = subprocess.run(
            [str(python), "-B", str(reader), str(root)],
            input=data,
            capture_output=True,
            timeout=IMPORT_TIMEOUT_SECONDS,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("ARGOS recording validation exceeded 20 seconds.") from exc
    except OSError as exc:
        raise ValueError("The configured ARGOS interpreter could not be started.") from exc
    # Never forward arbitrary stderr, tracebacks, file paths or recording text.
    if result.returncode:
        messages = {
            b"DEPENDENCY_UNAVAILABLE": (
                "The configured ARGOS installation needs its MAVLink dependencies."
            ),
            b"MULTIPLE_SOURCES": (
                "This recording has ATTITUDE data from multiple sources; "
                "choose a single-vehicle recording."
            ),
            b"NO_ATTITUDE": "This recording contains no ATTITUDE measurements.",
            b"INVALID_ATTITUDE": "This recording contains invalid ATTITUDE measurements.",
        }
        raise ValueError(
            messages.get(result.stderr.strip(), "The ARGOS recording failed validation.")
        )
    try:
        output = json.loads(result.stdout)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("The ARGOS reader returned an invalid result.") from exc
    if not isinstance(output, dict) or output.get("source") != "argos-recording":
        raise ValueError("The ARGOS reader returned an invalid result.")
    return output
