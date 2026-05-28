"""
db.py — создание и управление SQLite соединением.
Все остальные модули получают соединение через get_connection().
"""

import asyncio
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

_connection: sqlite3.Connection | None = None


def get_connection() -> sqlite3.Connection:
    """Вернуть текущее соединение. Вызывать только после init()."""
    if _connection is None:
        raise RuntimeError("БД не инициализирована. Вызовите db.init() сначала.")
    return _connection


async def init(db_path: str) -> sqlite3.Connection:
    """
    Инициализировать БД: создать файл, таблицы, включить WAL-режим.
    Возвращает соединение.
    """
    global _connection

    path = Path(db_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    logger.info("Открываем БД: %s", path)

    conn = await asyncio.to_thread(_create_connection, str(path))
    _connection = conn
    logger.info("БД готова")
    return conn


def _create_connection(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row

    # WAL-режим: параллельные читатели не блокируют писателей
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")

    _create_tables(conn)
    conn.commit()
    return conn


def _create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        -- Снапшоты контекста от коллекторов
        CREATE TABLE IF NOT EXISTS context_snapshots (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            source  TEXT NOT NULL,
            data    TEXT NOT NULL,
            ts      TEXT NOT NULL DEFAULT (datetime('now')),
            hash    TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ctx ON context_snapshots(source, ts);

        -- Задачи (синхронизируются с Obsidian)
        CREATE TABLE IF NOT EXISTS tasks (
            id          TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            priority    TEXT NOT NULL CHECK(priority IN ('HIGH','MED','LOW')),
            project     TEXT,
            description TEXT,
            done        INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            done_at     TEXT,
            date        TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_date ON tasks(date);

        -- Ошибки из PyCharm
        CREATE TABLE IF NOT EXISTS errors (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            error_type  TEXT NOT NULL,
            message     TEXT,
            file        TEXT,
            line        INTEGER,
            project     TEXT,
            source      TEXT NOT NULL CHECK(source IN ('runtime','static')),
            ts          TEXT NOT NULL DEFAULT (datetime('now')),
            hash        TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_err ON errors(error_type, file, ts);

        -- Сессии продуктивности
        CREATE TABLE IF NOT EXISTS productivity_sessions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT NOT NULL,
            start_ts        TEXT,
            end_ts          TEXT,
            duration_min    REAL,
            deep_work_min   REAL,
            switches_count  INTEGER,
            app_breakdown   TEXT
        );

        -- История запусков LLM
        CREATE TABLE IF NOT EXISTS llm_runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            trigger     TEXT NOT NULL,
            model       TEXT,
            duration_s  REAL,
            tokens      INTEGER,
            task_count  INTEGER,
            ts          TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    logger.debug("Таблицы созданы/проверены")


async def close() -> None:
    """Закрыть соединение с БД."""
    global _connection
    if _connection:
        await asyncio.to_thread(_connection.close)
        _connection = None
        logger.info("БД закрыта")
