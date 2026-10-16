from __future__ import annotations

import pytest
from dotenv import dotenv_values

import config


def test_env_file_overrides_exported_environment(monkeypatch):
    # ``config`` loads the root .env with override=True so a stale exported
    # variable cannot shadow the file (which caused 401s against opencode).
    import importlib

    file_value = dotenv_values(config.ENV_FILE).get("OPENCODE_SERVER_PASSWORD")
    if not file_value:
        pytest.skip("no OPENCODE_SERVER_PASSWORD in .env")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "definitely-not-the-file-value")
    try:
        reloaded = importlib.reload(config)
        assert reloaded.OPENCODE_SERVER_PASSWORD == file_value.strip()
    finally:
        importlib.reload(config)


def test_admin_user_id_required(monkeypatch):
    monkeypatch.delenv("ADMIN_USER_ID", raising=False)
    with pytest.raises(RuntimeError):
        config._parse_admin_id()


def test_admin_user_id_parses_numeric(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_ID", "123456789")
    assert config._parse_admin_id() == 123456789


def test_admin_user_id_rejects_non_numeric(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_ID", "not-an-id")
    with pytest.raises(RuntimeError):
        config._parse_admin_id()


def test_webhook_disabled_when_url_empty(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_WEBHOOK_URL", "")
    monkeypatch.setattr(config, "TELEGRAM_WEBHOOK_PORT", 8080)
    assert config.webhook_enabled() is False


def test_webhook_enabled_when_url_set(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_WEBHOOK_URL", "https://t.example")
    monkeypatch.setattr(config, "TELEGRAM_WEBHOOK_PORT", 8080)
    assert config.webhook_enabled() is True


def test_cloudflared_disabled_without_token(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_WEBHOOK_URL", "https://t.example")
    monkeypatch.setattr(config, "TELEGRAM_WEBHOOK_PORT", 8080)
    monkeypatch.setattr(config, "CLOUDFLARED_TUNNEL_TOKEN", "")
    assert config.cloudflared_enabled() is False


def test_cloudflared_enabled_with_url_and_token(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_WEBHOOK_URL", "https://t.example")
    monkeypatch.setattr(config, "TELEGRAM_WEBHOOK_PORT", 8080)
    monkeypatch.setattr(config, "CLOUDFLARED_TUNNEL_TOKEN", "tok")
    assert config.cloudflared_enabled() is True


def test_webhook_port_parses(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_PORT", "9000")
    assert config._parse_webhook_port() == 9000
    monkeypatch.delenv("TELEGRAM_WEBHOOK_PORT", raising=False)
    assert config._parse_webhook_port() == 8080


def test_webhook_port_rejects_invalid(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_PORT", "nope")
    with pytest.raises(RuntimeError):
        config._parse_webhook_port()
    monkeypatch.setenv("TELEGRAM_WEBHOOK_PORT", "0")
    with pytest.raises(RuntimeError):
        config._parse_webhook_port()


def test_opencode_server_credentials_none_without_password(monkeypatch):
    monkeypatch.setattr(config, "OPENCODE_SERVER_PASSWORD", "")
    assert config.opencode_server_credentials() is None


def test_opencode_server_credentials_defaults_username(monkeypatch):
    monkeypatch.setattr(config, "OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setattr(config, "OPENCODE_SERVER_USERNAME", "")
    assert config.opencode_server_credentials() == ("opencode", "pw")


def test_opencode_server_credentials_uses_configured_username(monkeypatch):
    monkeypatch.setattr(config, "OPENCODE_SERVER_PASSWORD", "pw")
    monkeypatch.setattr(config, "OPENCODE_SERVER_USERNAME", "alice")
    assert config.opencode_server_credentials() == ("alice", "pw")


def test_media_max_mb_default(monkeypatch):
    monkeypatch.delenv("MEDIA_MAX_MB", raising=False)
    assert config._parse_media_max_mb() == 20
    assert config.MEDIA_MAX_MB == int(config.MEDIA_MAX_MB) > 0


def test_media_max_mb_parses_custom_value(monkeypatch):
    monkeypatch.setenv("MEDIA_MAX_MB", "50")
    assert config._parse_media_max_mb() == 50


def test_media_max_mb_rejects_non_numeric(monkeypatch):
    monkeypatch.setenv("MEDIA_MAX_MB", "lots")
    with pytest.raises(RuntimeError):
        config._parse_media_max_mb()


def test_media_max_mb_rejects_non_positive(monkeypatch):
    monkeypatch.setenv("MEDIA_MAX_MB", "0")
    with pytest.raises(RuntimeError):
        config._parse_media_max_mb()
