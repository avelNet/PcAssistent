"""
focus_store.py — управление режимом фокуса.

Фокус — это указание системе работать только с одним проектом.
Состояние хранится в JSON-файле, переживает перезапуски.
"""

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_FOCUS_FILE = Path("~/.local/share/pc-assistant/focus.json").expanduser()


def get_focus() -> str | None:
    """Вернуть название активного проекта-фокуса или None."""
    try:
        if _FOCUS_FILE.exists():
            data = json.loads(_FOCUS_FILE.read_text())
            return data.get("project") or None
    except Exception:
        pass
    return None


def set_focus(project: str | None) -> None:
    """Установить фокус на проект или сбросить (project=None / 'off')."""
    try:
        _FOCUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not project or project.lower() == "off":
            _FOCUS_FILE.write_text(json.dumps({"project": None}))
            logger.info("Фокус снят — все проекты активны")
        else:
            _FOCUS_FILE.write_text(json.dumps({
                "project": project,
                "since": time.time(),
            }))
            logger.info("Фокус установлен → %s", project)
    except Exception as e:
        logger.warning("focus_store: не удалось сохранить фокус: %s", e)
