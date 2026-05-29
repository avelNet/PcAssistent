"""
client_factory.py — возвращает нужный LLM клиент по конфигу.

Использование в orchestrator:
    from llm.client_factory import create_llm_client
    llm = create_llm_client(config)

Конфиг:
    llm:
      provider: "auto"       # Groq → OpenRouter → Ollama (рекомендуется)
      provider: "groq"       # только Groq
      provider: "openrouter" # только OpenRouter
      provider: "ollama"     # только Ollama (локально)
"""

import asyncio
import logging

logger = logging.getLogger(__name__)


class ChainLLMClient:
    """
    Пробует провайдеров по цепочке: первый доступный выигрывает.
    Интерфейс идентичен отдельным клиентам: complete(), check_availability(), close().
    """

    def __init__(self, clients: list, names: list[str]) -> None:
        self._clients = clients
        self._names = names
        self._active: int = 0  # индекс текущего рабочего провайдера

    @property
    def model(self) -> str:
        return getattr(self._clients[self._active], "model", "unknown")

    async def check_availability(self) -> bool:
        """Проверить доступность всех провайдеров, выбрать первый рабочий."""
        for i, (client, name) in enumerate(zip(self._clients, self._names)):
            try:
                ok = await client.check_availability()
                if ok:
                    if i != self._active:
                        logger.info("LLM chain: активный провайдер → %s", name)
                        self._active = i
                    return True
                logger.info("LLM chain: %s недоступен, пробуем следующий", name)
            except Exception as e:
                logger.warning("LLM chain: ошибка проверки %s — %s", name, e)
        logger.warning("LLM chain: ни один провайдер не доступен")
        return False

    async def complete(self, system_prompt: str, user_prompt: str) -> tuple[str, dict]:
        """
        Выполнить запрос через активного провайдера.
        При ошибке — переключиться на следующего.
        """
        start_idx = self._active

        for offset in range(len(self._clients)):
            i = (start_idx + offset) % len(self._clients)
            client = self._clients[i]
            name = self._names[i]

            try:
                result = await client.complete(system_prompt, user_prompt)
                if i != self._active:
                    logger.info("LLM chain: успешно через %s, запоминаем", name)
                    self._active = i
                return result
            except Exception as e:
                logger.warning("LLM chain: %s ошибка — %s, пробуем следующий", name, e)

        raise RuntimeError(
            f"LLM chain: все провайдеры ({', '.join(self._names)}) недоступны"
        )

    async def close(self) -> None:
        await asyncio.gather(*(c.close() for c in self._clients), return_exceptions=True)


def create_llm_client(config: dict):
    """
    Создать LLM клиент согласно config.llm.provider.
    Все клиенты имеют одинаковый интерфейс:
      async def complete(system_prompt, user_prompt) -> tuple[str, dict]
      async def check_availability() -> bool
      async def close() -> None
    """
    provider = config.get("llm", {}).get("provider", "ollama")

    if provider == "groq":
        from llm.groq_client import GroqClient
        client = GroqClient(config)
        logger.info("LLM: используем Groq [модель=%s]", client.model)
        return client

    if provider == "openrouter":
        from llm.openrouter_client import OpenRouterClient
        client = OpenRouterClient(config)
        logger.info("LLM: используем OpenRouter [модель=%s]", client.model)
        return client

    if provider == "ollama":
        from llm.ollama_client import OllamaClient
        client = OllamaClient(config)
        logger.info("LLM: используем Ollama [модель=%s]", client.model)
        return client

    if provider == "auto":
        # Лёгкий chain: OpenRouter → Groq → Ollama (офлайн fallback)
        from llm.groq_client import GroqClient
        from llm.openrouter_client import OpenRouterClient
        from llm.ollama_client import OllamaClient
        clients = [OpenRouterClient(config), GroqClient(config), OllamaClient(config)]
        names   = ["openrouter", "groq", "ollama"]
        chain = ChainLLMClient(clients, names)
        logger.info("LLM: используем chain [openrouter → groq → ollama]")
        return chain

    if provider == "heavy":
        # Тяжёлый chain: Groq → OpenRouter → Ollama (офлайн fallback)
        from llm.groq_client import GroqClient
        from llm.openrouter_client import OpenRouterClient
        from llm.ollama_client import OllamaClient
        clients = [GroqClient(config), OpenRouterClient(config), OllamaClient(config)]
        names   = ["groq", "openrouter", "ollama"]
        chain = ChainLLMClient(clients, names)
        logger.info("LLM heavy: используем chain [groq → openrouter → ollama]")
        return chain

    if provider == "ollama":
        from llm.ollama_client import OllamaClient
        client = OllamaClient(config)
        logger.info("LLM: используем Ollama [модель=%s]", client.model)
        return client

    raise ValueError(
        f"Неизвестный LLM провайдер: '{provider}'. "
        "Допустимые значения: 'auto', 'heavy', 'groq', 'openrouter', 'ollama'"
    )
