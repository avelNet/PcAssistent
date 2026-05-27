"""
client_factory.py — возвращает нужный LLM клиент по конфигу.

Использование в orchestrator:
    from llm.client_factory import create_llm_client
    ollama = create_llm_client(config)

Конфиг:
    llm:
      provider: "ollama"       # или "openrouter"
"""

import logging

logger = logging.getLogger(__name__)


def create_llm_client(config: dict):
    """
    Создать LLM клиент согласно config.llm.provider.
    Возвращает OllamaClient или OpenRouterClient — оба имеют одинаковый интерфейс:
      async def complete(system_prompt, user_prompt) -> tuple[str, dict]
      async def check_availability() -> bool
      async def close() -> None
    """
    provider = config.get("llm", {}).get("provider", "ollama")

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

    raise ValueError(
        f"Неизвестный LLM провайдер: '{provider}'. "
        "Допустимые значения: 'ollama', 'openrouter'"
    )
