from __future__ import annotations

import os

import pytest

from control import settings


@pytest.fixture(autouse=True)
def config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("OCV_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("OCV_ENV_FILE", str(tmp_path / "cfg" / ".env"))
    return tmp_path / "cfg"


def _valid(**overrides):
    values = settings.defaults()
    values.update(
        {
            "TELEGRAM_BOT_TOKEN": "123456789:AAFakeTokenValueForTests1234567890",
            "ADMIN_USER_ID": "123456789",
            "OPENCODE_SERVER_PASSWORD": "secret",
        }
    )
    values.update(overrides)
    return values


def test_defaults_point_at_the_config_dir(config_dir):
    values = settings.defaults()

    assert values["DB_PATH"] == str(config_dir / "data" / "bot.db")
    assert values["MEDIA_DIR"] == str(config_dir / "media")
    assert values["OPENCODE_BASE_URL"] == "http://localhost:4096"


def test_fields_cover_every_group():
    groups = settings.fields_by_group()

    assert list(groups) == list(settings.GROUPS)
    assert all(groups.values())
    keys = [field.key for field in settings.FIELDS]
    assert len(keys) == len(set(keys))


def test_read_env_parses_quotes_comments_and_unknown_keys(config_dir):
    path = config_dir / ".env"
    path.parent.mkdir(parents=True)
    path.write_text(
        "# comment\n\nA=1\nB = two \nC=\"three\"\nD='four'\nE=not a comment # kept\n",
        encoding="utf-8",
    )

    values = settings.read_env(path)

    assert values == {"A": "1", "B": "two", "C": "three", "D": "four", "E": "not a comment # kept"}


def test_read_env_missing_file_is_empty(config_dir):
    assert settings.read_env(config_dir / "nope.env") == {}


def test_write_env_is_atomic_and_round_trips(config_dir):
    path = settings.write_env(_valid())

    assert path == config_dir / ".env"
    assert settings.read_env(path)["ADMIN_USER_ID"] == "123456789"
    assert not list(config_dir.glob(".env.*.tmp"))
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_write_env_keeps_unknown_keys(config_dir):
    values = _valid(CUSTOM_KEY="kept")

    settings.write_env(values)

    assert settings.read_env()["CUSTOM_KEY"] == "kept"
    assert "# Other" in (config_dir / ".env").read_text(encoding="utf-8")


def test_validate_accepts_a_complete_config():
    errors, _ = settings.validate(_valid())

    assert errors == []


@pytest.mark.parametrize(
    "key,value,message",
    [
        ("TELEGRAM_BOT_TOKEN", "", "Bot token is required."),
        ("TELEGRAM_BOT_TOKEN", "not-a-token", "does not look like"),
        ("ADMIN_USER_ID", "", "Admin Telegram id is required."),
        ("ADMIN_USER_ID", "abc", "must be a positive integer"),
        ("OPENCODE_BASE_URL", "localhost:4096", "must start with http"),
        ("TELEGRAM_WEBHOOK_PORT", "70000", "between 1 and 65535"),
        ("OPENCODE_TIMEOUT", "soon", "positive number"),
        ("MEDIA_MAX_MB", "0", "positive integer"),
        ("LOG_LEVEL", "LOUD", "Log level must be one of"),
        ("DB_PATH", " ", "Database path is required."),
    ],
)
def test_validate_rejects(key, value, message):
    errors, _ = settings.validate(_valid(**{key: value}))

    assert any(message in error for error in errors)


def test_validate_requires_webhook_path_when_url_set():
    errors, _ = settings.validate(_valid(TELEGRAM_WEBHOOK_URL="https://x.test"))

    assert any("Webhook path is required" in error for error in errors)


def test_validate_warns_about_missing_optional_secrets():
    _, warnings = settings.validate(
        _valid(STT_API_KEY="", OPENCODE_SERVER_PASSWORD="", TELEGRAM_WEBHOOK_URL="https://x.test", TELEGRAM_WEBHOOK_PATH="p")
    )

    joined = " ".join(warnings)
    assert "voice notes" in joined
    assert "unsecured" in joined
    assert "no secret token" in joined


def test_save_merges_and_ignores_unknown_keys(config_dir):
    settings.write_env(_valid())

    merged = settings.save({"LOG_LEVEL": "debug", "NOT_A_KEY": "x"})

    assert merged["LOG_LEVEL"] == "debug"
    assert settings.read_env()["LOG_LEVEL"] == "debug"
    assert "NOT_A_KEY" not in settings.read_env()


def test_save_keeps_untouched_secrets(config_dir):
    settings.write_env(_valid())

    settings.save({"LOG_LEVEL": "WARNING"})

    assert settings.read_env()["TELEGRAM_BOT_TOKEN"].startswith("123456789:")
    assert settings.read_env()["OPENCODE_SERVER_PASSWORD"] == "secret"


def test_save_rejects_invalid_values(config_dir):
    settings.write_env(_valid())

    with pytest.raises(ValueError, match="Bot token"):
        settings.save({"TELEGRAM_BOT_TOKEN": ""})

    assert settings.read_env()["TELEGRAM_BOT_TOKEN"].startswith("123456789:")


def test_missing_required_detects_unconfigured(config_dir):
    assert settings.missing_required(settings.defaults()) == [
        "TELEGRAM_BOT_TOKEN",
        "ADMIN_USER_ID",
    ]
    assert settings.missing_required(_valid()) == []


def test_missing_required_reports_settings_when_invalid(config_dir):
    values = _valid(TELEGRAM_BOT_TOKEN="bad")

    assert settings.missing_required(values) == ["SETTINGS"]


def test_payload_hides_secrets(config_dir):
    settings.write_env(_valid())

    payload = settings.payload()

    assert payload["values"]["TELEGRAM_BOT_TOKEN"] == ""
    assert payload["secrets_set"]["TELEGRAM_BOT_TOKEN"] is True
    assert payload["secrets_set"]["STT_API_KEY"] is False
    assert payload["values"]["OPENCODE_BASE_URL"] == "http://localhost:4096"
    assert payload["configured"] is True
    assert payload["env_file"].endswith(".env")
    assert "TELEGRAM_BOT_TOKEN" in [field["key"] for field in payload["fields"]]


def test_payload_marks_placeholder_token_as_unconfigured(config_dir):
    values = _valid(TELEGRAM_BOT_TOKEN="your-bot-token-here")
    settings.write_env(values)

    payload = settings.payload()

    assert payload["secrets_set"]["TELEGRAM_BOT_TOKEN"] is False
    assert payload["configured"] is False

def test_write_env_quotes_values_with_comment_markers(config_dir):
    settings.write_env(_valid(OPENCODE_AGENT="build # not a comment"))

    text = (config_dir / ".env").read_text(encoding="utf-8")
    assert 'OPENCODE_AGENT="build # not a comment"' in text
    assert settings.read_env()["OPENCODE_AGENT"] == "build # not a comment"


def test_write_env_rejects_line_breaks(config_dir):
    with pytest.raises(ValueError, match="line breaks"):
        settings.write_env(_valid(OPENCODE_AGENT="build\nADMIN_USER_ID=999"))


def test_save_refuses_to_overwrite_an_undecodable_file(config_dir):
    path = config_dir / ".env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes("TELEGRAM_BOT_TOKEN=x\n".encode("utf-16"))

    with pytest.raises(ValueError, match="not valid UTF-8"):
        settings.save({"LOG_LEVEL": "DEBUG"})

    assert path.read_text(encoding="utf-16").startswith("TELEGRAM_BOT_TOKEN")


def test_read_env_ignores_an_undecodable_file(config_dir):
    path = config_dir / ".env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes("A=1\n".encode("utf-16"))

    assert settings.read_env(path) == {}
