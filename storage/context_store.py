"""
context_store.py — все операции с данными.
Единая точка доступа к SQLite для всех модулей.
"""

import asyncio
import hashlib
import json
import logging
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from storage import db

logger = logging.getLogger(__name__)

# ─── Вспомогательные ────────────────────────────────────────────────────────

def _md5(data: Any) -> str:
    text = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.md5(text.encode()).hexdigest()


def _conn() -> sqlite3.Connection:
    return db.get_connection()


async def _run(fn, *args):
    """Запустить синхронную функцию в thread pool."""
    return await asyncio.to_thread(fn, *args)


# ─── Контекстные снапшоты ───────────────────────────────────────────────────

async def save_context(source: str, data: dict) -> None:
    """Сохранить снапшот. Дедупликация по MD5 — не сохраняет если ничего не изменилось."""
    h = _md5(data)

    def _do(h=h):
        conn = _conn()
        # Проверяем последний хэш для этого источника
        row = conn.execute(
            "SELECT hash FROM context_snapshots WHERE source=? ORDER BY ts DESC LIMIT 1",
            (source,),
        ).fetchone()
        if row and row["hash"] == h:
            logger.debug("context_store: '%s' не изменился, пропускаем", source)
            return False
        conn.execute(
            "INSERT INTO context_snapshots (source, data, hash) VALUES (?,?,?)",
            (source, json.dumps(data, ensure_ascii=False, default=str), h),
        )
        conn.commit()
        return True

    saved = await _run(_do)
    if saved:
        logger.debug("context_store: сохранён снапшот '%s'", source)


async def get_all_recent(hours: int = 48) -> list[dict]:
    """Все снапшоты за последние N часов, группированные по source (только свежайший)."""
    since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()

    def _do():
        conn = _conn()
        rows = conn.execute(
            """
            SELECT source, data, ts FROM context_snapshots
            WHERE ts >= ?
            GROUP BY source
            HAVING ts = MAX(ts)
            ORDER BY source
            """,
            (since,),
        ).fetchall()
        result = []
        for row in rows:
            try:
                result.append({
                    "source": row["source"],
                    "data": json.loads(row["data"]),
                    "ts": row["ts"],
                })
            except json.JSONDecodeError:
                pass
        return result

    return await _run(_do)


# ─── Задачи ─────────────────────────────────────────────────────────────────

async def save_tasks(tasks: list[dict]) -> None:
    """Сохранить список задач (INSERT OR REPLACE)."""
    def _do():
        conn = _conn()
        for t in tasks:
            conn.execute(
                """
                INSERT OR REPLACE INTO tasks
                    (id, title, priority, project, description, done, created_at, done_at, date)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    t["id"],
                    t["title"],
                    t["priority"],
                    t.get("project"),
                    t.get("description"),
                    int(t.get("done", False)),
                    t.get("created_at", datetime.utcnow().isoformat()),
                    t.get("done_at"),
                    t.get("date", date.today().isoformat()),
                ),
            )
        conn.commit()

    await _run(_do)
    logger.info("context_store: сохранено %d задач", len(tasks))


async def get_tasks_for_date(for_date: str | None = None) -> list[dict]:
    """Задачи за конкретный день (по умолчанию сегодня)."""
    for_date = for_date or date.today().isoformat()

    def _do():
        conn = _conn()
        rows = conn.execute(
            "SELECT * FROM tasks WHERE date=? ORDER BY priority DESC, created_at",
            (for_date,),
        ).fetchall()
        return [dict(r) for r in rows]

    return await _run(_do)


async def get_task_history(days: int = 7) -> list[dict]:
    """История задач за последние N дней."""
    since = (date.today() - timedelta(days=days)).isoformat()

    def _do():
        conn = _conn()
        rows = conn.execute(
            "SELECT * FROM tasks WHERE date >= ? ORDER BY date DESC, priority DESC",
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]

    return await _run(_do)


async def update_tasks_from_obsidian(tasks: list[dict]) -> None:
    """
    Обновить статус задач из Obsidian.
    Obsidian имеет приоритет — если там [ ] а у нас done=1, сбрасываем.
    """
    def _do():
        conn = _conn()
        for t in tasks:
            done_at = datetime.utcnow().isoformat() if t.get("done") else None
            conn.execute(
                "UPDATE tasks SET done=?, done_at=? WHERE id=?",
                (int(t.get("done", False)), done_at, t["id"]),
            )
        conn.commit()

    await _run(_do)


# ─── Ошибки ─────────────────────────────────────────────────────────────────

async def save_error(error: dict) -> bool:
    """
    Сохранить ошибку. Дедупликация: одна ошибка в одном месте — 1 раз за 10 минут.
    Возвращает True если сохранено (новая), False если дубль.
    """
    h = hashlib.md5(
        f"{error.get('file')}:{error.get('line')}:{error.get('error_type')}".encode()
    ).hexdigest()
    since = (datetime.utcnow() - timedelta(minutes=10)).isoformat()

    def _do():
        conn = _conn()
        existing = conn.execute(
            "SELECT id FROM errors WHERE hash=? AND ts >= ?",
            (h, since),
        ).fetchone()
        if existing:
            return False
        conn.execute(
            """
            INSERT INTO errors (error_type, message, file, line, project, source, hash)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                error.get("error_type", "Unknown"),
                error.get("message"),
                error.get("file"),
                error.get("line"),
                error.get("project"),
                error.get("source", "runtime"),
                h,
            ),
        )
        conn.commit()
        return True

    return await _run(_do)


