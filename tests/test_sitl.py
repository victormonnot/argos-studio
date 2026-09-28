"""Optional native simulator integration, isolated from host network endpoints."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_passive_sitl_reception_staleness_and_durable_export(tmp_path):
    binary = os.environ.get("ARGOS_STUDIO_SITL_BINARY")
    if not binary:
        pytest.skip("Set ARGOS_STUDIO_SITL_BINARY to the pinned ArduCopter 4.6.3 executable.")
    if sys.platform != "linux" or not shutil.which("unshare") or not shutil.which("ip"):
        pytest.skip("The native SITL check requires Linux, unshare and iproute2.")
    namespace = [
        "unshare",
        "--user",
        "--map-root-user",
        "--net",
        "--pid",
        "--fork",
        "--kill-child=SIGKILL",
        "/bin/sh",
        "-c",
    ]
    probe = subprocess.run(
        [*namespace, "ip link set dev lo up"],
        capture_output=True,
        timeout=5,
        check=False,
    )
    if probe.returncode:
        pytest.skip("User/network namespaces are unavailable; isolated SITL was not launched.")
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(root / "src")
    output = tmp_path / "sitl-evidence"
    completed = subprocess.run(
        [
            *namespace,
            'ip link set dev lo up && exec "$@"',
            "sh",
            sys.executable,
            "-B",
            str(root / "tools/run_sitl.py"),
            "--binary",
            str(Path(binary).expanduser().resolve()),
            "--output",
            str(output),
        ],
        capture_output=True,
        timeout=35,
        check=False,
        env=environment,
        cwd=root,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    result = json.loads((output / "result.json").read_text())
    exported = json.loads((output / "session.json").read_text())
    assert result["environment"] == "simulation"
    assert result["sample_count"] >= 10
    assert result["fresh_state"]["freshness"] == "fresh"
    assert result["stale_state"]["freshness"] == "stale"
    assert result["stale_state"]["connection_status"] == "stale"
    assert result["stale_state"]["heartbeat_freshness"] == "stale"
    assert result["stale_state"]["heartbeat_age_s"] > 2.5
    assert result["shutdown"]["returncode"] is not None
    assert exported["session"]["status"] == "completed"
    assert exported["analysis"]["sample_count"] == len(exported["samples"])
    assert exported["session"]["sample_count"] == result["sample_count"]
    datagrams = json.loads((output / "capture.json").read_text())["datagrams"]
    assert len(datagrams) == result["capture"]["datagram_count"]
    assert sum(item["sample_count"] for item in datagrams) == result["sample_count"]
