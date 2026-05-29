"""
vault_reader.py — динамический сканер Obsidian vault'ов.

Ключевой принцип: не лить всё в контекст LLM, а выбирать только релевантное.

Сигналы релевантности (в порядке приоритета):
  1. Kanban "В работе"  — заметки по активным задачам читаются полностью
  2. Git активность    — заметки связанные с файлами из последних коммитов
  3. Свежие изменения  — заметки изменённые за последние N дней
  4. Структура         — для неактивных проектов только список файлов

Новый проект в ~/Obsidian — подхватывается автоматически через inotify.
"""

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ObsidianNote:
    path: Path
    relative_path: str
    project: str
    size_bytes: int
    modified_at: float
    content: str = ""


@dataclass
class VaultIndex:
    projects: dict[str, list[ObsidianNote]] = field(default_factory=dict)
    total_notes: int = 0
    scanned_at: float = field(default_factory=time.time)


class VaultReader:
    def __init__(self, config: dict):
        obs_cfg = config.get("obsidian", {})
        # shared_root — папка со всеми проектами (~Obsidian/ProjectA, ~/Obsidian/ProjectB)
        # root — путь к активному vault для inotify watcher, не для сканирования
        self.root = Path(obs_cfg.get("shared_root", obs_cfg.get("root", "~/Obsidian"))).expanduser()
        self.skip_folders = set(obs_cfg.get("skip_folders", [".obsidian", "Templates", "Daily"]))
        self.max_note_size_kb = obs_cfg.get("max_note_size_kb", 10)
        self.preview_chars = obs_cfg.get("preview_chars", 600)
        self.recent_days = obs_cfg.get("recent_days", 3)

        # Заметки которые всегда читаем полностью (статус/навигация проекта)
        self._status_note_keywords = {
            "kanban", "dashboard", "roadmap", "knowledge map",
            "readme", "index", "overview", "карта знаний"
        }

    # ─── Публичный интерфейс ────────────────────────────────────────────────

    def build_context(
        self,
        git_snapshots: list[dict] | None = None,
        focus_project: str | None = None,
    ) -> dict:
        """
        Собрать умный контекст для LLM.
        focus_project — если задан, включаем только заметки этого проекта.
        """
        index = self.scan()
        kanban = self.parse_kanban()

        git_keywords = self._extract_git_keywords(git_snapshots or [])
        selected = self._select_relevant(index, kanban, git_keywords)

        # Фильтруем по фокус-проекту: другие проекты убираем полностью
        if focus_project and focus_project in selected:
            selected = {focus_project: selected[focus_project]}
            kanban   = {k: v for k, v in kanban.items() if k == focus_project}
        elif focus_project and focus_project not in selected:
            # Фокус-проект не найден в vault — оставляем пустым, не подмешиваем чужое
            selected = {}
            kanban   = {}

        total_selected = sum(len(v) for v in selected.values())
        total_all = index.total_notes
        logger.info(
            "VaultReader: отобрано %d/%d заметок (in_progress + git + recent + status)",
            total_selected, total_all
        )

        return {
            "projects": selected,
            "kanban": kanban,
            "total_notes": total_all,
            "selected_notes": total_selected,
            "vault_root": str(self.root),
        }

    def parse_kanban(self) -> dict:
        """Найти и распарсить все канбан-доски в vault'е."""
        if not self.root.exists():
            return {}

        kanban_files = []
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

        result = {}
        for kb_file in kanban_files:
            project = kb_file.parent.parent.name
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

    def get_structure_summary(self) -> str:
        """Краткое описание структуры vault'а (только пути)."""
        index = self.scan()
        lines = [f"Obsidian vault: {self.root}"]
        for project_name, notes in sorted(index.projects.items()):
            lines.append(f"\n📁 {project_name}/ ({len(notes)} заметок)")
            recent = sorted(notes, key=lambda n: n.modified_at, reverse=True)[:10]
            for note in recent:
                days = (time.time() - note.modified_at) / 86400
                age = f"{int(days)}д назад" if days >= 1 else "сегодня"
                rel = note.relative_path.replace(f"{project_name}/", "", 1)
                lines.append(f"  - {rel} ({age})")
        return "\n".join(lines)

    # ─── Умный отбор заметок ────────────────────────────────────────────────

    def _select_relevant(
        self,
        index: VaultIndex,
        kanban: dict,
        git_keywords: set[str],
    ) -> dict[str, list[dict]]:
        """
        Для каждого проекта отбирает только релевантные заметки.

        Приоритет:
          HIGH   — заметка совпадает с "В работе" на канбане
          MEDIUM — заметка изменена за recent_days или совпадает с git ключевыми словами
          LOW    — статусная заметка (Dashboard, Kanban, Knowledge Map)
          SKIP   — всё остальное → только путь в структуре, без содержимого
        """
        recent_cutoff = time.time() - self.recent_days * 86400
        result: dict[str, list[dict]] = {}

        # In-progress items из канбана для этого vault'а
        in_progress_tokens: set[str] = set()
        for cols in kanban.values():
            for item in cols.get("in_progress", []):
                in_progress_tokens.update(self._tokenize(item))

        for project_name, notes in index.projects.items():
            selected_notes = []
            skipped_paths = []

            for note in sorted(notes, key=lambda n: n.modified_at, reverse=True):
                note_tokens = self._tokenize(Path(note.relative_path).stem)
                note_name_lower = Path(note.relative_path).name.lower()

                # Сигнал 1: совпадение с канбаном "В работе"
                is_wip = bool(in_progress_tokens & note_tokens)

                # Сигнал 2: недавно изменена
                is_recent = note.modified_at >= recent_cutoff

                # Сигнал 3: связана с git активностью
                is_git_related = bool(git_keywords & note_tokens)

                # Сигнал 4: статусная заметка (всегда читаем)
                is_status = any(
                    kw in note_name_lower for kw in self._status_note_keywords
                )

                if is_wip or is_recent or is_git_related or is_status:
                    content = self.read_content(note)
                    priority = (
                        "wip" if is_wip else
                        "recent" if is_recent else
                        "git" if is_git_related else
                        "status"
                    )
                    selected_notes.append({
                        "path": note.relative_path,
                        "modified_days_ago": round((time.time() - note.modified_at) / 86400, 1),
                        "size_kb": round(note.size_bytes / 1024, 1),
                        "content": content,
                        "priority": priority,
                    })
                else:
                    skipped_paths.append(note.relative_path)

            if skipped_paths:
                logger.debug(
                    "VaultReader: %s — пропущено %d заметок (не в работе, не свежие)",
                    project_name, len(skipped_paths)
                )

            if selected_notes:
                result[project_name] = selected_notes
            elif skipped_paths:
                # Проект есть но ничего не отобрано — добавляем только структуру
                result[project_name] = [{
                    "path": f"{project_name}/ (структура)",
                    "modified_days_ago": 0,
                    "size_kb": 0,
                    "content": f"Заметок: {len(skipped_paths)}. Канбан: ничего не в работе.",
                    "priority": "structure_only",
                }]

        return result

    def _tokenize(self, text: str) -> set[str]:
        """
        Разбить строку на значимые токены для fuzzy-матчинга.
        "Сервис 01 — Инфраструктура" → {"01", "инфраструктура", "сервис"}
        "Service 01 — Infrastructure" → {"01", "infrastructure", "service"}
        """
        # Извлекаем числа и слова длиннее 2 символов
        tokens = re.findall(r'\d+|[a-zа-яё]{3,}', text.lower())
        return set(tokens)

    def _extract_git_keywords(self, git_snapshots: list[dict]) -> set[str]:
        """
        Извлечь ключевые слова из git контекста.
        Используются для поиска связанных заметок в Obsidian.
        """
        keywords: set[str] = set()
        for repo in git_snapshots:
            # Из названия репозитория
            keywords.update(self._tokenize(repo.get("name", "")))
            # Из последнего коммита
            last = repo.get("last_commit", {})
            if last.get("message"):
                keywords.update(self._tokenize(last["message"]))
            # Из TODO/FIXME
            for todo in repo.get("todos", []):
                keywords.update(self._tokenize(todo))
        # Убираем слишком общие слова
        stop_words = {"add", "fix", "the", "and", "for", "not", "this", "that",
                      "добавить", "исправить", "обновить", "для", "это", "файл"}
        return keywords - stop_words

    # ─── Сканирование файловой системы ──────────────────────────────────────

    def scan(self) -> VaultIndex:
        index = VaultIndex()
        if not self.root.exists():
            logger.warning("VaultReader: папка не найдена: %s", self.root)
            return index

        for entry in sorted(self.root.iterdir()):
            if entry.is_dir() and not entry.name.startswith("."):
                notes = self._scan_project(entry)
                if notes:
                    index.projects[entry.name] = notes
                    index.total_notes += len(notes)
            elif entry.is_file() and entry.suffix == ".md":
                note = self._read_note_meta(entry, project="root")
                if note:
                    index.projects.setdefault("root", []).append(note)
                    index.total_notes += 1

        logger.info("VaultReader: найдено %d проектов, %d заметок",
                    len(index.projects), index.total_notes)
        return index

    def _scan_project(self, project_dir: Path) -> list[ObsidianNote]:
        notes = []
        for md_file in self._walk_md_files(project_dir):
            note = self._read_note_meta(md_file, project=project_dir.name)
            if note:
                notes.append(note)
        return notes

    def _walk_md_files(self, base: Path):
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
        try:
            stat = path.stat()
            return ObsidianNote(
                path=path,
                relative_path=str(path.relative_to(self.root)),
                project=project,
                size_bytes=stat.st_size,
                modified_at=stat.st_mtime,
            )
        except (OSError, ValueError):
            return None

    def read_content(self, note: ObsidianNote) -> str:
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

    # ─── Парсинг канбана ────────────────────────────────────────────────────

    def _parse_kanban_file(self, path: Path) -> dict:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {}

        section_re = re.compile(r"^##\s+(.+)$", re.MULTILINE)
        item_re = re.compile(
            r"^\s*-\s+\[([xX ]?)\]\s+(?:\[\[(?:[^\]|]+)(?:\|([^\]]+))?\]\]|(.+))$",
            re.MULTILINE
        )

        def classify(header: str) -> str:
            h = header.lower()
            if any(w in h for w in ["в работе", "in progress", "doing", "🔄"]):
                return "in_progress"
            if any(w in h for w in ["готово", "done", "завершено", "✅", "complete"]):
                return "done"
            if any(w in h for w in ["не начат", "todo", "backlog", "📋", "to do"]):
                return "not_started"
            return "other"

        columns: dict[str, list[str]] = {
            "in_progress": [], "not_started": [], "done": [], "other": []
        }

        sections = section_re.split(content)
        i = 1
        while i < len(sections) - 1:
            header = sections[i].strip()
            body = sections[i + 1]
            col_type = classify(header)
            for m in item_re.finditer(body):
                display = (m.group(2) or m.group(3) or "").strip()
                if display and not display.startswith("%%"):
                    columns[col_type].append(display)
            i += 2

        return {k: v for k, v in columns.items() if v}
