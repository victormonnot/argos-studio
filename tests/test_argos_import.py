import json
import math
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from argos_studio.argos_import import MAX_RECORDING_BYTES, read_argos_recording
from argos_studio.argos_reader import convert_recording


def event(*, received_at=101.25, system=1, component=1, roll=math.pi / 2, boot_ms=1234):
    return SimpleNamespace(
        received_at=received_at,
        system=system,
        component=component,
        type_name="ATTITUDE",
        fields={
            "roll": roll,
            "pitch": -0.5,
            "yaw": 0.75,
            "rollspeed": 0.25,
            "time_boot_ms": boot_ms,
        },
    )


def recording(events, context=None):
    return SimpleNamespace(
        started_at=100.0,
        ended_at=103.0,
        codec_version="2.4.49",
        events=events,
        context=context,
        end_reason=None,
        end_detail="",
    )


RAW = b'{"version":1}\n'


def test_preserves_independent_clocks_and_converts_attitude_units():
    result = convert_recording(recording([event()]), RAW)
    sample = result["samples"][0]
    assert sample["received_at"] == 101.25  # Source monotonic time, never Unix time.
    assert sample["elapsed_s"] == 1.25
    assert sample["source_time_s"] == 1.234
    assert sample["roll_deg"] == pytest.approx(90.0)
    assert sample["pitch_deg"] == pytest.approx(math.degrees(-0.5))
    assert sample["gyro_x_deg_s"] == pytest.approx(math.degrees(0.25))
    assert result["duration_s"] == 3.0
    assert result["metadata"]["environment"] == "unknown"
    assert result["metadata"]["captured_at_utc"] is None


def test_declaration_does_not_convert_monotonic_to_wall_time():
    context = {
        "captured_at_utc": "2026-09-10T07:56:01.207139Z",
        "configuration": {"environment": "simulation"},
    }
    result = convert_recording(recording([event()], context), RAW)
    assert result["metadata"]["environment"] == "simulation"
    assert result["metadata"]["native_context"] == context
    assert result["samples"][0]["received_at"] == 101.25


def test_preserves_argos_real_environment_declaration():
    result = convert_recording(
        recording([event()], {"configuration": {"environment": "real"}}), RAW
    )
    assert result["metadata"]["environment"] == "real"


def test_allows_equal_receipt_times_and_sender_clock_reset():
    result = convert_recording(recording([event(boot_ms=5000), event(boot_ms=10)]), RAW)
    assert [sample["source_time_s"] for sample in result["samples"]] == [5.0, 0.01]


@pytest.mark.parametrize(
    "events,code",
    [
        ([], "NO_ATTITUDE"),
        ([event(), event(system=2)], "MULTIPLE_SOURCES"),
        ([event(), event(component=2)], "MULTIPLE_SOURCES"),
        ([event(roll=float("nan"))], "INVALID_ATTITUDE"),
        ([event(boot_ms=-1)], "INVALID_ATTITUDE"),
    ],
)
def test_rejects_ambiguous_or_unusable_attitude(events, code):
    with pytest.raises(ValueError, match=code):
        convert_recording(recording(events), RAW)


@pytest.fixture
def configured_root(tmp_path):
    source = tmp_path / "argos/backends/mavlink/recording.py"
    source.parent.mkdir(parents=True)
    source.write_text("# Explicit test parser location\n")
    return tmp_path


def test_subprocess_is_bounded_and_disables_bytecode(monkeypatch, configured_root):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args, 0, json.dumps({"source": "argos-recording"}).encode(), b""
        )

    monkeypatch.setattr(subprocess, "run", run)
    assert (
        read_argos_recording(
            RAW, python_executable=sys.executable, argos_root=str(configured_root)
        )["source"]
        == "argos-recording"
    )
    args, options = calls[0]
    assert args[0] == sys.executable
    assert args[1] == "-B"
    assert args[-1] == str(configured_root)
    assert options["input"] == RAW
    assert options["timeout"] == 20
    assert options["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert not options.get("shell")


def test_dependency_tracebacks_are_not_returned_to_caller(monkeypatch, configured_root):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [], 1, b"", b"secret /private/path\nTraceback"
        ),
    )
    with pytest.raises(ValueError, match="^The ARGOS recording failed validation\\.$"):
        read_argos_recording(RAW, python_executable=sys.executable, argos_root=str(configured_root))


def test_timeout_has_bounded_public_error(monkeypatch, configured_root):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("reader", 20)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(ValueError, match="exceeded 20 seconds"):
        read_argos_recording(RAW, python_executable=sys.executable, argos_root=str(configured_root))


def test_refuses_oversized_data_before_starting_reader(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: pytest.fail("reader must not start")
    )
    with pytest.raises(ValueError, match="10 MiB"):
        read_argos_recording(
            b"x" * (MAX_RECORDING_BYTES + 1), python_executable=sys.executable, argos_root="/unused"
        )


def test_optional_native_recording_integration():
    root = os.environ.get("ARGOS_STUDIO_ARGOS_ROOT")
    python = os.environ.get("ARGOS_STUDIO_ARGOS_PYTHON")
    if not root or not python:
        pytest.skip("Set both ARGOS installation variables to test the optional native adapter.")
    sample = Path(root) / "examples/demo-flight/05aa147d371144c793c8f88ec3ef0509.jsonl"
    result = read_argos_recording(sample.read_bytes(), python_executable=python, argos_root=root)
    assert len(result["samples"]) == 201
    assert result["duration_s"] == pytest.approx(36.579789561000325)
    assert result["metadata"]["environment"] == "simulation"
    assert result["samples"][0]["received_at"] == pytest.approx(66.18139215200063)
    assert result["samples"][0]["source_time_s"] == 34.606
    broken = sample.read_bytes().replace(b'"received_at":', b'"received_at_bad":', 1)
    with pytest.raises(ValueError, match="failed validation"):
        read_argos_recording(broken, python_executable=python, argos_root=root)
