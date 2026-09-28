"""Emit bounded, explicitly synthetic MAVLink packets to loopback for browser checks."""

import argparse
import math
import socket
import time

from pymavlink.dialects.v20 import ardupilotmega as dialect


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--duration", type=float, default=2.0)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    if not math.isfinite(args.duration) or not 0.1 <= args.duration <= 10.0:
        parser.error("duration must be finite and between 0.1 and 10 seconds")

    target = ("127.0.0.1", args.port)
    encoder = dialect.MAVLink(None, srcSystem=1, srcComponent=1)
    foreign = dialect.MAVLink(None, srcSystem=2, srcComponent=1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        sender.bind(("127.0.0.1", 0))
        # Preserve both rejected and accepted evidence in the same capture.
        sender.sendto(foreign.heartbeat_encode(2, 3, 0, 0, 3).pack(foreign), target)
        sender.sendto(b"invalid-synthetic-browser-fixture", target)
        started = time.monotonic()
        while time.monotonic() - started < args.duration:
            elapsed = time.monotonic() - started
            attitude = encoder.attitude_encode(
                int(elapsed * 1000),
                0.12 * math.sin(elapsed),
                0.04 * math.cos(elapsed),
                0.0,
                0.12 * math.cos(elapsed),
                0.0,
                0.0,
            )
            heartbeat = encoder.heartbeat_encode(2, 3, 0, 0, 3)
            sender.sendto(attitude.pack(encoder) + heartbeat.pack(encoder), target)
            encoder.seq = (encoder.seq + 1) % 256
            time.sleep(0.05)


if __name__ == "__main__":
    main()
