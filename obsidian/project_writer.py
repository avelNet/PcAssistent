"""
obsidian/project_writer.py — ведение документации проекта в Obsidian.

Структура (только для АКТИВНОГО проекта):
  ~/Obsidian/{project}/
    Dashboard.md          ← статус, ссылки, последние коммиты (обновляется каждый раз)
    Architecture.md       ← из ТЗ.md/README.md репозитория (создаётся один раз)
    Dev Log/DD.MM.YYYY.md ← ошибки, коммиты, сессия (дозаписывается)
    Decisions/README.md   ← заглушка, пользователь заполняет вручную

Фоновые проекты: только Daily/{date}.md (через TaskSyncer, не здесь)
"""

import asyncio
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_MONTHS_RU = {
    1: "января",   2: "февраля",  3: "марта",    4: "апреля",
    5: "мая",      6: "июня",     7: "июля",      8: "августа",
    9: "сентября", 10: "октября", 11: "ноября",  12: "декабря",
}


def _today_str() -> str:
    return date.today().strftime("%d.%m.%Y")


def _today_display() -> str:
    d = date.today()
    return f"{d.day} {_MONTHS_RU[d.month]} {d.year}"


class ProjectWriter:
    """
    Записывает и обновляет документацию активного проекта в ~/Obsidian/{project}/.
    Создавать только для активного проекта — фоновые пишутся через TaskSyncer.
    """

    def __init__(self, config: dict):
        obs_cfg = config.get("obsidian", {})
        shared = obs_cfg.get("shared_root", "")
        self.shared_root: Optional[Path] = Path(shared).expanduser() if shared else None
        self.enabled = bool(self.shared_root) and obs_cfg.get("enabled", False)

    def project_root(self, project: str) -> Path:
        assert self.shared_root is not None
        return self.shared_root / project

    # ─── Публичный API ───────────────────────────────────────────────────────

    def roadmap_exists(self, project: str) -> bool:
        """Проверить существует ли уже Roadmap.md для проекта."""
        if not self.shared_root:
            return True  # нет vault — не создаём
        return (self.project_root(project) / "Roadmap.md").exists()

    async def write_project_structure(
        self,
        project: str,
        repo_path: Optional[str] = None,
        git_snapshot: Optional[dict] = None,
        errors: Optional[list] = None,
        roadmap_content: Optional[str] = None,
    ) -> None:
        """
        Создать / обновить структуру документации активного проекта.
        Вызывается после каждого LLM-цикла для активного проекта.

        project      — название (совпадает с папкой ~/Obsidian/{project}/)
        repo_path    — абсолютный путь к git-репозиторию на диске
        git_snapshot — снапшот из GitWatcher (branch, recent_log, uncommitted_files…)
        errors       — список ошибок из context_store (за последние 24ч)
        """
        if not self.enabled or not project:
            return

        root = self.project_root(project)
        logger.info("ProjectWriter: обновляю документацию '%s'", project)

        await asyncio.gather(
            self._update_dashboard(project, root, repo_path, git_snapshot),
            self._ensure_architecture(project, root, repo_path),
            self._ensure_decisions(root),
            self._ensure_roadmap(project, root, roadmap_content),
            self._append_dev_log(project, root, git_snapshot, errors),
        )
        logger.info("ProjectWriter: структура '%s' готова (%s)", project, root)

    async def announce_focus_switch(
        self,
        new_project: str,
        config: dict,
    ) -> None:
        """
        При переключении фокуса на new_project — прочитать накопленные фоновые
        задачи из Obsidian и озвучить их через TTS.
        Вызывается из TriggerEngine до основного LLM-цикла.
        """
        if not self.enabled or not self.shared_root:
            return

        daily = self.shared_root / new_project / "Daily" / f"{_today_str()}.md"
        if not daily.exists():
            logger.debug("ProjectWriter: нет фоновых задач для '%s'", new_project)
            return

        try:
            content = await asyncio.to_thread(daily.read_text, "utf-8")
        except Exception:
            return

        # Парсим чекбоксы из файла
        import re
        tasks = []
        for line in content.splitlines():
            m = re.match(r'\s*-\s+\[( |x)\]\s+(.+?)(?:\s+<!--[^>]*-->)?\s*$', line)
            if m and m.group(1) == " ":  # только невыполненные
                tasks.append({"title": m.group(2).strip()})

        if not tasks:
            logger.debug("ProjectWriter: фоновые задачи для '%s' — все выполнены", new_project)
            return

        logger.info(
            "ProjectWriter: озвучиваю %d фоновых задач для '%s'",
            len(tasks), new_project
        )

        try:
            from voice.speech_output import SpeechOutput
            sp = SpeechOutput(config)
            # Краткий пролог о переключении
            prologue = f"Переключаюсь на проект {new_project}. Вот задачи которые я подготовил в фоне:"
            await sp.speak_tasks(tasks, prologue=prologue, trigger="focus_switch")
        except Exception as e:
            logger.warning("ProjectWriter: TTS ошибка при переключении фокуса: %s", e)

    # ─── Внутренние методы ───────────────────────────────────────────────────

    async def _update_dashboard(
        self,
        project: str,
        root: Path,
        repo_path: Optional[str],
        git_snapshot: Optional[dict],
    ) -> None:
        """Dashboard.md — перезаписываем каждый раз (статус меняется)."""
        path = root / "Dashboard.md"
        content = self._build_dashboard(project, repo_path, git_snapshot)

        def _write():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        await asyncio.to_thread(_write)
        logger.debug("ProjectWriter: Dashboard.md обновлён")

    async def _ensure_architecture(
        self,
        project: str,
        root: Path,
        repo_path: Optional[str],
    ) -> None:
        """
        Architecture.md — создаём только если не существует.
        Читаем из ТЗ.md / README.md репозитория.
        Не перезаписываем — пользователь мог отредактировать.
        """
        path = root / "Architecture.md"
        if path.exists():
            return

        content = await asyncio.to_thread(
            self._build_architecture, project, repo_path
        )

        def _write():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        await asyncio.to_thread(_write)
        logger.info("ProjectWriter: Architecture.md создан → %s", path)

    async def _ensure_decisions(self, root: Path) -> None:
        """Создать Decisions/ с README если его нет."""
        readme = root / "Decisions" / "README.md"
        if readme.exists():
            return

        def _write():
            readme.parent.mkdir(parents=True, exist_ok=True)
            readme.write_text(
                "# Решения по проекту\n\n"
                "Здесь фиксируются архитектурные и продуктовые решения.\n\n"
                "Формат файла: `DD.MM.YYYY — Название решения.md`\n\n"
                "Пример:\n"
                "- `28.05.2026 — Переход на файловую систему вместо REST API.md`\n",
                encoding="utf-8",
            )

        await asyncio.to_thread(_write)
        logger.debug("ProjectWriter: Decisions/README.md создан")

    async def _ensure_roadmap(
        self,
        project: str,
        root: Path,
        llm_content: Optional[str] = None,
    ) -> None:
        """
        Создать Roadmap.md если его нет. Никогда не перезаписывать.
        llm_content — сгенерированный LLM текст; если None — пустой шаблон.
        """
        path = root / "Roadmap.md"
        if path.exists():
            return

        today = _today_str()
        header = (
            f"---\nproject: {project}\ntype: roadmap\ncreated: {today}\n---\n\n"
            f"# 🗺 Roadmap — {project}\n\n"
            f"> Составлено ассистентом. Редактируй под свои нужды.\n\n"
        )

        if llm_content:
            content = header + llm_content
        else:
            content = (
                header
                + "## 🔥 В работе\n\n\n"
                + "## 📅 Планируется\n\n- [ ] \n\n"
                + "## 💡 Идеи\n\n- [ ] \n\n"
                + "## ✅ Сделано\n\n"
            )

        def _write():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        await asyncio.to_thread(_write)
        logger.info("ProjectWriter: Roadmap.md создан → %s", path)

    async def _append_dev_log(
        self,
        project: str,
        root: Path,
        git_snapshot: Optional[dict],
        errors: Optional[list],
    ) -> None:
        """
        Dev Log/DD.MM.YYYY.md — дозаписываем сессию.
        Не пишем если коммиты и ошибки не изменились с прошлой записи.
        """
        today = _today_str()
        dev_log_dir = root / "Dev Log"
        path = dev_log_dir / f"{today}.md"

        # Последний хеш коммита из снапшота
        last_commit_hash = ""
        if git_snapshot:
            log = git_snapshot.get("recent_log", "")
            last_commit_hash = log.splitlines()[0][:7] if log.strip() else ""

        def _write():
            dev_log_dir.mkdir(parents=True, exist_ok=True)
            # Проверяем: если файл существует и уже содержит этот коммит — не пишем
            if path.exists() and last_commit_hash:
                existing = path.read_text(encoding="utf-8")
                if last_commit_hash in existing:
                    return False  # ничего нового

            if not path.exists():
                header = (
                    f"---\ndate: {today}\nproject: {project}\ntype: dev-log\n---\n\n"
                    f"# 🛠 Dev Log — {_today_display()}\n"
                )
                path.write_text(header, encoding="utf-8")

            entry = self._build_dev_log_entry(git_snapshot, errors)
            with open(path, "a", encoding="utf-8") as f:
                f.write(entry)
            return True

        written = await asyncio.to_thread(_write)
        if written:
            logger.debug("ProjectWriter: Dev Log/%s.md — новая запись", today)

    # ─── Авто-завершение задач ───────────────────────────────────────────────

    async def auto_complete_task_fs(
        self,
        task_id: str,
        project: str,
        done_by: str = "ассистент",
    ) -> bool:
        """
        Пометить задачу как выполненную напрямую в FS-файле.
        Изменяет [ ] → [x] и добавляет <!-- ✓ {done_by} HH:MM -->.
        Возвращает True если задача найдена и обновлена, False если уже выполнена или не найдена.
        """
        if not self.shared_root or not project or not task_id:
            return False

        daily = self.shared_root / project / "Daily" / f"{_today_str()}.md"
        if not daily.exists():
            return False

        def _update() -> bool:
            content = daily.read_text(encoding="utf-8")
            lines = content.splitlines()
            changed = False
            now_str = datetime.now().strftime("%H:%M")

            for i, line in enumerate(lines):
                # Ищем незавершённую задачу с нашим id
                if f"id:{task_id}" not in line:
                    continue
                if "- [x]" in line:
                    return False  # уже выполнена — не трогаем
                if "- [ ]" in line:
                    # Убираем старый id-тег, добавляем x и двойную атрибуцию
                    new_line = line.replace("- [ ]", "- [x]", 1)
                    # Добавляем атрибуцию после id-тега
                    new_line = re.sub(
                        r'(<!--\s+id:[^>]+-->)',
                        rf'\1 <!-- ✓ {done_by} {now_str} -->',
                        new_line,
                    )
                    lines[i] = new_line
                    changed = True
                    break

            if changed:
                daily.write_text("\n".join(lines), encoding="utf-8")
            return changed

        result = await asyncio.to_thread(_update)
        if result:
            logger.info(
                "ProjectWriter: задача '%s' → [x] (помечена: %s)", task_id, done_by
            )
        return result

    async def is_task_done_in_obsidian(self, task_id: str, project: str) -> bool:
        """Проверить отмечена ли задача в Obsidian-файле."""
        if not self.shared_root or not project or not task_id:
            return False

        daily = self.shared_root / project / "Daily" / f"{_today_str()}.md"
        if not daily.exists():
            return False

        def _check():
            content = daily.read_text(encoding="utf-8")
            for line in content.splitlines():
                if f"id:{task_id}" in line and "- [x]" in line:
                    return True
            return False

        return await asyncio.to_thread(_check)

    # ─── Генераторы контента ─────────────────────────────────────────────────

    def _build_dashboard(
        self,
        project: str,
        repo_path: Optional[str],
        git_snapshot: Optional[dict],
    ) -> str:
        now = datetime.now()
        today = _today_str()

        lines = [
            "---",
            f"project: {project}",
            f"updated: {now.strftime('%d.%m.%Y %H:%M')}",
            "type: dashboard",
            "---",
            "",
            f"# 🚀 {project}",
            "",
            f"> Обновлено: {now.strftime('%H:%M')}, {_today_display()}",
            "",
            "## 🔗 Навигация",
            "",
            f"- [[Daily/{today}|📋 Задачи сегодня]]",
            "- [[Architecture|🏗 Архитектура]]",
            f"- [[Dev Log/{today}|🛠 Dev Log сегодня]]",
            "- [[Decisions/README|📝 Решения]]",
            "- [[Roadmap|🗺 Roadmap]]",
            "",
        ]

        if git_snapshot:
            lines += ["## 📦 Git", ""]
            branch = git_snapshot.get("branch", "?")
            lines.append(f"**Ветка:** `{branch}`")

            uncommitted = git_snapshot.get("uncommitted_files", [])
            if uncommitted:
                lines.append(f"**Изменено файлов:** {len(uncommitted)}")
            else:
                lines.append("**Состояние:** чисто ✅")

            recent_log = git_snapshot.get("recent_log", "").strip()
            if recent_log:
                lines += ["", "**Последние коммиты:**", "```"]
                for ln in recent_log.splitlines()[:5]:
                    lines.append(ln)
                lines.append("```")
            lines.append("")

        if repo_path:
            lines += [
                "## 📁 Репозиторий",
                "",
                f"`{repo_path}`",
                "",
            ]

        return "\n".join(lines)

    def _build_architecture(
        self,
        project: str,
        repo_path: Optional[str],
    ) -> str:
        """
        Генерирует Architecture.md из ТЗ.md / README.md репозитория.
        Вызывается синхронно из to_thread.
        """
        lines = [
            "---",
            f"project: {project}",
            "type: architecture",
            "---",
            "",
            f"# 🏗 Архитектура — {project}",
            "",
            "> Создано автоматически из репозитория. Редактируй смело — повторной перезаписи не будет.",
            "",
        ]

        if not repo_path:
            lines += ["Репозиторий не найден. Заполни раздел вручную.", ""]
            return "\n".join(lines)

        repo = Path(repo_path)

        # Источники в порядке приоритета
        candidates = [
            ("ТЗ.md",          "Техническое задание"),
            ("TZ.md",          "Техническое задание"),
            ("ARCHITECTURE.md","Архитектура"),
            ("architecture.md","Архитектура"),
            ("README.md",      "README"),
            ("readme.md",      "README"),
        ]

        found_any = False
        for filename, label in candidates:
            src = repo / filename
            if not src.exists():
                continue
            try:
                raw = src.read_text(encoding="utf-8")
                # Обрезаем большие файлы
                if len(raw) > 12000:
                    raw = raw[:12000] + "\n\n…(обрезано — открой оригинал)\n"

                lines += [f"## 📄 {label} (`{filename}`)", ""]
                # Смещаем заголовки чтобы не конфликтовали с нашим h1
                for ln in raw.splitlines():
                    if ln.startswith("### "):
                        lines.append("#### " + ln[4:])
                    elif ln.startswith("## "):
                        lines.append("### " + ln[3:])
                    elif ln.startswith("# "):
                        lines.append("## " + ln[2:])
                    else:
                        lines.append(ln)
                lines.append("")
                found_any = True
                logger.info(
                    "ProjectWriter: %s/%s включён в архитектуру (%d симв.)",
                    project, filename, len(raw)
                )
            except Exception as e:
                logger.debug("ProjectWriter: не удалось прочитать %s: %s", filename, e)

        if not found_any:
            lines += [
                "Файлы ТЗ.md / README.md не найдены.",
                "",
                f"Путь репозитория: `{repo_path}`",
                "",
                "Добавь описание архитектуры вручную.",
            ]

        return "\n".join(lines)

    def _build_dev_log_entry(
        self,
        git_snapshot: Optional[dict],
        errors: Optional[list],
    ) -> str:
        """Одна запись в Dev Log (дозаписывается, не заменяется)."""
        now = datetime.now().strftime("%H:%M")
        lines = [f"\n## ⏱ {now}\n"]

        has_content = False

        if git_snapshot:
            uncommitted = git_snapshot.get("uncommitted_files", [])
            if uncommitted:
                lines += ["**Изменено:**"]
                for f in uncommitted[:8]:
                    fname = f.get("path", str(f)) if isinstance(f, dict) else str(f)
                    lines.append(f"- `{fname}`")
                lines.append("")
                has_content = True

            recent_log = git_snapshot.get("recent_log", "").strip()
            if recent_log:
                lines += ["**Коммиты:**", "```"]
                for ln in recent_log.splitlines()[:3]:
                    lines.append(ln)
                lines += ["```", ""]
                has_content = True

        if errors:
            lines += ["**Ошибки:**"]
            for err in errors[:5]:
                etype = err.get("error_type", "?")
                msg   = (err.get("message") or "")[:80]
                fpath = err.get("file") or ""
                loc   = f" в `{fpath}`" if fpath else ""
                lines.append(f"- `{etype}`{loc}: {msg}")
            lines.append("")
            has_content = True

        if not has_content:
            lines.append("_Нет активности в этой сессии._\n")

        return "\n".join(lines)
