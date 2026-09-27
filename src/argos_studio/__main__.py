"""Local, single-process application entry point."""

import argparse
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local ARGOS Studio workspace.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default=os.environ.get("ARGOS_STUDIO_DATA_DIR", ".data"))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    os.environ["ARGOS_STUDIO_DATA_DIR"] = args.data_dir
    uvicorn.run("argos_studio.app:create_app", factory=True, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
