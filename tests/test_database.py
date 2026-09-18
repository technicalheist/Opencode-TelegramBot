from __future__ import annotations

import sqlite3

import pytest

import config
from telegram_bot import database


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "data" / "bot.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")
    database.init_db()
    return path


def _table_names(path) -> set[str]:
    connection = sqlite3.connect(str(path))
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    finally:
        connection.close()
    return {row[0] for row in rows}


def _user_count(path) -> int:
    connection = sqlite3.connect(str(path))
    try:
        return connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        connection.close()


def test_init_db_creates_tables(db_path):
    names = _table_names(db_path)
    assert "users" in names
    assert "media" in names
    assert "opencode_sessions" in names


def test_init_db_adds_voice_mode_to_legacy_db(tmp_path, monkeypatch):
    path = tmp_path / "data" / "bot.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")

    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(
            """
            CREATE TABLE users (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id      INTEGER NOT NULL UNIQUE,
                username         TEXT,
                first_name       TEXT,
                last_name        TEXT,
                language_code    TEXT,
                is_authenticated INTEGER NOT NULL DEFAULT 0,
                role             TEXT NOT NULL DEFAULT 'user',
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL
            );
            INSERT INTO users (
                telegram_id, first_name, is_authenticated, role, created_at, updated_at
            ) VALUES (5, 'Legacy', 1, 'admin', '2020-01-01T00:00:00+00:00',
                      '2020-01-01T00:00:00+00:00');
            """
        )
        connection.commit()
    finally:
        connection.close()

    database.init_db()

    connection = sqlite3.connect(str(path))
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
        row = connection.execute(
            "SELECT voice_mode, first_name FROM users WHERE telegram_id = 5"
        ).fetchone()
    finally:
        connection.close()

    assert "voice_mode" in columns
    assert row[0] == 0
    assert row[1] == "Legacy"


def test_init_db_adds_workdir_and_model_columns_to_legacy_db(tmp_path, monkeypatch):
    path = tmp_path / "data" / "bot.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")

    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(
            """
            CREATE TABLE users (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id      INTEGER NOT NULL UNIQUE,
                username         TEXT,
                first_name       TEXT,
                last_name        TEXT,
                language_code    TEXT,
                is_authenticated INTEGER NOT NULL DEFAULT 0,
                role             TEXT NOT NULL DEFAULT 'user',
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL
            );
            INSERT INTO users (
                telegram_id, first_name, is_authenticated, role, created_at, updated_at
            ) VALUES (9, 'Legacy', 1, 'admin', '2020-01-01T00:00:00+00:00',
                      '2020-01-01T00:00:00+00:00');
            """
        )
        connection.commit()
    finally:
        connection.close()

    database.init_db()

    connection = sqlite3.connect(str(path))
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
        row = connection.execute(
            "SELECT voice_mode, workdir, model_provider, model_id, first_name "
            "FROM users WHERE telegram_id = 9"
        ).fetchone()
        count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        connection.close()

    assert {"voice_mode", "workdir", "model_provider", "model_id"} <= columns
    assert row[0] == 0
    assert row[1] is None
    assert row[2] is None
    assert row[3] is None
    assert row[4] == "Legacy"
    assert count == 1


def test_workdir_get_set_round_trip(db_path):
    database.upsert_user(88, "gina", "Gina", "G", "en")

    assert database.get_workdir(88) is None

    database.set_workdir(88, "D:/projects/demo")
    assert database.get_workdir(88) == "D:/projects/demo"

    database.set_workdir(88, "D:/projects/other")
    assert database.get_workdir(88) == "D:/projects/other"

    assert database.get_workdir(999) is None


def test_model_get_set_round_trip(db_path):
    database.upsert_user(89, "hank", "Hank", "H", "en")

    assert database.get_model(89) is None

    database.set_model(89, "opencode-go", "deepseek-v4.1-flash")
    assert database.get_model(89) == ("opencode-go", "deepseek-v4.1-flash")

    database.set_model(89, "opencode", "gpt-5")
    assert database.get_model(89) == ("opencode", "gpt-5")

    assert database.get_model(999) is None


def test_workdir_and_model_preserved_by_upsert(db_path):
    database.upsert_user(90, "ivy", "Ivy", "I", "en")
    database.set_workdir(90, "D:/projects/demo")
    database.set_model(90, "opencode-go", "deepseek-v4.1-flash")

    updated = database.upsert_user(90, "ivy2", "Ivy", "J", "fr")

    assert updated["workdir"] == "D:/projects/demo"
    assert updated["model_provider"] == "opencode-go"
    assert updated["model_id"] == "deepseek-v4.1-flash"
    assert database.get_workdir(90) == "D:/projects/demo"
    assert database.get_model(90) == ("opencode-go", "deepseek-v4.1-flash")


def test_voice_mode_default_toggle_and_preserved_by_upsert(db_path):
    database.upsert_user(55, "eve", "Eve", "E", "en")
    assert database.get_voice_mode(55) is False

    assert database.set_voice_mode(55, True) is True
    assert database.get_voice_mode(55) is True

    updated = database.upsert_user(55, "eve2", "Eve", "F", "de")
    assert updated["voice_mode"] == 1
    assert database.get_voice_mode(55) is True

    assert database.set_voice_mode(55, False) is True
    assert database.get_voice_mode(55) is False
    assert database.set_voice_mode(999, True) is False
    assert database.get_voice_mode(999) is False


