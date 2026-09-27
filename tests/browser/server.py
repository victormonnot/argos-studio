"""Run browser checks against an isolated, disposable data directory."""

from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn

from argos_studio.app import Settings, create_app

if __name__ == "__main__":
    with TemporaryDirectory(prefix="argos-studio-browser-") as directory:
        app = create_app(Settings(data_dir=Path(directory), argos_root=None, argos_python=None))
        uvicorn.run(app, host="127.0.0.1", port=8766)
