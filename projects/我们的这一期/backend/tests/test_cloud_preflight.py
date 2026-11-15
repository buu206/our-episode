"""Offline preflight must report facts without revealing values or touching data."""
import os
from pathlib import Path
import socket
import sys

import httpx
import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import cloud_preflight as preflight


@pytest.fixture(autouse=True)
def forbid_network_and_clients(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("preflight must not create clients or connect")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(httpx, "Client", denied)
    monkeypatch.setattr(httpx, "AsyncClient", denied)


@pytest.fixture
def config():
    return {"CLOUD_ENABLED": "true", "CLOUD_RETENTION_VERIFIED": "true", "CLOUD_REGION_VERIFIED": "true",
            "MODEL_API_KEY": "local-fake-key", "MODEL_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "MODEL_NAME": "qwen3-vl-plus"}


def statuses(values, tmp_path):
    return {item.name: item.status for item in preflight.check_configuration(values, product=tmp_path)}


def test_empty_config_and_key_alone_do_not_unlock(tmp_path):
    empty = statuses({}, tmp_path)
    with_key = statuses({"MODEL_API_KEY": "local-fake-key"}, tmp_path)
    for result in (empty, with_key):
        assert all(result[name] == "missing" for name in preflight.SWITCHES)
        assert all(result[name] == "missing" for name in ("DIRECTOR_ADAPTER", "VISION_ADAPTER", "SPEECH_ADAPTER"))
    assert list(tmp_path.iterdir()) == []


def test_ready_is_offline_and_does_not_read_database_or_media(config, tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    database = data / "app.sqlite3"
    database.write_bytes(b"not a database")
    media = data / "private.mp4"
    media.write_bytes(b"private-content")
    before = {p.name: p.stat() for p in data.iterdir()}
    def denied(*args, **kwargs):
        raise AssertionError("no application data reads")
    monkeypatch.setattr(Path, "open", denied)
    result = statuses(config, tmp_path)
    assert all(value == "ready" for key, value in result.items() if key != "BUDGET")
    assert result["BUDGET"] == "not_checked"
    assert {p.name: p.stat() for p in data.iterdir()} == before


@pytest.mark.parametrize("name,value", [
    ("MODEL_BASE_URL", "http://dashscope.aliyuncs.com/compatible-mode/v1"),
    ("MODEL_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1?credential=private-value"),
    ("MODEL_BASE_URL", "https://private-value@dashscope.aliyuncs.com/compatible-mode/v1"),
    ("MODEL_BASE_URL", "https://localhost/compatible-mode/v1"),
    ("MODEL_BASE_URL", "https://dashscope.aliyuncs.com/api/v1"),
    ("MODEL_NAME", "private-value"), ("ASR_MODEL", "private-value"), ("TTS_MODEL", "private-value"),
    ("MODEL_API_KEY", "private value"),
])
def test_adapter_restrictions_do_not_echo_values(config, tmp_path, name, value):
    config[name] = value
    checks = preflight.check_configuration(config, product=tmp_path)
    assert next(item.status for item in checks if item.name == name) == "invalid"
    assert value not in repr(checks)


def test_environment_precedence_and_interpolation_without_mutation(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("PREFLIGHT_TEST_CHOICE=file-value\nMODEL_API_KEY=${PREFLIGHT_TEST_CHOICE}\nCLOUD_ENABLED=true\n")
    monkeypatch.setenv("PREFLIGHT_TEST_CHOICE", "environment-value")
    monkeypatch.setenv("CLOUD_ENABLED", "false")
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    before = dict(os.environ)
    values = preflight.read_configuration(env_file)
    assert values["MODEL_API_KEY"] == "environment-value"
    assert values["CLOUD_ENABLED"] == "false"
    assert dict(os.environ) == before


@pytest.mark.parametrize("flag", ["1", "TRUE", "t", "Yes", "y"])
def test_disabled_dotenv_matches_settings_and_does_not_open_file(tmp_path, monkeypatch, flag):
    from dotenv.main import DotEnv
    def denied(*args, **kwargs):
        raise AssertionError("disabled dotenv must not read a file")
    monkeypatch.setattr(DotEnv, "dict", denied)
    supplied = {"PYTHON_DOTENV_DISABLED": flag, "MODEL_API_KEY": "local-fake-key"}
    assert preflight.read_configuration(tmp_path / ".env", supplied) == supplied


def test_bare_dotenv_keys_are_ignored_and_empty_directory_matches_settings(config, tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("ASR_MODEL\nTTS_MODEL\nAPP_DATA_DIR=\n")
    values = preflight.read_configuration(env_file, {})
    assert "ASR_MODEL" not in values and "TTS_MODEL" not in values
    monkeypatch.chdir(tmp_path)
    result = statuses({**config, **values}, tmp_path)
    assert result["ASR_MODEL"] == result["TTS_MODEL"] == result["APP_DATA_DIR"] == "ready"


@pytest.mark.parametrize("kind", ["demo", "demo_child", "demo_symlink", "file"])
def test_reject_demo_aliases_and_non_directory(config, tmp_path, kind):
    demo = tmp_path / "data/demo"
    demo.mkdir(parents=True)
    if kind == "demo":
        target = demo
    elif kind == "demo_child":
        target = demo / "nested"
    elif kind == "demo_symlink":
        target = tmp_path / "normal-alias"
        target.symlink_to(demo, target_is_directory=True)
    else:
        target = tmp_path / "file"
        target.write_text("not a directory")
    config["APP_DATA_DIR"] = str(target)
    assert statuses(config, tmp_path)["APP_DATA_DIR"] == "invalid"


def test_custom_directory_is_allowed_without_creation(config, tmp_path):
    target = tmp_path / "another-normal-location"
    config["APP_DATA_DIR"] = str(target)
    assert statuses(config, tmp_path)["APP_DATA_DIR"] == "ready"
    assert not target.exists()


def test_cli_ready_and_missing_have_distinct_codes_and_no_values(config, tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("\n".join(f"{key}={value}" for key, value in config.items()))
    content = env_file.read_bytes()
    assert preflight.main([], env_file=env_file, environ={}, product=tmp_path) == 0
    output = capsys.readouterr().out
    assert config["MODEL_API_KEY"] not in output
    assert config["MODEL_BASE_URL"] not in output
    assert config["MODEL_NAME"] not in output
    assert str(tmp_path) not in output
    assert preflight.NOTICE in output and "预算仍由服务端检查" in output
    assert env_file.read_bytes() == content
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]
    assert preflight.main([], env_file=tmp_path / "missing", environ={}, product=tmp_path) == 2
    assert "本地配置未就绪" in capsys.readouterr().out


def test_adapter_exception_is_sanitized(config, tmp_path, monkeypatch, capsys):
    from app.services.director import CloudDirector
    def failure(*args):
        raise RuntimeError("private-value-must-not-leak")
    monkeypatch.setattr(CloudDirector, "ready", failure)
    assert preflight.main([], env_file=tmp_path / "missing", environ=config, product=tmp_path) == 2
    output = capsys.readouterr().out
    assert "private-value-must-not-leak" not in output
    assert "适配器本地检查失败" in output


def test_read_failure_and_help_never_echo_or_load_config(tmp_path, monkeypatch, capsys):
    def failure(*args):
        raise OSError("private-value-must-not-leak")
    monkeypatch.setattr(preflight, "read_configuration", failure)
    with pytest.raises(SystemExit) as stopped:
        preflight.main(["--help"])
    assert stopped.value.code == 0
    assert "Offline configuration check" in capsys.readouterr().out
    assert preflight.main([], product=tmp_path) == 2
    output = capsys.readouterr().out
    assert "private-value-must-not-leak" not in output
    assert "CONFIGURATION" in output