def test_is_authenticated(db_path):
    database.upsert_user(77, "frank", "Frank", "F", "en")
    assert database.is_authenticated(77) is False

    assert database.set_authenticated(77, True) is True
    assert database.is_authenticated(77) is True

    assert database.set_authenticated(77, False) is True
    assert database.is_authenticated(77) is False
    assert database.is_authenticated(999) is False


def test_opencode_session_upsert_replace_delete_and_lookup(db_path):
    user = database.upsert_user(321, "dave", "Dave", "D", "en")
    user_id = int(user["id"])

    assert database.get_opencode_session(user_id) is None

    database.set_opencode_session(user_id, "ses_1", "D:/repo")
    row = database.get_opencode_session(user_id)
    assert row is not None
    assert row["session_id"] == "ses_1"
    assert row["directory"] == "D:/repo"
    assert row["created_at"]
    assert row["updated_at"]

    database.set_opencode_session(user_id, "ses_2", "D:/other")
    replaced = database.get_opencode_session(user_id)
    assert replaced["session_id"] == "ses_2"
    assert replaced["directory"] == "D:/other"

    found = database.get_user_by_session_id("ses_2")
    assert found is not None
    assert found["telegram_id"] == 321
    assert database.get_user_by_session_id("missing") is None

    assert database.delete_opencode_session(user_id) is True
    assert database.get_opencode_session(user_id) is None
    assert database.get_user_by_session_id("ses_2") is None
    assert database.delete_opencode_session(user_id) is False


def test_init_db_is_idempotent(db_path):
    database.init_db()
    assert "users" in _table_names(db_path)


def test_upsert_user_inserts_defaults(db_path):
    row = database.upsert_user(111, "alice", "Alice", "A", "en")
    assert row["telegram_id"] == 111
    assert row["username"] == "alice"
    assert row["first_name"] == "Alice"
    assert row["is_authenticated"] == 0
    assert row["role"] == "user"
    assert row["created_at"]
    assert row["updated_at"]


def test_upsert_user_updates_without_clobbering_auth(db_path):
    first = database.upsert_user(111, "alice", "Alice", "A", "en")
    assert database.set_authenticated(111, True) is True

    second = database.upsert_user(111, "alice2", "Alice", "B", "fr")
    assert second["id"] == first["id"]
    assert second["username"] == "alice2"
    assert second["last_name"] == "B"
    assert second["language_code"] == "fr"
    assert second["is_authenticated"] == 1
    assert second["created_at"] == first["created_at"]


def test_set_authenticated_missing_user_returns_false(db_path):
    assert database.set_authenticated(999, True) is False


def test_seed_admin_sets_role_and_auth_idempotently(db_path):
    database.seed_admin(123456789)
    database.seed_admin(123456789)

    row = database.get_user_by_telegram_id(123456789)
    assert row is not None
    assert row["is_authenticated"] == 1
    assert row["role"] == "admin"
    assert row["first_name"] == "Admin"
    assert _user_count(db_path) == 1


def test_seed_admin_promotes_existing_user(db_path):
    database.upsert_user(123456789, "admin", "Admin", "A", "en")
    database.seed_admin(123456789)
    row = database.get_user_by_telegram_id(123456789)
    assert row["is_authenticated"] == 1
    assert row["role"] == "admin"


def test_media_insert_list_get_and_dedupe(db_path):
    user = database.upsert_user(42, "bob", "Bob", "B", "en")

    media_id = database.insert_media(
        user_id=user["id"],
        telegram_id=42,
        message_id=5,
        media_type="photo",
        file_id="file-1",
        file_unique_id="uniq-1",
        local_path="42/uniq-1.jpg",
        file_size=123,
    )
    assert media_id > 0

    duplicate_id = database.insert_media(
        user_id=user["id"],
        telegram_id=42,
        message_id=6,
        media_type="photo",
        file_id="file-1b",
        file_unique_id="uniq-1",
        local_path="42/uniq-1.jpg",
    )
    assert duplicate_id == media_id

    rows = database.list_media(user_id=user["id"])
    assert len(rows) == 1
    assert rows[0]["file_unique_id"] == "uniq-1"

    fetched = database.get_media(media_id)
    assert fetched is not None
    assert fetched["local_path"] == "42/uniq-1.jpg"
    assert fetched["file_size"] == 123
    assert database.get_media(9999) is None


def test_list_media_orders_newest_first_and_respects_limit(db_path):
    user = database.upsert_user(7, "carol", "Carol", "C", "en")
    for index in range(3):
        database.insert_media(
            user_id=user["id"],
            telegram_id=7,
            message_id=index,
            media_type="voice",
            file_id=f"f{index}",
            file_unique_id=f"u{index}",
            local_path=f"7/u{index}.ogg",
        )

    rows = database.list_media(user_id=user["id"], limit=2)
    assert [row["file_unique_id"] for row in rows] == ["u2", "u1"]

    all_rows = database.list_media(limit=10)
    assert len(all_rows) == 3
