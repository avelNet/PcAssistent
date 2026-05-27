"""
obsidian/client.py — HTTP клиент к плагину Obsidian Local REST API.

Принцип (ТЗ §9.1): после каждой записи — открыть, уведомить, второй монитор.
API: https://github.com/coddingtonbear/obsidian-local-rest-api
Порт: https://127.0.0.1:27124 (self-signed SSL — verify=False, всё локально)
"""

import asyncio
import logging
import re
import ssl
from datetime import date, datetime
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

_MONTHS_RU = {
    "January": "января",  "February": "февраля", "March": "марта",
    "April":   "апреля",  "May":      "мая",      "June":  "июня",
    "July":    "июля",    "August":   "августа",  "September": "сентября",
    "October": "октября", "November": "ноября",   "December":  "декабря",
}


def _today_display() -> str:
    """Возвращает '27 мая 2026' для текущей даты."""
    s = date.today().strftime("%-d %B %Y")
    for en, ru in _MONTHS_RU.items():
        s = s.replace(en, ru)
    return s


class ObsidianClient:
    def __init__(self, config: dict):
        obs_cfg = config.get("obsidian", {})
        self.enabled: bool     = obs_cfg.get("enabled", False)
        self.api_key: str      = obs_cfg.get("api_key", "")
        self.host: str         = obs_cfg.get("host", "https://127.0.0.1:27124").rstrip("/")
        self.daily_folder: str = obs_cfg.get("daily_folder", "Daily")
        self.auto_open: bool   = obs_cfg.get("auto_open", True)
        self._session: Optional[aiohttp.ClientSession] = None

        # Плагин использует self-signed сертификат — отключаем проверку (локально безопасно)
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

    # ─── Сессия ──────────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "text/markdown; charset=utf-8",
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
            self._session = aiohttp.ClientSession(
                headers=self._headers(),
                connector=connector,
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ─── Доступность ─────────────────────────────────────────────────────────

    async def check_availability(self) -> bool:
        """Проверить что плагин запущен и отвечает. Быстро — timeout 3с."""
        if not self.enabled:
            return False
        try:
            session = await self._get_session()
            async with session.get(
                f"{self.host}/",
                timeout=aiohttp.ClientTimeout(total=3),
            ) as resp:
                return resp.status < 500
        except Exception:
            return False

    async def project_exists_in_vault(self, project: str) -> bool:
        """
        Проверить что папка проекта реально существует в vault (не создавать!).
        Задачи проекта пишем в {project}/Daily/ только если папка уже есть.
        Иначе используем корневую Daily/ — чтобы не засорять чужие vault-ы.
        """
        if not self.enabled or not project:
            return False
        try:
            session = await self._get_session()
            async with session.get(
                f"{self.host}/vault/{project}/",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    # ─── Базовые CRUD ────────────────────────────────────────────────────────

    async def read_note(self, path: str) -> Optional[str]:
        """Прочитать заметку по пути относительно vault root. None если не найдена."""
        if not self.enabled:
            return None
        try:
            session = await self._get_session()
            async with session.get(
                f"{self.host}/vault/{path}",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    return await resp.text()
                if resp.status == 404:
                    return None
                logger.warning("obsidian: read_note '%s' → HTTP %d", path, resp.status)
                return None
        except Exception as e:
            logger.error("obsidian: read_note ошибка — %s", e)
            return None

    async def write_note(self, path: str, content: str, *, open_after: bool = True) -> bool:
        """
        Создать / полностью заменить заметку (PUT).
        После записи — открыть + второй монитор + уведомление (§9.1).
        """
        if not self.enabled:
            return False
        try:
            session = await self._get_session()
            async with session.put(
                f"{self.host}/vault/{path}",
                data=content.encode("utf-8"),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status not in (200, 204):
                    logger.error("obsidian: write_note '%s' → HTTP %d", path, resp.status)
                    return False
        except Exception as e:
            logger.error("obsidian: write_note ошибка — %s", e)
            return False

        logger.info("obsidian: записано '%s' (%d байт)", path, len(content.encode()))
        if open_after and self.auto_open:
            await self._post_write(path)
        return True

    async def append_note(self, path: str, content: str, *, open_after: bool = True) -> bool:
        """
        Дозаписать в конец заметки (POST). Создаёт файл если не существует.
        После записи — открыть + второй монитор + уведомление.
        """
        if not self.enabled:
            return False
        try:
            session = await self._get_session()
            async with session.post(
                f"{self.host}/vault/{path}",
                data=content.encode("utf-8"),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status not in (200, 204):
                    logger.error("obsidian: append_note '%s' → HTTP %d", path, resp.status)
                    return False
        except Exception as e:
            logger.error("obsidian: append_note ошибка — %s", e)
            return False

        logger.info("obsidian: дозаписано в '%s'", path)
        if open_after and self.auto_open:
            await self._post_write(path, note_type="заметка обновлена")
        return True

    async def _post_write(self, path: str, note_type: str = "заметка") -> None:
        """
        Действия после любой записи (§9.1):
        1. Открыть в Obsidian
        2. Переместить на второй монитор
        3. Уведомление
        """
        from core.notifier import notify_obsidian_written
        await self.open_on_second_monitor(path)
        await notify_obsidian_written(path, note_type)

    # ─── Открытие в Obsidian ─────────────────────────────────────────────────

    async def open_in_obsidian(self, path: str) -> None:
        """Отправить команду открыть заметку в Obsidian (POST /open/{path})."""
        if not self.enabled:
            return
        try:
            session = await self._get_session()
            async with session.post(
                f"{self.host}/open/{path}",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status not in (200, 204):
                    logger.debug("obsidian: open_in_obsidian → HTTP %d", resp.status)
        except Exception as e:
            logger.debug("obsidian: open_in_obsidian ошибка — %s", e)

    # ─── Второй монитор (§9.4) ───────────────────────────────────────────────

    async def _get_monitors(self) -> list[dict]:
        """
        Список мониторов через xrandr --listmonitors.
        Формат строки: " 0: +*eDP-1 1920/309x1080/173+0+0  eDP-1"
        Возвращает: [{"name": str, "x": int, "y": int, "w": int, "h": int}]
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "xrandr", "--listmonitors",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
            monitors = []
            for line in stdout.decode().splitlines()[1:]:  # пропускаем "Monitors: N"
                m = re.search(
                    r'\d+:\s+\S+\s+(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)\s+(\S+)',
                    line,
                )
                if m:
                    monitors.append({
                        "w":    int(m.group(1)),
                        "h":    int(m.group(2)),
                        "x":    int(m.group(3)),
                        "y":    int(m.group(4)),
                        "name": m.group(5),
                    })
            return monitors
        except Exception as e:
            logger.debug("obsidian: xrandr ошибка — %s", e)
            return []

    async def open_on_second_monitor(self, path: str) -> None:
        """
        Открыть заметку в Obsidian.
        Если два монитора — переместить Obsidian на второй (§9.4).
        Если один — просто открывает, не перекрывает рабочее окно.
        """
        await self.open_in_obsidian(path)
        await asyncio.sleep(0.5)  # дать Obsidian время перерисоваться

        monitors = await self._get_monitors()
        if len(monitors) >= 2:
            m = monitors[1]  # второй монитор (не primary)
            cmd = [
                "wmctrl", "-r", "Obsidian",
                "-e", f"0,{m['x']},{m['y']},{m['w']},{m['h']}",
            ]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), timeout=3)
                logger.debug("obsidian: Obsidian перемещён на %s (%dx%d+%d+%d)",
                             m["name"], m["w"], m["h"], m["x"], m["y"])
            except FileNotFoundError:
                logger.debug("obsidian: wmctrl не установлен — пропускаем перемещение")
            except Exception as e:
                logger.debug("obsidian: wmctrl ошибка — %s", e)

    # ─── Пути дейли заметок ──────────────────────────────────────────────────

    def daily_path(self, project: Optional[str] = None) -> str:
        """
        Путь к дейли заметке относительно vault root (§9.3 Правило 2):
          С проектом:  {project}/Daily/DD.MM.YYYY.md
          Без проекта: Daily/DD.MM.YYYY.md
        """
        today = date.today().strftime("%d.%m.%Y")
        if project:
            return f"{project}/{self.daily_folder}/{today}.md"
        return f"{self.daily_folder}/{today}.md"

    # ─── Форматирование заметок ──────────────────────────────────────────────

    def _build_task_list(
        self,
        tasks: list[dict],
        prologue: str = "",
        project: Optional[str] = None,
    ) -> str:
        """
        Markdown дейли заметки с задачами (§9.2, §9.3).
        Структура: frontmatter → заголовок → пролог → задачи по приоритетам.
        id задачи кодируется как HTML-комментарий для обратной синхронизации.
        """
        today_str  = date.today().strftime("%d.%m.%Y")
        today_disp = _today_display()

        lines = ["---", f"date: {today_str}", "type: task-list"]
        if project:
            lines.append(f"project: {project}")
        lines += ["generated_by: pc-assistant", "---", ""]
        lines += [f"# 📋 Задачи на {today_disp}", ""]

        if prologue:
            # Максимум 4 строки (§9.3 Правило 4 — минимализм)
            for ln in prologue.strip().splitlines()[:4]:
                lines.append(f"> {ln}")
            lines.append("")

        sections = [
            ("HIGH", "🔴 Высокий приоритет"),
            ("MED",  "🟡 Средний приоритет"),
            ("LOW",  "🟢 Низкий приоритет"),
        ]
        for priority, heading in sections:
            group = [t for t in tasks if t.get("priority") == priority]
            if not group:
                continue
            lines += [f"## {heading}", ""]
            for task in group:
                task_id = task.get("id", "")
                title   = task.get("title", "")
                desc    = task.get("description", "")
                id_tag  = f" <!-- id:{task_id} -->" if task_id else ""
                lines.append(f"- [ ] {title}{id_tag}")
                if desc:
                    lines.append(f"  > _{desc}_")
            lines.append("")

        return "\n".join(lines)

    def _build_evening_summary(
        self,
        tasks: list[dict],
        prologue: str = "",
    ) -> str:
        """Секция вечернего итога для дозаписи в дейли заметку (§9.2)."""
        now_str = datetime.now().strftime("%H:%M")

        done    = [t for t in tasks if t.get("done")]
        pending = [t for t in tasks if not t.get("done")]

        lines = ["", "---", "", f"## 📊 Итог дня ({now_str})", ""]

        if done:
            lines += ["### ✅ Сделано", ""]
            for t in done:
                lines.append(f"- [x] {t['title']}")
            lines.append("")

        if pending:
            lines += ["### 🔄 Не завершено", ""]
            for t in pending:
                lines.append(f"- [ ] {t['title']}")
            lines.append("")

        if prologue:
            lines += ["### 💡 Заметки ассистента", ""]
            for ln in prologue.strip().splitlines()[:4]:
                lines.append(ln)
            lines.append("")

        return "\n".join(lines)

    # ─── Высокоуровневые операции ────────────────────────────────────────────

    async def create_daily_note(
        self,
        tasks: list[dict],
        prologue: str = "",
        project: Optional[str] = None,
    ) -> Optional[str]:
        """
        Создать дейли заметку с задачами.
        Если уже существует — не перезаписывает, только открывает.
        Возвращает путь к заметке или None при ошибке.
        """
        if not self.enabled:
            return None

        path = self.daily_path(project)

        # Проверяем существование (§9.3 Правило 3 — дозаписывать не перезаписывать)
        existing = await self.read_note(path)
        if existing is not None:
            logger.info("obsidian: '%s' уже существует — открываю без перезаписи", path)
            if self.auto_open:
                await self.open_on_second_monitor(path)
            return path

        content = self._build_task_list(tasks, prologue, project)
        ok = await self.write_note(path, content, open_after=True)
        return path if ok else None

    async def append_evening_summary(
        self,
        tasks: list[dict],
        prologue: str = "",
        project: Optional[str] = None,
    ) -> Optional[str]:
        """
        Дозаписать вечерний итог в дейли заметку.
        Создаёт заметку если её ещё нет.
        """
        if not self.enabled:
            return None

        path = self.daily_path(project)
        content = self._build_evening_summary(tasks, prologue)
        ok = await self.append_note(path, content, open_after=True)
        return path if ok else None

    # ─── Синхронизация задач ────────────────────────────────────────────────

    async def get_today_tasks(self, project: Optional[str] = None) -> list[dict]:
        """
        Прочитать и распарсить задачи из дейли заметки.
        Возвращает: [{id, title, done, line_num}]
        Задачи без <!-- id:... --> тоже включаются (пользовательские, id="").
        """
        if not self.enabled:
            return []

        path = self.daily_path(project)
        content = await self.read_note(path)
        if not content:
            return []

        tasks = []
        for i, line in enumerate(content.splitlines()):
            # Парсим: - [ ] Title <!-- id:uuid -->
            #         - [x] Title <!-- id:uuid -->
            m = re.match(
                r'\s*-\s+\[( |x)\]\s+(.+?)(?:\s+<!--\s+id:([^>]+?)\s+-->)?\s*$',
                line,
            )
            if m:
                tasks.append({
                    "id":       (m.group(3) or "").strip(),
                    "title":    m.group(2).strip(),
                    "done":     m.group(1) == "x",
                    "line_num": i,
                })
        return tasks

    async def update_task_status(
        self,
        task_id: str,
        done: bool,
        project: Optional[str] = None,
    ) -> bool:
        """
        Изменить [ ]↔[x] для задачи по id.
        Читает файл, патчит нужную строку, пишет обратно (PUT).
        Не открывает Obsidian — пользователь уже в нём.
        """
        if not self.enabled or not task_id:
            return False

        path = self.daily_path(project)
        content = await self.read_note(path)
        if content is None:
            return False

        old_cb, new_cb = ("[ ]", "[x]") if done else ("[x]", "[ ]")
        lines = content.splitlines()
        changed = False

        for i, line in enumerate(lines):
            if f"id:{task_id}" in line and old_cb in line:
                lines[i] = line.replace(old_cb, new_cb, 1)
                changed = True
                break

        if not changed:
            logger.warning("obsidian: update_task_status — задача '%s' не найдена", task_id)
            return False

        # Пишем без open_after — пользователь сам в Obsidian
        new_content = "\n".join(lines)
        try:
            session = await self._get_session()
            async with session.put(
                f"{self.host}/vault/{path}",
                data=new_content.encode("utf-8"),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                return resp.status in (200, 204)
        except Exception as e:
            logger.error("obsidian: update_task_status ошибка — %s", e)
            return False
