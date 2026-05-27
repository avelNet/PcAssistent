"""
openrouter_client.py — клиент к OpenRouter API (OpenAI-совместимый).
Документация: https://openrouter.ai/docs

Режимы выбора модели:
  model: "auto"   — автоматически выбирает лучшую доступную бесплатную модель
  model: "..."    — конкретная модель (напр. "meta-llama/llama-3.1-8b-instruct:free")

При "auto" — при каждом старте запрашивает /api/v1/models, фильтрует
бесплатные (price == 0) и выбирает лучшую по: размер контекста → предпочитаемые
семейства моделей.
"""

import asyncio
import json
import logging
import time
from typing import AsyncIterator

import aiohttp

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Семейства моделей в порядке предпочтения (чем раньше в списке — тем лучше)
_PREFERRED_FAMILIES = [
    "llama-3",
    "llama-3.1",
    "llama-3.2",
    "llama-3.3",
    "gemma-2",
    "gemma-3",
    "qwen",
    "mistral",
    "phi-3",
    "phi-4",
]

# Минимальный контекст — не берём совсем маленькие модели
_MIN_CONTEXT = 8000


class OpenRouterError(Exception):
    pass


class OpenRouterClient:
    def __init__(self, config: dict) -> None:
        cfg = config.get("openrouter", {})
        self.api_key: str = cfg.get("api_key", "")
        self._model_config: str = cfg.get("model", "auto")
        self.model: str = self._model_config  # будет обновлён при auto
        self.temperature: float = cfg.get("temperature", 0.3)
        self.max_tokens: int = cfg.get("max_tokens", 1024)
        self.timeout_s: int = cfg.get("timeout_s", 60)
        self._session: aiohttp.ClientSession | None = None
        self._free_models_cache: list[dict] | None = None

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

    # ─── Авто-выбор бесплатной модели ───────────────────────────────────────

    async def fetch_free_models(self) -> list[dict]:
        """
        Получить список всех бесплатных моделей с OpenRouter.
        Бесплатная = prompt_price == 0 AND completion_price == 0.
        """
        if self._free_models_cache is not None:
            return self._free_models_cache

        session = await self._get_session()
        try:
            async with session.get(
                f"{OPENROUTER_BASE_URL}/models",
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.warning("OpenRouter: не удалось получить список моделей (HTTP %d)", resp.status)
                    return []
                data = await resp.json()
        except Exception as e:
            logger.warning("OpenRouter: ошибка получения моделей — %s", e)
            return []

        free = []
        for m in data.get("data", []):
            pricing = m.get("pricing", {})
            try:
                prompt_price = float(pricing.get("prompt", "1") or "1")
                completion_price = float(pricing.get("completion", "1") or "1")
            except (ValueError, TypeError):
                continue

            if prompt_price == 0 and completion_price == 0:
                ctx = m.get("context_length", 0) or 0
                if ctx >= _MIN_CONTEXT:
                    free.append({
                        "id": m["id"],
                        "name": m.get("name", m["id"]),
                        "context_length": ctx,
                    })

        self._free_models_cache = free
        logger.info("OpenRouter: найдено %d бесплатных моделей с контекстом ≥%d", len(free), _MIN_CONTEXT)
        return free

    def _pick_best(self, models: list[dict]) -> str | None:
        """
        Выбрать лучшую модель из списка.
        Критерии (по убыванию важности):
          1. Предпочитаемое семейство (llama-3 > gemma-2 > qwen > ...)
          2. Размер контекстного окна (больше = лучше)
        """
        if not models:
            return None

        def score(m: dict) -> tuple[int, int]:
            model_id = m["id"].lower()
            family_score = len(_PREFERRED_FAMILIES)  # худший score по умолчанию
            for i, family in enumerate(_PREFERRED_FAMILIES):
                if family in model_id:
                    family_score = i
                    break
            return (family_score, -m["context_length"])  # меньше family_score = лучше

        best = min(models, key=score)
        return best["id"]

    async def _resolve_model(self) -> str:
        """Вернуть итоговое имя модели (авто-выбор или явное из конфига)."""
        if self._model_config != "auto":
            return self._model_config

        free_models = await self.fetch_free_models()
        picked = self._pick_best(free_models)

        if picked:
            if picked != self.model:
                logger.info("OpenRouter: авто-выбор модели → %s", picked)
                self.model = picked
            return picked

        # Фолбэк если API не ответил
        fallback = "meta-llama/llama-3.1-8b-instruct:free"
        logger.warning("OpenRouter: не удалось получить список моделей, используем %s", fallback)
        self.model = fallback
        return fallback

    # ─── Проверка доступности ────────────────────────────────────────────────

    async def check_availability(self) -> bool:
        """Проверить что API ключ валиден и разрешить модель."""
        if not self.api_key or self.api_key == "YOUR_KEY_HERE":
            logger.warning("OpenRouter: api_key не задан в config.local.yaml")
            return False
        try:
            model = await self._resolve_model()
            logger.info("OpenRouter: модель = %s", model)
            return True
        except Exception as e:
            logger.warning("OpenRouter: ошибка проверки — %s", e)
            return False

    # ─── Запросы к LLM ──────────────────────────────────────────────────────

    async def complete(self, system_prompt: str, user_prompt: str) -> tuple[str, dict]:
        """
        Отправить промпт и дождаться полного ответа.
        Возвращает (текст ответа, метаданные: tokens, duration).
        """
        if not self.api_key or self.api_key == "YOUR_KEY_HERE":
            raise OpenRouterError(
                "OpenRouter: api_key не задан. "
                "Добавь в config.local.yaml: openrouter.api_key"
            )

        model = await self._resolve_model()
        session = await self._get_session()
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        logger.info("OpenRouter: отправляю запрос [модель=%s]", model)
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
                    raise OpenRouterError("Недостаточно кредитов (402). Используй :free модель.")
                if resp.status != 200:
                    raise OpenRouterError(f"HTTP {resp.status}: {body[:300]}")

                data = json.loads(body)

        except aiohttp.ClientConnectorError:
            raise OpenRouterError("OpenRouter недоступен. Проверь интернет-соединение.")
        except aiohttp.ClientError as e:
            raise OpenRouterError(f"Сетевая ошибка: {e}")

        duration = time.monotonic() - start

        # Обработка ошибки от API внутри 200-ответа (bывает у некоторых моделей)
        if "error" in data:
            err = data["error"]
            raise OpenRouterError(f"API ошибка: {err.get('message', err)}")

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
            "model": data.get("model", model),
        }

        return content, meta

    async def stream(self, system_prompt: str, user_prompt: str) -> AsyncIterator[str]:
        """Стриминговый режим — yields токены по мере генерации."""
        if not self.api_key or self.api_key == "YOUR_KEY_HERE":
            raise OpenRouterError("OpenRouter: api_key не задан")

        model = await self._resolve_model()
        session = await self._get_session()
        payload = {
            "model": model,
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
