"""
vault_reader.py — динамический сканер Obsidian vault'ов.

Читает /home/pavel/Obsidian (или любой root из конфига) и находит
все проекты/vault'ы автоматически. Новые папки подхватываются через
inotify без перезапуска.

Структура которую понимает:
    ~/Obsidian/
    ├── Tap2Go Vault/          ← vault (есть .obsidian/)
    │   ├── .obsidian/
    │   ├── Services/
    │   ├── API/
    │   └── Daily/             ← пропускаем
    ├── AnotherProject/        ← просто папка с заметками
    │   └── ...
    └── SomeNote.md            ← заметка в корне
"""

import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ObsidianNote:
    path: Path
    relative_path: str      # относительно root
    project: str            # название проекта/vault
    size_bytes: int
    modified_at: float      # unix timestamp
    content: str = ""       # заполняется при чтении


@dataclass
class VaultIndex:
    """Полный индекс всех найденных заметок."""
    projects: dict[str, list[ObsidianNote]] = field(default_factory=dict)
    total_notes: int = 0
    scanned_at: float = field(default_factory=time.time)


class VaultReader:
    def __init__(self, config: dict):
        obs_cfg = config.get("obsidian", {})
        self.root = Path(obs_cfg.get("root", "~/Obsidian")).expanduser()
        self.skip_folders = set(obs_cfg.get("skip_folders", [".obsidian", "Templates", "Daily"]))
        self.max_note_size_kb = obs_cfg.get("max_note_size_kb", 10)
        self.preview_chars = obs_cfg.get("preview_chars", 600)
        self.recent_days = obs_cfg.get("recent_days", 7)

    def scan(self) -> VaultIndex:
        """
        Полный скан root-папки. Находит все проекты и заметки.
        Запускать в thread pool (синхронный I/O).
        """
        index = VaultIndex()

        if not self.root.exists():
            logger.warning("VaultReader: папка не найдена: %s", self.root)
            return index

        logger.info("VaultReader: сканирую %s", self.root)

        # Ищем проекты — любые директории верхнего уровня
        for entry in sorted(self.root.iterdir()):
            if entry.is_dir() and not entry.name.startswith("."):
                notes = self._scan_project(entry)
                if notes:
                    index.projects[entry.name] = notes
                    index.total_notes += len(notes)
            elif entry.is_file() and entry.suffix == ".md":
                # Заметки прямо в корне
                note = self._read_note_meta(entry, project="root")
                if note:
                    index.projects.setdefault("root", []).append(note)
                    index.total_notes += 1

        logger.info(
            "VaultReader: найдено %d проектов, %d заметок",
            len(index.projects), index.total_notes
        )
        return index

    def _scan_project(self, project_dir: Path) -> list[ObsidianNote]:
        """Рекурсивно собрать все .md файлы из директории проекта."""
        notes = []
        project_name = project_dir.name

        for md_file in self._walk_md_files(project_dir):
            note = self._read_note_meta(md_file, project=project_name)
            if note:
                notes.append(note)

        return notes

    def _walk_md_files(self, base: Path):
        """Обход .md файлов с пропуском служебных папок."""
        try:
            for entry in base.iterdir():
                if entry.is_dir():
                    if entry.name not in self.skip_folders and not entry.name.startswith("."):
                        yield from self._walk_md_files(entry)
                elif entry.is_file() and entry.suffix == ".md":
                    yield entry
        except PermissionError:
            pass

    def _read_note_meta(self, path: Path, project: str) -> ObsidianNote | None:
        """Читает метаданные заметки (без содержимого — оно читается отдельно)."""
        try:
            stat = path.stat()
            relative = str(path.relative_to(self.root))
            return ObsidianNote(
                path=path,
                relative_path=relative,
                project=project,
                size_bytes=stat.st_size,
                modified_at=stat.st_mtime,
            )
        except (OSError, ValueError):
            return None

    def read_content(self, note: ObsidianNote) -> str:
        """
        Читает содержимое заметки.
        Большие заметки обрезает до preview_chars.
        Недавно изменённые — всегда полностью (если <= max_note_size_kb).
        """
        max_bytes = self.max_note_size_kb * 1024
        recent_cutoff = time.time() - self.recent_days * 86400

        try:
            is_recent = note.modified_at >= recent_cutoff
            is_small = note.size_bytes <= max_bytes

            with open(note.path, encoding="utf-8", errors="replace") as f:
                if is_small or is_recent:
                    content = f.read()
                else:
                    content = f.read(self.preview_chars)
                    if note.size_bytes > self.preview_chars:
                        content += f"\n\n... [ещё {note.size_bytes - self.preview_chars} байт]"

            return content
        except OSError as e:
            logger.warning("VaultReader: не удалось прочитать %s: %s", note.path, e)
            return ""

    def build_context(self) -> dict:
        """
        Собрать контекст для LLM из всего vault'а.
        Возвращает структурированный словарь.
        """
        index = self.scan()

        projects_context = {}
        for project_name, notes in index.projects.items():
            project_notes = []
            for note in sorted(notes, key=lambda n: n.modified_at, reverse=True):
                content = self.read_content(note)
                if not content.strip():
                    continue
                project_notes.append({
                    "path": note.relative_path,
                    "modified_days_ago": round((time.time() - note.modified_at) / 86400, 1),
                    "size_kb": round(note.size_bytes / 1024, 1),
                    "content": content,
                })
            if project_notes:
                projects_context[project_name] = project_notes

        return {
            "projects": projects_context,
            "total_notes": index.total_notes,
            "vault_root": str(self.root),
        }

    def get_structure_summary(self) -> str:
        """
        Краткое описание структуры vault'а для LLM промпта.
        Не читает содержимое — только названия файлов.
        """
        index = self.scan()
        lines = [f"Obsidian vault: {self.root}"]

        for project_name, notes in sorted(index.projects.items()):
            lines.append(f"\n📁 {project_name}/ ({len(notes)} заметок)")
            # Показываем только самые свежие/важные
            recent = sorted(notes, key=lambda n: n.modified_at, reverse=True)[:10]
            for note in recent:
                days = (time.time() - note.modified_at) / 86400
                age = f"{int(days)}д назад" if days >= 1 else "сегодня"
                name = Path(note.relative_path).stem
                # Убираем название проекта из пути для краткости
                rel = note.relative_path.replace(f"{project_name}/", "", 1)
                lines.append(f"  - {rel} ({age})")

        return "\n".join(lines)

    def parse_kanban(self) -> dict:
        """
        Найти и распарсить все канбан-доски в vault'е.
        Возвращает статус задач по колонкам.

        Формат Obsidian Kanban plugin:
            ## 🔄 В работе
            - [ ] [[Services/Service 01|Сервис 01]]
        """
        kanban_files = []
        if not self.root.exists():
            return {}

        # Ищем файлы с kanban-plugin в frontmatter
        for md_file in self.root.rglob("*.md"):
            if any(p in self.skip_folders for p in md_file.parts):
                continue
            try:
                with open(md_file, encoding="utf-8", errors="replace") as f:
                    head = f.read(200)
                if "kanban-plugin" in head:
                    kanban_files.append(md_file)
            except OSError:
                pass

        if not kanban_files:
            return {}

        result = {}
        for kb_file in kanban_files:
            project = kb_file.parent.parent.name  # vault name
            parsed = self._parse_kanban_file(kb_file)
            if parsed:
                result[project] = parsed
                logger.info(
                    "VaultReader: канбан '%s' — в работе: %d, не начато: %d, готово: %d",
                    kb_file.name,
                    len(parsed.get("in_progress", [])),
                    len(parsed.get("not_started", [])),
                    len(parsed.get("done", [])),
                )
        return result

    def _parse_kanban_file(self, path: Path) -> dict:
        """Парсит одну канбан-доску. Возвращает словарь с колонками."""
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {}

        # Паттерн: ## Заголовок колонки
        section_re = re.compile(r"^##\s+(.+)$", re.MULTILINE)
        # Паттерн: - [ ] или - [x] с wiki-ссылкой или обычным текстом
        item_re = re.compile(r"^\s*-\s+\[([xX ]?)\]\s+(?:\[\[(?:[^\]|]+)(?:\|([^\]]+))?\]\]|(.+))$", re.MULTILINE)

        # Определяем смысл колонки по ключевым словам
        def classify_column(header: str) -> str:
            h = header.lower()
            if any(w in h for w in ["в работе", "in progress", "doing", "🔄"]):
                return "in_progress"
            if any(w in h for w in ["готово", "done", "завершено", "✅", "complete"]):
                return "done"
            if any(w in h for w in ["не начат", "todo", "backlog", "📋", "to do"]):
                return "not_started"
            return "other"

        columns: dict[str, list[str]] = {
            "in_progress": [],
            "not_started": [],
            "done": [],
            "other": [],
        }

        # Разбиваем контент по секциям
        sections = section_re.split(content)
        # sections: [pre, header1, body1, header2, body2, ...]
        i = 1
        while i < len(sections) - 1:
            header = sections[i].strip()
            body = sections[i + 1]
            col_type = classify_column(header)

            for m in item_re.finditer(body):
                # Извлекаем отображаемое имя: либо alias wiki-ссылки, либо обычный текст
                display = (m.group(2) or m.group(3) or "").strip()
                if display and not display.startswith("%%"):
                    columns[col_type].append(display)
            i += 2

        return {k: v for k, v in columns.items() if v}

    def build_context(self) -> dict:
        """Собрать полный контекст для LLM: заметки + статус канбана."""
        index = self.scan()
        kanban = self.parse_kanban()

        projects_context = {}
        for project_name, notes in index.projects.items():
            project_notes = []
            for note in sorted(notes, key=lambda n: n.modified_at, reverse=True):
                content = self.read_content(note)
                if not content.strip():
                    continue
                project_notes.append({
                    "path": note.relative_path,
                    "modified_days_ago": round((time.time() - note.modified_at) / 86400, 1),
                    "size_kb": round(note.size_bytes / 1024, 1),
                    "content": content,
                })
            if project_notes:
                projects_context[project_name] = project_notes

        return {
            "projects": projects_context,
            "kanban": kanban,
            "total_notes": index.total_notes,
            "vault_root": str(self.root),
        }
