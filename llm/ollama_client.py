"""
ollama_client.py — async HTTP клиент к Ollama.
Документация API: https://github.com/ollama/ollama/blob/main/docs/api.md
"""

import asyncio
import json
import logging
import time
from typing import AsyncIterator

import aiohttp

logger = logging.getLogger(__name__)


class OllamaError(Exception):
    pass


class OllamaClient:
    def __init__(self, config: dict) -> None:
        cfg = config.get("ollama", {})
        self.host = cfg.get("host", "http://localhost:11434")
        self.model = cfg.get("model", "qwen2.5:14b")
        self.keep_alive = cfg.get("keep_alive", "10m")
        self.num_ctx = cfg.get("num_ctx", 8192)
        self.num_predict = cfg.get("num_predict", 1024)
        self.temperature = cfg.get("temperature", 0.3)
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=300)  # 5 мин макс
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def check_availability(self) -> bool:
        """Проверить что Ollama запущена и модель доступна."""
        try:
            session = await self._get_session()
            async with session.get(f"{self.host}/api/tags", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
                models = [m["name"] for m in data.get("models", [])]
                # Проверяем по префиксу (qwen2.5:14b и qwen2.5:14b-instruct-q4 — одно и то же)
                model_base = self.model.split(":")[0]
                available = any(m.startswith(model_base) for m in models)
                if not available:
                    logger.warning(
                        "Ollama: модель '%s' не найдена. Доступные: %s",
                        self.model, ", ".join(models) or "нет"
                    )
                return available
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False

    async def complete(self, system_prompt: str, user_prompt: str) -> tuple[str, dict]:
        """
        Отправить промпт и дождаться полного ответа.
        Возвращает (текст ответа, метаданные: tokens, duration).
        """
        session = await self._get_session()
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": self.temperature,
                "num_ctx": self.num_ctx,
                "num_predict": self.num_predict,
            },
        }

        logger.info("Ollama: отправляю запрос [модель=%s, num_ctx=%d]", self.model, self.num_ctx)
        start = time.monotonic()

        try:
            async with session.post(f"{self.host}/api/chat", json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise OllamaError(f"HTTP {resp.status}: {body[:200]}")

                data = await resp.json()

        except aiohttp.ClientConnectorError:
            raise OllamaError(
                f"Ollama недоступна по адресу {self.host}. "
                "Запусти: ollama serve"
            )
        except aiohttp.ClientError as e:
            raise OllamaError(f"Сетевая ошибка: {e}")

        duration = time.monotonic() - start
        content = data.get("message", {}).get("content", "")
        eval_count = data.get("eval_count", 0)
        prompt_count = data.get("prompt_eval_count", 0)

        logger.info(
            "Ollama: ответ получен за %.1fс, токены: prompt=%d eval=%d",
            duration, prompt_count, eval_count
        )

        meta = {
            "duration_s": round(duration, 2),
            "tokens_prompt": prompt_count,
            "tokens_eval": eval_count,
            "tokens_total": prompt_count + eval_count,
            "model": data.get("model", self.model),
        }

        return content, meta

    async def stream(self, system_prompt: str, user_prompt: str) -> AsyncIterator[str]:
        """
        Стриминговый режим — yields токены по мере генерации.
        Удобно для отладки и показа в реальном времени.
        """
        session = await self._get_session()
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": True,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": self.temperature,
                "num_ctx": self.num_ctx,
                "num_predict": self.num_predict,
            },
        }

        try:
            async with session.post(f"{self.host}/api/chat", json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise OllamaError(f"HTTP {resp.status}: {body[:200]}")

                async for line in resp.content:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                        token = chunk.get("message", {}).get("content", "")
                        if token:
                            yield token
                        if chunk.get("done"):
                            break
                    except json.JSONDecodeError:
                        continue

        except aiohttp.ClientConnectorError:
            raise OllamaError(f"Ollama недоступна по адресу {self.host}")

    async def unload_model(self) -> None:
        """Принудительно выгрузить модель из VRAM."""
        session = await self._get_session()
        try:
            payload = {"model": self.model, "keep_alive": "0"}
            async with session.post(f"{self.host}/api/generate", json=payload) as resp:
                if resp.status == 200:
                    logger.info("Ollama: модель выгружена из VRAM")
        except Exception:
            pass

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
