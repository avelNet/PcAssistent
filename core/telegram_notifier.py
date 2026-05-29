"""
core/telegram_notifier.py — отправка HIGH-задач в Telegram.

Настройка в config.local.yaml:
  telegram:
    bot_token: "123456:ABC..."   # от @BotFather
    chat_id: "123456789"         # твой chat_id (узнать: написать боту /start, потом
                                  # открыть api.telegram.org/bot{TOKEN}/getUpdates)
    enabled: true

Когда срабатывает:
  - При любом LLM-цикле с voice=True если есть задачи HIGH-приоритета
  - Только если десктоп неактивен (заблокирован или idle > 5 мин)
"""

import logging
from typing import TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_TG_API = "https://api.telegram.org/bot{token}/sendMessage"


async def send_telegram(
    tasks: list[dict],
    prologue: str = "",
    trigger: str = "",
    config: dict | None = None,
) -> None:
    """
    Отправить HIGH-задачи в Telegram.
    Вызывается из trigger_engine после LLM если есть HIGH-задачи.
    """
    if not config:
        return

    tg = config.get("telegram", {})
    if not tg.get("enabled"):
        return

    token   = tg.get("bot_token", "").strip()
    chat_id = str(tg.get("chat_id", "")).strip()
    if not token or not chat_id:
        logger.debug("telegram: bot_token или chat_id не настроены")
        return

    high = [t for t in tasks if t.get("priority") == "HIGH"]
    if not high:
        return

    # Формируем сообщение
    lines = [f"🤖 *PC Assistant* — {trigger}"]
    if prologue:
        short = prologue.strip()[:200]
        lines.append(f"\n_{short}_")

    lines.append("\n🔴 *Срочные задачи:*")
    for task in high[:5]:
        title = task.get("title", "")
        proj  = task.get("project", "")
        proj_str = f" `[{proj}]`" if proj else ""
        lines.append(f"• {title}{proj_str}")

    if len(high) > 5:
        lines.append(f"_...и ещё {len(high) - 5} срочных_")

    text = "\n".join(lines)

    url = _TG_API.format(token=token)
    payload = {
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": "Markdown",
    }

    try:
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.post(
                url, json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    logger.info("telegram: отправлено %d HIGH-задач", len(high))
                else:
                    body = await resp.text()
                    logger.warning("telegram: ошибка %d — %s", resp.status, body[:100])
    except Exception as e:
        logger.warning("telegram: не удалось отправить — %s", e)
