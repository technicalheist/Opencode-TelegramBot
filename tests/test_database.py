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
    assert "user_workdirs" in names
    assert "user_models" in names


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


def test_default_server_id_returns_lowest(db_path):
    assert database.default_server_id() is None

    first = database.add_server("A", "http://a:1")
    second = database.add_server("B", "http://b:2")

    assert database.default_server_id() == int(first["id"])
    assert int(first["id"]) < int(second["id"])


def test_workdir_is_per_server(db_path):
    first = database.add_server("A", "http://a:1")
    second = database.add_server("B", "http://b:2")
    database.upsert_user(88, "gina", "Gina", "G", "en")

    assert database.get_workdir(88, int(first["id"])) is None
    assert database.get_workdir(88, int(second["id"])) is None

    database.set_workdir(88, "/server/a", int(first["id"]))
    database.set_workdir(88, "/server/b", int(second["id"]))

    assert database.get_workdir(88, int(first["id"])) == "/server/a"
    assert database.get_workdir(88, int(second["id"])) == "/server/b"

    database.set_workdir(88, "/server/a2", int(first["id"]))
    assert database.get_workdir(88, int(first["id"])) == "/server/a2"
    assert database.get_workdir(88, int(second["id"])) == "/server/b"


def test_workdir_default_server_falls_back_to_legacy(db_path):
    first = database.add_server("A", "http://a:1")
    second = database.add_server("B", "http://b:2")
    database.upsert_user(88, "gina", "Gina", "G", "en")
    database.set_workdir(88, "D:/legacy")

    assert database.get_workdir(88, int(first["id"])) == "D:/legacy"
    assert database.get_workdir(88, int(second["id"])) is None


def test_set_workdir_default_server_syncs_legacy(db_path):
    first = database.add_server("A", "http://a:1")
    second = database.add_server("B", "http://b:2")
    database.upsert_user(88, "gina", "Gina", "G", "en")

    database.set_workdir(88, "/first", int(first["id"]))
    assert database.get_workdir(88) == "/first"

    database.set_workdir(88, "/second", int(second["id"]))
    assert database.get_workdir(88) == "/first"
    assert database.get_workdir(88, int(second["id"])) == "/second"


def test_model_get_set_round_trip(db_path):
    database.upsert_user(89, "hank", "Hank", "H", "en")

    assert database.get_model(89) is None

    database.set_model(89, "opencode-go", "deepseek-v4.1-flash")
    assert database.get_model(89) == ("opencode-go", "deepseek-v4.1-flash")

    database.set_model(89, "opencode", "gpt-5")
    assert database.get_model(89) == ("opencode", "gpt-5")

    assert database.get_model(999) is None


def test_model_is_per_server(db_path):
    first = database.add_server("A", "http://a:1")
    second = database.add_server("B", "http://b:2")
    database.upsert_user(91, "jane", "Jane", "J", "en")

    assert database.get_model(91, int(first["id"])) is None
    assert database.get_model(91, int(second["id"])) is None

    database.set_model(91, "openrouter", "remote-only", int(first["id"]))
    database.set_model(91, "opencode", "local-only", int(second["id"]))

    assert database.get_model(91, int(first["id"])) == ("openrouter", "remote-only")
    assert database.get_model(91, int(second["id"])) == ("opencode", "local-only")

    database.set_model(91, "openrouter", "remote-only-2", int(first["id"]))
    assert database.get_model(91, int(first["id"])) == (
        "openrouter",
        "remote-only-2",
    )
    assert database.get_model(91, int(second["id"])) == ("opencode", "local-only")


def test_model_default_server_falls_back_to_legacy(db_path):
    first = database.add_server("A", "http://a:1")
    second = database.add_server("B", "http://b:2")
    database.upsert_user(92, "kate", "Kate", "K", "en")
    database.set_model(92, "opencode-go", "legacy-model")

    assert database.get_model(92, int(first["id"])) == (
        "opencode-go",
        "legacy-model",
    )
    assert database.get_model(92, int(second["id"])) is None


def test_set_model_default_server_syncs_legacy(db_path):
    first = database.add_server("A", "http://a:1")
    second = database.add_server("B", "http://b:2")
    database.upsert_user(93, "liam", "Liam", "L", "en")

    database.set_model(93, "openrouter", "remote-only", int(first["id"]))
    assert database.get_model(93) == ("openrouter", "remote-only")

    database.set_model(93, "opencode", "local-only", int(second["id"]))
    assert database.get_model(93) == ("openrouter", "remote-only")
    assert database.get_model(93, int(second["id"])) == ("opencode", "local-only")


