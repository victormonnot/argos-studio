"""Load optional local configuration without changing explicit process settings."""

from pathlib import Path

from dotenv import load_dotenv


def load_environment() -> None:
    # Restrict discovery to the launch directory: a neighboring project's .env
    # must not silently configure Studio. Values are data, not shell commands.
    load_dotenv(Path.cwd() / ".env", override=False, interpolate=False)
