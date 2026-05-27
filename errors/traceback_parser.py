"""
errors/traceback_parser.py — парсит Python traceback в структурированный объект.

Что извлекает:
  error_type   — TypeError / KeyError / ImportError / ...
  message      — текст ошибки
  file         — файл где произошло (последний в стеке)
  line         — номер строки
  function     — имя функции
  full_chain   — весь стек вызовов
  project      — к какому проекту относится (из пути)
"""

import re
from pathlib import Path
from typing import Optional


# Паттерны для парсинга
_TRACEBACK_START = re.compile(r'^Traceback \(most recent call last\):', re.MULTILINE)
_FILE_LINE = re.compile(
    r'^\s+File "(.+?)", line (\d+), in (.+)$', re.MULTILINE
)
_ERROR_LINE = re.compile(
    r'^([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*(?:Error|Exception|Warning|'
    r'KeyboardInterrupt|SystemExit|GeneratorExit))\s*:?\s*(.*)$',
    re.MULTILINE,
)


def parse(text: str, scan_dirs: Optional[list[str]] = None) -> Optional[dict]:
    """
    Распарсить текст трейсбека.
    scan_dirs — список корней проектов для определения project по пути.
    Возвращает None если трейсбек не найден.
    """
    if not _TRACEBACK_START.search(text):
        return None

    # Стек файлов
    frames = [
        {"file": m.group(1), "line": int(m.group(2)), "function": m.group(3)}
        for m in _FILE_LINE.finditer(text)
    ]

    # Последний фрейм — место ошибки
    last_frame = frames[-1] if frames else {}

    # Тип и сообщение ошибки
    error_type = ""
    message = ""
    err_match = _ERROR_LINE.search(text)
    if err_match:
        error_type = err_match.group(1)
        message = err_match.group(2).strip()

    file_path = last_frame.get("file", "")
    line = last_frame.get("line")
    function = last_frame.get("function", "")

    # Определяем проект по пути файла
    project = _detect_project(file_path, scan_dirs or [])

    return {
        "error_type": error_type,
        "message":    message,
        "file":       file_path,
        "line":       line,
        "function":   function,
        "project":    project,
        "full_chain": frames,
        "raw":        text.strip()[-2000:],  # последние 2000 символов
    }


def _detect_project(file_path: str, scan_dirs: list[str]) -> str:
    """Определить имя проекта по пути файла."""
    if not file_path:
        return ""

    p = Path(file_path)

    # Пробуем сопоставить с известными корнями проектов
    for scan_dir in scan_dirs:
        root = Path(scan_dir).expanduser()
        try:
            rel = p.relative_to(root)
            # Первый компонент относительного пути — имя проекта
            parts = rel.parts
            if parts:
                return parts[0]
        except ValueError:
            continue

    # Фолбэк: имя родительской папки
    return p.parent.name if p.parent != p else ""