def test_model_legacy_accessors_unchanged(db_path):
    database.upsert_user(94, "mona", "Mona", "M", "en")

    assert database.get_model(94) is None

    database.set_model(94, "opencode-go", "deepseek-v4.1-flash")
    assert database.get_model(94) == ("opencode-go", "deepseek-v4.1-flash")

    database.set_model(94, "opencode", "gpt-5")
    assert database.get_model(94) == ("opencode", "gpt-5")

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

    database.set_opencode_session(user_id, "ses_1", "D:/repo", None)
    row = database.get_opencode_session(user_id)
    assert row is not None
    assert row["session_id"] == "ses_1"
    assert row["directory"] == "D:/repo"
    assert row["created_at"]
    assert row["updated_at"]

    database.set_opencode_session(user_id, "ses_2", "D:/other", None)
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


def test_init_db_adds_approval_column_to_legacy_db(tmp_path, monkeypatch):
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
            ) VALUES (12, 'Legacy', 0, 'user', '2020-01-01T00:00:00+00:00',
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
            "SELECT approval_requested_at, first_name FROM users WHERE telegram_id = 12"
        ).fetchone()
    finally:
        connection.close()

    assert "approval_requested_at" in columns
    assert row[0] is None
    assert row[1] == "Legacy"


def test_approval_requested_round_trip(db_path):
    database.upsert_user(66, "nina", "Nina", "N", "en")

    assert database.has_pending_approval(66) is False

    database.set_approval_requested(66, True)
    assert database.has_pending_approval(66) is True

    database.set_approval_requested(66, False)
    assert database.has_pending_approval(66) is False

    assert database.has_pending_approval(999) is False


def test_pending_approval_cleared_by_authentication(db_path):
    database.upsert_user(67, "omar", "Omar", "O", "en")
    database.set_approval_requested(67, True)
    assert database.has_pending_approval(67) is True

    database.set_authenticated(67, True)
    assert database.has_pending_approval(67) is False

    database.set_authenticated(67, False)
    database.set_approval_requested(67, True)
    assert database.has_pending_approval(67) is True


def test_approval_requested_preserved_by_upsert(db_path):
    database.upsert_user(68, "pam", "Pam", "P", "en")
    database.set_approval_requested(68, True)

    updated = database.upsert_user(68, "pam2", "Pam", "Q", "fr")

    assert updated["approval_requested_at"] is not None
    assert database.has_pending_approval(68) is True


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


def test_ensure_default_server_is_idempotent(db_path):
    first = database.ensure_default_server("http://localhost:4096")
    second = database.ensure_default_server("http://localhost:4096", label="Other")

    assert first["id"] == second["id"]
    assert second["label"] == first["label"]
    assert len(database.list_servers()) == 1


def test_add_server_duplicate_returns_none(db_path):
    created = database.add_server("Local", "http://localhost:4096")
    assert created is not None
    assert created["label"] == "Local"
    assert created["base_url"] == "http://localhost:4096"
    assert database.add_server("Dup", "http://localhost:4096") is None


def test_add_server_duplicate_is_atomic(db_path):
    first = database.add_server("Local", "http://localhost:4096")
    second = database.add_server("Other", "http://localhost:4096")

    assert first is not None
    assert second is None
    assert len(database.list_servers()) == 1
    assert database.list_servers()[0]["label"] == "Local"


def test_list_get_and_get_by_url(db_path):
    first = database.add_server("A", "http://a:1/")
    second = database.add_server("B", "http://b:2")

    assert [row["id"] for row in database.list_servers()] == [
        first["id"],
        second["id"],
    ]
    assert database.get_server(first["id"])["label"] == "A"
    assert database.get_server(999) is None
    assert database.get_server_by_url("http://a:1")["id"] == first["id"]
    assert database.get_server_by_url("http://a:1/")["id"] == first["id"]
    assert database.get_server_by_url("http://missing") is None


def test_delete_server_detaches_users_and_sessions(db_path):
    first = database.add_server("A", "http://a:1")
    second = database.add_server("B", "http://b:2")
    user = database.upsert_user(500, "s", "S", "T", "en")
    user_id = int(user["id"])
    database.set_user_server(500, int(second["id"]))
    database.set_opencode_session(user_id, "ses_1", "D:/repo", int(second["id"]))

    assert database.delete_server(int(second["id"])) is True

    assert database.get_server(int(second["id"])) is None
    assert database.get_opencode_session(user_id) is None
    refreshed = database.get_user_by_telegram_id(500)
    assert refreshed["server_id"] is None
    assert database.get_user_server(500)["id"] == first["id"]


