from __future__ import annotations

import sys

from control import paths


def test_env_file_honours_override(monkeypatch, tmp_path):
    target = tmp_path / "custom.env"
    monkeypatch.setenv(paths.ENV_FILE_OVERRIDE, str(target))

    assert paths.env_file() == target


def test_config_dir_honours_override(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.CONFIG_DIR_OVERRIDE, str(tmp_path / "cfg"))

    assert paths.config_dir() == tmp_path / "cfg"
    assert paths.logs_dir() == tmp_path / "cfg" / "logs"
    assert paths.bot_log() == tmp_path / "cfg" / "logs" / "bot.log"


def test_config_dir_defaults_below_opencode(tmp_path, monkeypatch):
    monkeypatch.delenv(paths.CONFIG_DIR_OVERRIDE, raising=False)
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: tmp_path))

    assert paths.config_dir() == tmp_path / ".config" / "opencode" / "opencode-voice"


def test_env_file_prefers_repo_env_in_dev(tmp_path, monkeypatch):
    monkeypatch.delenv(paths.ENV_FILE_OVERRIDE, raising=False)
    monkeypatch.setattr(paths, "REPO_ROOT", tmp_path)
    (tmp_path / ".env").write_text("A=1\n", encoding="utf-8")

    assert paths.env_file() == tmp_path / ".env"


def test_env_file_falls_back_to_config_dir(monkeypatch, tmp_path):
    monkeypatch.delenv(paths.ENV_FILE_OVERRIDE, raising=False)
    monkeypatch.setattr(paths, "REPO_ROOT", tmp_path / "repo")
    monkeypatch.setenv(paths.CONFIG_DIR_OVERRIDE, str(tmp_path / "cfg"))

    assert paths.env_file() == tmp_path / "cfg" / ".env"


def test_bot_command():
    assert paths.bot_command() == [sys.executable, "-m", "control", "bot"]


def test_panel_command():
    assert paths.panel_command(port=9100) == [
        sys.executable,
        "-m",
        "control",
        "serve",
        "--no-open",
        "--port",
        "9100",
    ]


def test_default_port(monkeypatch):
    monkeypatch.delenv(paths.PORT_ENV, raising=False)
    assert paths.default_port() == paths.DEFAULT_PORT

    monkeypatch.setenv(paths.PORT_ENV, "9999")
    assert paths.default_port() == 9999

    monkeypatch.setenv(paths.PORT_ENV, "nonsense")
    assert paths.default_port() == paths.DEFAULT_PORT


def test_ensure_dirs_creates_layout(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.CONFIG_DIR_OVERRIDE, str(tmp_path / "cfg"))

    paths.ensure_dirs()

    for directory in ("", "data", "media", "logs", "run"):
        assert (tmp_path / "cfg" / directory).is_dir()


def test_command_cwd_is_the_repo_root():
    assert paths.command_cwd() == paths.REPO_ROOT
