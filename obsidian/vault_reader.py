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
