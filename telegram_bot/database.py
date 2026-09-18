from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Optional

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
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
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS opencode_sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL UNIQUE REFERENCES users(id),
    session_id TEXT NOT NULL,
    directory  TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id),
    telegram_id    INTEGER NOT NULL,
    message_id     INTEGER,
    media_type     TEXT NOT NULL,
    file_id        TEXT NOT NULL,
    file_unique_id TEXT NOT NULL UNIQUE,
    file_name      TEXT,
    mime_type      TEXT,
    file_size      INTEGER,
    local_path     TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_media_user_id ON media(user_id);
CREATE INDEX IF NOT EXISTS idx_media_file_unique_id ON media(file_unique_id);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(config.DB_PATH))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _migrate(connection: sqlite3.Connection) -> None:
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(users)").fetchall()
    }
    additions = {
        "voice_mode": "INTEGER NOT NULL DEFAULT 0",
        "workdir": "TEXT",
        "model_provider": "TEXT",
        "model_id": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")


def init_db() -> None:
    with closing(_connect()) as connection, connection:
        connection.executescript(SCHEMA)
        _migrate(connection)


def upsert_user(
    telegram_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    language_code: Optional[str] = None,
) -> sqlite3.Row:
    now = _utcnow()
    with closing(_connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO users (
                telegram_id, username, first_name, last_name, language_code,
                is_authenticated, role, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, 'user', ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                language_code = excluded.language_code,
                updated_at = excluded.updated_at
            """,
            (telegram_id, username, first_name, last_name, language_code, now, now),
        )
        row = connection.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    assert row is not None
    return row


def seed_admin(
    telegram_id: int,
    first_name: str = "Admin",
    last_name: str = "",
    language_code: str = "en",
) -> None:
    now = _utcnow()
    with closing(_connect()) as connection, connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO users (
                telegram_id, username, first_name, last_name, language_code,
                is_authenticated, role, created_at, updated_at
            ) VALUES (?, NULL, ?, ?, ?, 1, 'admin', ?, ?)
            """,
            (telegram_id, first_name, last_name, language_code, now, now),
        )
        connection.execute(
            """
            UPDATE users
            SET is_authenticated = 1, role = 'admin', updated_at = ?
            WHERE telegram_id = ?
            """,
            (now, telegram_id),
        )


def get_user_by_telegram_id(telegram_id: int) -> Optional[sqlite3.Row]:
    with closing(_connect()) as connection:
        return connection.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()


def get_user_by_id(user_id: int) -> Optional[sqlite3.Row]:
    with closing(_connect()) as connection:
        return connection.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()


def set_authenticated(telegram_id: int, value: bool) -> bool:
    with closing(_connect()) as connection, connection:
        cursor = connection.execute(
            "UPDATE users SET is_authenticated = ?, updated_at = ? WHERE telegram_id = ?",
            (1 if value else 0, _utcnow(), telegram_id),
        )
    return cursor.rowcount > 0


def is_authenticated(telegram_id: int) -> bool:
    row = get_user_by_telegram_id(telegram_id)
    return bool(row["is_authenticated"]) if row is not None else False


def get_voice_mode(telegram_id: int) -> bool:
    with closing(_connect()) as connection:
        row = connection.execute(
            "SELECT voice_mode FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    return bool(row["voice_mode"]) if row is not None else False


def set_voice_mode(telegram_id: int, enabled: bool) -> bool:
    with closing(_connect()) as connection, connection:
        cursor = connection.execute(
            "UPDATE users SET voice_mode = ?, updated_at = ? WHERE telegram_id = ?",
            (1 if enabled else 0, _utcnow(), telegram_id),
        )
    return cursor.rowcount > 0


def get_workdir(telegram_id: int) -> Optional[str]:
    with closing(_connect()) as connection:
        row = connection.execute(
            "SELECT workdir FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    if row is None or not row["workdir"]:
        return None
    return str(row["workdir"])


def set_workdir(telegram_id: int, directory: str) -> None:
    with closing(_connect()) as connection, connection:
        connection.execute(
            "UPDATE users SET workdir = ?, updated_at = ? WHERE telegram_id = ?",
            (directory, _utcnow(), telegram_id),
        )


def get_model(telegram_id: int) -> Optional[tuple[str, str]]:
    with closing(_connect()) as connection:
        row = connection.execute(
            "SELECT model_provider, model_id FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
    if row is None or not row["model_provider"] or not row["model_id"]:
        return None
    return str(row["model_provider"]), str(row["model_id"])


def set_model(telegram_id: int, provider_id: str, model_id: str) -> None:
    with closing(_connect()) as connection, connection:
        connection.execute(
            """
            UPDATE users
            SET model_provider = ?, model_id = ?, updated_at = ?
            WHERE telegram_id = ?
            """,
            (provider_id, model_id, _utcnow(), telegram_id),
        )


def get_opencode_session(user_id: int) -> Optional[sqlite3.Row]:
    with closing(_connect()) as connection:
        return connection.execute(
            "SELECT * FROM opencode_sessions WHERE user_id = ?", (user_id,)
        ).fetchone()


def set_opencode_session(user_id: int, session_id: str, directory: str) -> None:
    now = _utcnow()
    with closing(_connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO opencode_sessions (
                user_id, session_id, directory, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                session_id = excluded.session_id,
                directory = excluded.directory,
                updated_at = excluded.updated_at
            """,
            (user_id, session_id, directory, now, now),
        )


def delete_opencode_session(user_id: int) -> bool:
    with closing(_connect()) as connection, connection:
        cursor = connection.execute(
            "DELETE FROM opencode_sessions WHERE user_id = ?", (user_id,)
        )
    return cursor.rowcount > 0


def list_opencode_sessions() -> list[sqlite3.Row]:
    with closing(_connect()) as connection:
        rows = connection.execute(
            """
            SELECT
                opencode_sessions.session_id AS session_id,
                opencode_sessions.directory AS directory,
                users.id AS id,
                users.telegram_id AS telegram_id,
                users.is_authenticated AS is_authenticated
            FROM opencode_sessions
            JOIN users ON users.id = opencode_sessions.user_id
            ORDER BY opencode_sessions.updated_at DESC
            """
        ).fetchall()
    return list(rows)


def get_user_by_session_id(session_id: str) -> Optional[sqlite3.Row]:
    with closing(_connect()) as connection:
        return connection.execute(
            """
            SELECT users.*
            FROM users
            JOIN opencode_sessions ON opencode_sessions.user_id = users.id
            WHERE opencode_sessions.session_id = ?
            """,
            (session_id,),
        ).fetchone()


def insert_media(
    user_id: int,
    telegram_id: int,
    message_id: Optional[int],
    media_type: str,
    file_id: str,
    file_unique_id: str,
    local_path: str,
    file_name: Optional[str] = None,
    mime_type: Optional[str] = None,
    file_size: Optional[int] = None,
) -> int:
    with closing(_connect()) as connection, connection:
        existing = connection.execute(
            "SELECT id FROM media WHERE file_unique_id = ?", (file_unique_id,)
        ).fetchone()
        if existing is not None:
            return int(existing["id"])
        cursor = connection.execute(
            """
            INSERT INTO media (
                user_id, telegram_id, message_id, media_type, file_id,
                file_unique_id, file_name, mime_type, file_size, local_path, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                telegram_id,
                message_id,
                media_type,
                file_id,
                file_unique_id,
                file_name,
                mime_type,
                file_size,
                local_path,
                _utcnow(),
            ),
        )
        return int(cursor.lastrowid)


def list_media(user_id: Optional[int] = None, limit: int = 50) -> list[sqlite3.Row]:
    with closing(_connect()) as connection:
        if user_id is None:
            rows = connection.execute(
                "SELECT * FROM media ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM media WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
    return list(rows)


def get_media(media_id: int) -> Optional[sqlite3.Row]:
    with closing(_connect()) as connection:
        return connection.execute(
            "SELECT * FROM media WHERE id = ?", (media_id,)
        ).fetchone()
