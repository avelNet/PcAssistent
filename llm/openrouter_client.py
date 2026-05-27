"""
openrouter_client.py — клиент к OpenRouter API (OpenAI-совместимый).
Документация: https://openrouter.ai/docs

Бесплатные модели (на момент разработки):
  meta-llama/llama-3.1-8b-instruct:free
  meta-llama/llama-3.2-3b-instruct:free
  google/gemma-2-9b-it:free
  mistralai/mistral-7b-instruct:free
  qwen/qwen-2-7b-instruct:free
  microsoft/phi-3-mini-128k-instruct:free

Полный список: https://openrouter.ai/models?q=free
"""

import asyncio
import json
import logging
import time
from typing import AsyncIterator

import aiohttp

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterError(Exception):
    pass


class OpenRouterClient:
    def __init__(self, config: dict) -> None:
        cfg = config.get("openrouter", {})
        self.api_key: str = cfg.get("api_key", "")
        self.model: str = cfg.get("model", "meta-llama/llama-3.1-8b-instruct:free")
        self.temperature: float = cfg.get("temperature", 0.3)
        self.max_tokens: int = cfg.get("max_tokens", 1024)
        self.timeout_s: int = cfg.get("timeout_s", 120)  # облако быстрее CPU
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout_s)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/avelNet/PcAssistent",
            "X-Title": "PC Assistant",
        }

    async def check_availability(self) -> bool:
        """Проверить что API ключ валиден и модель доступна."""
        if not self.api_key or self.api_key == "YOUR_KEY_HERE":
            logger.warning("OpenRouter: api_key не задан в config.yaml")
            return False
        try:
            session = await self._get_session()
            async with session.get(
                f"{OPENROUTER_BASE_URL}/models",
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 401:
                    logger.error("OpenRouter: неверный API ключ (401)")
                    return False
                if resp.status != 200:
                    logger.warning("OpenRouter: /models вернул %d", resp.status)
                    return False
                data = await resp.json()
                models = [m["id"] for m in data.get("data", [])]
                if self.model not in models:
                    logger.warning(
                        "OpenRouter: модель '%s' не найдена. Проверь https://openrouter.ai/models",
                        self.model
                    )
                    # Не блокируем — модель может быть валидна даже если не в списке
                return True
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("OpenRouter: проверка недоступна — %s", e)
            return False

    async def complete(self, system_prompt: str, user_prompt: str) -> tuple[str, dict]:
        """
        Отправить промпт и дождаться полного ответа.
        Возвращает (текст ответа, метаданные: tokens, duration).
        """
        if not self.api_key or self.api_key == "YOUR_KEY_HERE":
            raise OpenRouterError(
                "OpenRouter: api_key не задан. "
                "Получи ключ на https://openrouter.ai/keys и укажи в config.yaml"
            )

        session = await self._get_session()
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        logger.info("OpenRouter: отправляю запрос [модель=%s]", self.model)
        start = time.monotonic()

        try:
            async with session.post(
                f"{OPENROUTER_BASE_URL}/chat/completions",
                json=payload,
                headers=self._headers(),
            ) as resp:
                body = await resp.text()

                if resp.status == 401:
                    raise OpenRouterError("Неверный API ключ (401). Проверь openrouter.api_key")
                if resp.status == 429:
                    raise OpenRouterError("Превышен лимит запросов (429). Подожди немного.")
                if resp.status == 402:
                    raise OpenRouterError("Недостаточно кредитов (402). Пополни баланс или используй :free модель.")
                if resp.status != 200:
                    raise OpenRouterError(f"HTTP {resp.status}: {body[:300]}")

                data = json.loads(body)

        except aiohttp.ClientConnectorError:
            raise OpenRouterError("OpenRouter недоступен. Проверь интернет-соединение.")
        except aiohttp.ClientError as e:
            raise OpenRouterError(f"Сетевая ошибка: {e}")

        duration = time.monotonic() - start

        choice = data.get("choices", [{}])[0]
        content = choice.get("message", {}).get("content", "")

        usage = data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)

        logger.info(
            "OpenRouter: ответ за %.1fс, токены: prompt=%d completion=%d",
            duration, prompt_tokens, completion_tokens
        )

        meta = {
            "duration_s": round(duration, 2),
            "tokens_prompt": prompt_tokens,
            "tokens_eval": completion_tokens,
            "tokens_total": prompt_tokens + completion_tokens,
            "model": data.get("model", self.model),
        }

        return content, meta

    async def stream(self, system_prompt: str, user_prompt: str) -> AsyncIterator[str]:
        """Стриминговый режим — yields токены по мере генерации."""
        if not self.api_key or self.api_key == "YOUR_KEY_HERE":
            raise OpenRouterError("OpenRouter: api_key не задан")

        session = await self._get_session()
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": True,
        }

        try:
            async with session.post(
                f"{OPENROUTER_BASE_URL}/chat/completions",
                json=payload,
                headers=self._headers(),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise OpenRouterError(f"HTTP {resp.status}: {body[:200]}")

                async for line in resp.content:
                    line = line.strip()
                    if not line or line == b"data: [DONE]":
                        continue
                    if line.startswith(b"data: "):
                        try:
                            chunk = json.loads(line[6:])
                            token = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                            if token:
                                yield token
                        except json.JSONDecodeError:
                            continue

        except aiohttp.ClientConnectorError:
            raise OpenRouterError("OpenRouter недоступен. Проверь интернет-соединение.")

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