def test_delete_server_removes_detached_users_legacy_session(db_path):
    first = database.add_server("A", "http://a:1")
    second = database.add_server("B", "http://b:2")
    user = database.upsert_user(700, "u", "U", "V", "en")
    user_id = int(user["id"])
    database.set_user_server(700, int(second["id"]))
    database.set_opencode_session(user_id, "ses_legacy", "D:/repo", None)

    assert database.delete_server(int(second["id"])) is True

    assert database.get_opencode_session(user_id) is None
    assert database.get_user_by_telegram_id(700)["server_id"] is None
    assert database.get_user_server(700)["id"] == first["id"]


def test_delete_server_refuses_last(db_path):
    only = database.add_server("Only", "http://only:1")

    assert database.delete_server(int(only["id"])) is False
    assert database.get_server(int(only["id"])) is not None
    assert database.delete_server(999) is False


def test_get_user_server_default_fallback_and_selection(db_path):
    first = database.add_server("A", "http://a:1")
    second = database.add_server("B", "http://b:2")
    database.upsert_user(600, "u", "U", "V", "en")

    assert database.get_user_server(600)["id"] == first["id"]

    assert database.set_user_server(600, int(second["id"])) is True
    assert database.get_user_server(600)["id"] == second["id"]

    assert database.get_user_server(999)["id"] == first["id"]
    assert database.set_user_server(999, int(first["id"])) is False


def test_set_opencode_session_persists_server_and_list_exposes_base_url(db_path):
    server = database.add_server("A", "http://a:1")
    user = database.upsert_user(601, "u", "U", "V", "en")
    user_id = int(user["id"])
    database.set_opencode_session(user_id, "ses_1", "D:/repo", int(server["id"]))

    row = database.get_opencode_session(user_id)
    assert row["server_id"] == server["id"]

    rows = database.list_opencode_sessions()
    match = next(item for item in rows if item["session_id"] == "ses_1")
    assert match["base_url"] == "http://a:1"
    assert match["id"] == user_id
    assert match["telegram_id"] == 601
    assert match["is_authenticated"] == 0


def test_init_db_adds_server_columns_to_legacy_schema(tmp_path, monkeypatch):
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
                voice_mode       INTEGER NOT NULL DEFAULT 0,
                workdir          TEXT,
                model_provider   TEXT,
                model_id         TEXT,
                approval_requested_at TEXT,
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL
            );
            CREATE TABLE opencode_sessions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL UNIQUE REFERENCES users(id),
                session_id TEXT NOT NULL,
                directory  TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        connection.commit()
    finally:
        connection.close()

    database.init_db()

    connection = sqlite3.connect(str(path))
    try:
        user_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(users)")
        }
        session_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(opencode_sessions)")
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        connection.close()

    assert "server_id" in user_columns
    assert "server_id" in session_columns
    assert "servers" in tables


def test_init_db_adds_server_credentials_to_legacy_schema(tmp_path, monkeypatch):
    path = tmp_path / "data" / "bot.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")

    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(
            """
            CREATE TABLE servers (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                label      TEXT NOT NULL,
                base_url   TEXT NOT NULL UNIQUE,
                created_by INTEGER,
                created_at TEXT NOT NULL
            );
            INSERT INTO servers (label, base_url, created_at)
            VALUES ('Legacy', 'http://legacy:1', '2020-01-01T00:00:00+00:00');
            """
        )
        connection.commit()
    finally:
        connection.close()

    database.init_db()

    connection = sqlite3.connect(str(path))
    try:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(servers)")
        }
        row = connection.execute(
            "SELECT username, password, label FROM servers WHERE base_url = 'http://legacy:1'"
        ).fetchone()
    finally:
        connection.close()

    assert {"username", "password"} <= columns
    assert row[0] is None
    assert row[1] is None
    assert row[2] == "Legacy"


def test_add_server_credentials_round_trip(db_path):
    created = database.add_server(
        "Basic", "http://basic:1", username="alice", password="s3cret"
    )
    assert created is not None
    assert created["username"] == "alice"
    assert created["password"] == "s3cret"

    fetched = database.get_server(int(created["id"]))
    assert fetched["username"] == "alice"
    assert fetched["password"] == "s3cret"
    assert database.get_server_by_url("http://basic:1")["username"] == "alice"
    assert any(
        row["password"] == "s3cret" for row in database.list_servers()
    )

    plain = database.add_server("Plain", "http://plain:1")
    assert plain["username"] is None
    assert plain["password"] is None


def test_ensure_default_server_has_no_credentials(db_path):
    row = database.ensure_default_server("http://localhost:4096")
    assert row["username"] is None
    assert row["password"] is None
