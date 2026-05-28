"""
ui/tray_app.py — GTK системный трей через AppIndicator3.

Требует системные пакеты:
  sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1

Если пакеты не установлены — тихо отключается (enabled=False в config.yaml).

⚠️  Работает только на X11. На Wayland AppIndicator3 недоступен.

Меню:
  📋 Задачи (N/M выполнено)
  ─────────────────────────
  🔴 Задача 1  [ ]
  🟡 Задача 2  [ ]
  🟢 Задача 3  [x]
  ─────────────────────────
  🔄 Обновить задачи
  🔊 Озвучить задачи
  📊 Статистика дня
  ─────────────────────────
  Выход
"""

import asyncio
import logging
import threading
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from core.event_bus import EventBus

logger = logging.getLogger(__name__)

_PRIORITY_EMOJI = {"HIGH": "🔴", "MED": "🟡", "LOW": "🟢"}


class TrayApp:
    """
    GTK трей-индикатор.
    Запускается в отдельном потоке (GTK main loop не совместим с asyncio).
    Связь с asyncio через asyncio.Queue.
    """

    def __init__(self, config: dict, bus: "EventBus"):
        ui_cfg = config.get("ui", {})
        self._enabled:     bool = ui_cfg.get("enabled", False)
        self._refresh_sec: int  = ui_cfg.get("tray_refresh_sec", 30)
        self.bus = bus

        self._tasks_data: list[dict] = []
        self._indicator = None
        self._menu = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._async_task: Optional[asyncio.Task] = None

        # GTK модули — загружаем лениво
        self._gi_available = False

    def subscribe(self) -> None:
        if not self._enabled:
            return
        self.bus.on("llm.completed",          self._on_llm_completed)
        self.bus.on("obsidian.tasks_updated",  self._on_tasks_updated)

    async def start(self) -> None:
        if not self._enabled:
            logger.debug("TrayApp: отключён в конфиге (ui.enabled=false)")
            return

        # На Wayland AppIndicator3 требует gnome-shell-extension-appindicator
        # и без него дёргает дашборд GNOME — лучше не запускать
        import os
        if os.environ.get("XDG_SESSION_TYPE") == "wayland":
            logger.info(
                "TrayApp: Wayland-сессия — AppIndicator пропускаем "
                "(требует gnome-shell-extension-appindicator)"
            )
            return

        if not self._check_gi():
            logger.warning(
                "TrayApp: PyGObject недоступен — установи: "
                "sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1"
            )
            return

        self._loop = asyncio.get_event_loop()

        # GTK main loop в отдельном потоке
        self._thread = threading.Thread(
            target=self._gtk_thread, daemon=True, name="tray_gtk"
        )
        self._thread.start()

        # Периодическое обновление меню
        self._async_task = asyncio.create_task(
            self._refresh_loop(), name="tray_refresh"
        )
        logger.info("TrayApp: запущен")

    def stop(self) -> None:
        if self._async_task:
            self._async_task.cancel()
        if self._gi_available:
            try:
                import gi
                gi.require_version("Gtk", "3.0")
                from gi.repository import Gtk
                Gtk.main_quit()
            except Exception:
                pass
        logger.info("TrayApp: остановлен")

    # ─── GTK поток ───────────────────────────────────────────────────────────

    def _check_gi(self) -> bool:
        try:
            import gi
            gi.require_version("Gtk", "3.0")
            gi.require_version("AyatanaAppIndicator3", "0.1")
            from gi.repository import Gtk, AyatanaAppIndicator3  # noqa: F401
            self._gi_available = True
            return True
        except (ImportError, ValueError, Exception):
            return False

    def _gtk_thread(self) -> None:
        try:
            import gi
            gi.require_version("Gtk", "3.0")
            gi.require_version("AyatanaAppIndicator3", "0.1")
            from gi.repository import Gtk, AyatanaAppIndicator3

            self._indicator = AyatanaAppIndicator3.Indicator.new(
                "pc-assistant",
                "dialog-information",
                AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS,
            )
            self._indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
            self._indicator.set_title("PC Assistant")

            self._menu = Gtk.Menu()
            self._build_menu(Gtk, self._menu)
            self._indicator.set_menu(self._menu)

            Gtk.main()
        except Exception as e:
            logger.error("TrayApp: ошибка GTK — %s", e)

    def _build_menu(self, Gtk, menu) -> None:
        """Построить/обновить меню. Вызывается из GTK-потока."""
        # Очищаем
        for child in menu.get_children():
            menu.remove(child)

        tasks = self._tasks_data
        done_count  = sum(1 for t in tasks if t.get("done"))
        total_count = len(tasks)

        # Заголовок
        header = Gtk.MenuItem(label=f"📋 Задачи ({done_count}/{total_count} выполнено)")
        header.set_sensitive(False)
        menu.append(header)
        menu.append(Gtk.SeparatorMenuItem())

        # Задачи (топ-6)
        for task in tasks[:6]:
            done  = task.get("done", False)
            emoji = _PRIORITY_EMOJI.get(task.get("priority", ""), "⚪")
            mark  = "[x]" if done else "[ ]"
            title = task.get("title", "")[:40]
            item  = Gtk.MenuItem(label=f"{emoji} {title}  {mark}")
            item.set_sensitive(False)
            menu.append(item)

        menu.append(Gtk.SeparatorMenuItem())

        # Действия
        refresh_item = Gtk.MenuItem(label="🔄 Обновить задачи")
        refresh_item.connect("activate", self._on_refresh_click)
        menu.append(refresh_item)

        speak_item = Gtk.MenuItem(label="🔊 Озвучить задачи")
        speak_item.connect("activate", self._on_speak_click)
        menu.append(speak_item)

        stats_item = Gtk.MenuItem(label="📊 Статистика дня")
        stats_item.connect("activate", self._on_stats_click)
        menu.append(stats_item)

        menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label="Выход")
        quit_item.connect("activate", self._on_quit_click)
        menu.append(quit_item)

        menu.show_all()

    def _rebuild_menu_safe(self) -> None:
        """Перестроить меню из GTK-потока (через GLib.idle_add)."""
        if not self._gi_available or not self._menu:
            return
        try:
            import gi
            gi.require_version("Gtk", "3.0")
            from gi.repository import Gtk, GLib
            GLib.idle_add(self._build_menu, Gtk, self._menu)
        except Exception as e:
            logger.debug("TrayApp: rebuild_menu ошибка — %s", e)

    # ─── Обработчики кликов (GTK-поток) ─────────────────────────────────────

    def _on_refresh_click(self, *_) -> None:
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self.bus.emit("trigger.llm", {"reason": "tray_refresh", "voice": False}),
                self._loop,
            )

    def _on_speak_click(self, *_) -> None:
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self.bus.emit("trigger.llm", {"reason": "tray_speak", "voice": True}),
                self._loop,
            )

    def _on_stats_click(self, *_) -> None:
        logger.info("TrayApp: статистика дня запрошена из трея")

    def _on_quit_click(self, *_) -> None:
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self.bus.emit("app.quit", {}),
                self._loop,
            )

    # ─── Обработчики событий шины (asyncio-поток) ────────────────────────────

    async def _on_llm_completed(self, data: dict) -> None:
        if not data:
            return
        tasks = data.get("tasks", [])
        if tasks:
            self._tasks_data = tasks
            self._rebuild_menu_safe()

    async def _on_tasks_updated(self, data: dict) -> None:
        if not data:
            return
        tasks = data.get("tasks", [])
        if tasks:
            self._tasks_data = tasks
            self._rebuild_menu_safe()

    # ─── Периодическое обновление ────────────────────────────────────────────

    async def _refresh_loop(self) -> None:
        """Периодически запрашиваем актуальные задачи из БД."""
        while True:
            try:
                await asyncio.sleep(self._refresh_sec)
                await self._load_tasks_from_db()
                self._rebuild_menu_safe()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug("TrayApp: ошибка refresh_loop", exc_info=True)

    async def _load_tasks_from_db(self) -> None:
        from storage import context_store
        try:
            tasks = await context_store.get_tasks_for_date()
            if tasks:
                self._tasks_data = tasks
        except Exception as e:
            logger.debug("TrayApp: ошибка загрузки задач — %s", e)
