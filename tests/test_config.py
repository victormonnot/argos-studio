import os
import sys

import pytest
from fastapi.testclient import TestClient

from argos_studio import __main__
from argos_studio.app import Settings, create_app
from argos_studio.config import load_environment

TEST_KEY = "sk-test-dotenv-not-a-real-key"
CONFIG_VARIABLES = (
    "ARGOS_STUDIO_DATA_DIR",
    "ARGOS_STUDIO_AGENT_PROVIDER",
    "ARGOS_STUDIO_AGENT_MODEL",
    "ARGOS_STUDIO_ARGOS_ROOT",
    "ARGOS_STUDIO_ARGOS_PYTHON",
    "OPENAI_API_KEY",
    "PYTHON_DOTENV_DISABLED",
)


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", dict(os.environ))
    for name in CONFIG_VARIABLES:
        os.environ.pop(name, None)


def write_agent_environment(path):
    path.write_text(
        "ARGOS_STUDIO_AGENT_PROVIDER=openai\n"
        "ARGOS_STUDIO_AGENT_MODEL=test-model\n"
        f"OPENAI_API_KEY={TEST_KEY}\n",
        encoding="utf-8",
    )


def test_factory_loads_local_environment_without_disclosing_secret(tmp_path):
    write_agent_environment(tmp_path / ".env")

    with TestClient(create_app()) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["agent"]["available"] is True
        assert health.json()["agent"]["provider"] == "openai"
        assert health.json()["agent"]["model"] == "test-model"
        assert TEST_KEY not in health.text

    settings = Settings()
    assert settings.agent_config.api_key == TEST_KEY
    assert TEST_KEY not in repr(settings)


@pytest.mark.parametrize("existing_value", ["already-configured", ""])
def test_existing_environment_takes_precedence_even_when_empty(
    tmp_path, monkeypatch, existing_value
):
    write_agent_environment(tmp_path / ".env")
    monkeypatch.setenv("OPENAI_API_KEY", existing_value)

    load_environment()

    assert os.environ["OPENAI_API_KEY"] == existing_value
    assert os.environ["ARGOS_STUDIO_AGENT_MODEL"] == "test-model"


def test_missing_environment_file_is_optional(monkeypatch):
    monkeypatch.setenv("ARGOS_STUDIO_AGENT_MODEL", "existing-model")

    load_environment()

    assert os.environ["ARGOS_STUDIO_AGENT_MODEL"] == "existing-model"
    assert "OPENAI_API_KEY" not in os.environ


def test_environment_from_parent_directory_is_not_loaded(tmp_path, monkeypatch):
    write_agent_environment(tmp_path / ".env")
    child = tmp_path / "another-project"
    child.mkdir()
    monkeypatch.chdir(child)

    load_environment()

    assert "OPENAI_API_KEY" not in os.environ
    assert "ARGOS_STUDIO_AGENT_MODEL" not in os.environ


def test_environment_values_are_literal_without_shell_or_variable_expansion(tmp_path):
    (tmp_path / ".env").write_text(
        "ARGOS_STUDIO_AGENT_MODEL=test-model\n"
        "OPENAI_API_KEY='${ARGOS_STUDIO_AGENT_MODEL}'\n"
        "ARGOS_STUDIO_DATA_DIR='$(touch command-was-executed)'\n",
        encoding="utf-8",
    )

    load_environment()

    assert os.environ["OPENAI_API_KEY"] == "${ARGOS_STUDIO_AGENT_MODEL}"
    assert os.environ["ARGOS_STUDIO_DATA_DIR"] == "$(touch command-was-executed)"
    assert not (tmp_path / "command-was-executed").exists()


@pytest.mark.parametrize(
    ("arguments", "expected_data_dir"),
    [([], "configured-data"), (["--data-dir", "explicit-data"], "explicit-data")],
)
def test_cli_loads_environment_before_defaults_and_preserves_arguments(
    tmp_path, monkeypatch, capsys, arguments, expected_data_dir
):
    write_agent_environment(tmp_path / ".env")
    with (tmp_path / ".env").open("a", encoding="utf-8") as configuration:
        configuration.write("ARGOS_STUDIO_DATA_DIR=configured-data\n")
    monkeypatch.setattr(sys, "argv", ["argos-studio", *arguments])
    calls = []

    def run(application, **kwargs):
        calls.append((application, kwargs))
        assert os.environ["ARGOS_STUDIO_DATA_DIR"] == expected_data_dir
        assert Settings().agent_config.api_key == TEST_KEY

    monkeypatch.setattr(__main__.uvicorn, "run", run)

    __main__.main()

    assert calls == [
        (
            "argos_studio.app:create_app",
            {"factory": True, "host": "127.0.0.1", "port": 8765},
        )
    ]
    captured = capsys.readouterr()
    assert TEST_KEY not in captured.out + captured.err


def test_explicit_settings_do_not_load_environment_file(tmp_path):
    write_agent_environment(tmp_path / ".env")
    settings = Settings(data_dir=tmp_path / "explicit-data")

    with TestClient(create_app(settings)) as client:
        assert client.get("/api/health").json()["agent"]["available"] is False

    assert "OPENAI_API_KEY" not in os.environ
    assert settings.agent_config.provider == ""
