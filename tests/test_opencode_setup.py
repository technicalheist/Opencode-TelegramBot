from __future__ import annotations

import json
import sys

import pytest

from telegram_bot import opencode_setup


@pytest.fixture
def setup_paths(monkeypatch, tmp_path):
    base = tmp_path / ".config" / "opencode"
    skill = base / "skills" / "telegram-media" / "SKILL.md"
    monkeypatch.setattr(opencode_setup, "GLOBAL_CONFIG_DIR", base)
    monkeypatch.setattr(opencode_setup, "GLOBAL_CONFIG", base / "opencode.json")
    monkeypatch.setattr(opencode_setup, "GLOBAL_SKILL_DIR", skill.parent)
    monkeypatch.setattr(opencode_setup, "GLOBAL_SKILL", skill)
    return base


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_install_mcp_config_creates_file(setup_paths):
    result = opencode_setup.install_mcp_config()

    assert result is True
    config = _load(opencode_setup.GLOBAL_CONFIG)
    assert config["$schema"] == opencode_setup.CONFIG_SCHEMA
    assert config["mcp"]["telegram"]["type"] == "local"
    assert config["mcp"]["telegram"]["enabled"] is True
    command = config["mcp"]["telegram"]["command"]
    assert command[0] == sys.executable
    assert command[1].endswith("mcp_server.py")


def test_install_mcp_config_preserves_existing_keys(setup_paths):
    opencode_setup.GLOBAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    opencode_setup.GLOBAL_CONFIG.write_text(
        json.dumps({"theme": "dark", "mcp": {"other": {"type": "local"}}}),
        encoding="utf-8",
    )

    result = opencode_setup.install_mcp_config()

    assert result is True
    config = _load(opencode_setup.GLOBAL_CONFIG)
    assert config["theme"] == "dark"
    assert config["mcp"]["other"] == {"type": "local"}
    assert "telegram" in config["mcp"]
    assert config["$schema"] == opencode_setup.CONFIG_SCHEMA


def test_install_mcp_config_skips_when_present(setup_paths):
    opencode_setup.GLOBAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    existing = {"mcp": {"telegram": {"type": "local", "command": ["x"]}}}
    opencode_setup.GLOBAL_CONFIG.write_text(json.dumps(existing), encoding="utf-8")
    before = opencode_setup.GLOBAL_CONFIG.read_text(encoding="utf-8")

    result = opencode_setup.install_mcp_config()

    assert result is False
    assert opencode_setup.GLOBAL_CONFIG.read_text(encoding="utf-8") == before


def test_install_mcp_config_leaves_invalid_json_untouched(setup_paths):
    opencode_setup.GLOBAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    opencode_setup.GLOBAL_CONFIG.write_text("{not valid json", encoding="utf-8")

    result = opencode_setup.install_mcp_config()

    assert result is False
    assert opencode_setup.GLOBAL_CONFIG.read_text(encoding="utf-8") == "{not valid json"


def test_install_skill_writes_frontmatter_and_skips(setup_paths):
    result = opencode_setup.install_skill()

    assert result is True
    content = opencode_setup.GLOBAL_SKILL.read_text(encoding="utf-8")
    assert content.startswith("---\nname: telegram-media\n")
    assert "telegram_send_file" in content
    assert "send_media.py" in content

    assert opencode_setup.install_skill() is False


def test_install_all_reports_both(setup_paths):
    first = opencode_setup.install_all()
    assert first == {"mcp": True, "skill": True}

    second = opencode_setup.install_all()
    assert second == {"mcp": False, "skill": False}