async def get_errors(hours: int = 24) -> list[dict]:
    """Ошибки за последние N часов."""
    since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()

    def _do():
        conn = _conn()
        rows = conn.execute(
            """
            SELECT error_type, message, file, line, project, source, ts,
                   COUNT(*) as count
            FROM errors
            WHERE ts >= ?
            GROUP BY error_type, file, line
            ORDER BY count DESC, ts DESC
            """,
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]

    return await _run(_do)


async def count_errors_by_type(error_type: str, file: str, minutes: int = 60) -> int:
    """Количество ошибок одного типа в одном файле за последние N минут."""
    since = (datetime.utcnow() - timedelta(minutes=minutes)).isoformat()

    def _do():
        conn = _conn()
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM errors WHERE error_type=? AND file=? AND ts>=?",
            (error_type, file, since),
        ).fetchone()
        return row["cnt"] if row else 0

    return await _run(_do)


# ─── Продуктивность ─────────────────────────────────────────────────────────

async def save_productivity(session: dict) -> None:
    """Сохранить сессию продуктивности."""
    def _do():
        conn = _conn()
        conn.execute(
            """
            INSERT INTO productivity_sessions
                (date, start_ts, end_ts, duration_min, deep_work_min, switches_count, app_breakdown)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                session.get("date", date.today().isoformat()),
                session.get("start_ts"),
                session.get("end_ts"),
                session.get("duration_min"),
                session.get("deep_work_min"),
                session.get("switches_count"),
                json.dumps(session.get("app_breakdown", {})),
            ),
        )
        conn.commit()

    await _run(_do)


async def get_productivity(days: int = 1) -> list[dict]:
    """Сессии продуктивности за последние N дней."""
    since = (date.today() - timedelta(days=days)).isoformat()

    def _do():
        conn = _conn()
        rows = conn.execute(
            "SELECT * FROM productivity_sessions WHERE date >= ? ORDER BY date DESC",
            (since,),
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            if d.get("app_breakdown"):
                try:
                    d["app_breakdown"] = json.loads(d["app_breakdown"])
                except json.JSONDecodeError:
                    pass
            result.append(d)
        return result

    return await _run(_do)


# ─── LLM история ────────────────────────────────────────────────────────────

async def save_llm_run(trigger: str, model: str, duration_s: float,
                       tokens: int, task_count: int) -> None:
    def _do():
        conn = _conn()
        conn.execute(
            """
            INSERT INTO llm_runs (trigger, model, duration_s, tokens, task_count)
            VALUES (?,?,?,?,?)
            """,
            (trigger, model, duration_s, tokens, task_count),
        )
        conn.commit()

    await _run(_do)


async def get_last_llm_run() -> dict | None:
    """Последний запуск LLM."""
    def _do():
        conn = _conn()
        row = conn.execute(
            "SELECT * FROM llm_runs ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    return await _run(_do)


async def had_llm_run_today() -> bool:
    """Был ли хотя бы один запуск LLM сегодня."""
    today = date.today().isoformat()

    def _do():
        conn = _conn()
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM llm_runs WHERE ts >= ?",
            (today,),
        ).fetchone()
        return (row["cnt"] if row else 0) > 0

    return await _run(_do)


# ─── Очистка ────────────────────────────────────────────────────────────────

async def cleanup(ttl_days: int = 7) -> None:
    """Удалить данные старше TTL дней из всех таблиц."""
    since = (datetime.utcnow() - timedelta(days=ttl_days)).isoformat()

    def _do():
        conn = _conn()
        tables = {
            "context_snapshots": "ts",
            "errors": "ts",
            "llm_runs": "ts",
            "productivity_sessions": "date",
        }
        total = 0
        for table, col in tables.items():
            cursor = conn.execute(f"DELETE FROM {table} WHERE {col} < ?", (since,))
            total += cursor.rowcount
        conn.commit()
        return total

    deleted = await _run(_do)
    logger.info("cleanup: удалено %d устаревших записей (TTL=%d дней)", deleted, ttl_days)
