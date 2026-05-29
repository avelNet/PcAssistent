"""
groq_client.py — клиент к Groq API (OpenAI-совместимый).
Base URL: https://api.groq.com/openai/v1
Free tier: 1000 RPD, 6000 TPM. Ответ ~2 сек на LPU-железе.

Модели (в порядке приоритета при 429):
  llama-3.3-70b-versatile  — 1000 RPD, 6000 TPM, лучшее качество
  llama-3.1-8b-instant     — 14400 RPD, 20000 TPM, быстрее при перегрузке
"""

import json
import logging
import time

import aiohttp

logger = logging.getLogger(__name__)

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
_FALLBACK_MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]


class GroqError(Exception):
    pass


class GroqClient:
    def __init__(self, config: dict) -> None:
        cfg = config.get("groq", {})
        self.api_key: str = cfg.get("api_key", "")
        self.model: str = cfg.get("model", _FALLBACK_MODELS[0])
        self.temperature: float = cfg.get("temperature", 0.3)
        self.max_tokens: int = cfg.get("max_tokens", 1024)
        self.timeout_s: int = cfg.get("timeout_s", 30)
        self._session: aiohttp.ClientSession | None = None

    def _is_configured(self) -> bool:
        return bool(self.api_key) and self.api_key not in ("YOUR_KEY_HERE", "")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout_s)
            self._session = aiohttp.ClientSession(timeout=timeout, trust_env=True)
        return self._session

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def check_availability(self) -> bool:
        """Проверить что ключ задан и API отвечает."""
        if not self._is_configured():
            logger.warning("Groq: api_key не задан в config.local.yaml")
            return False
        try:
            session = await self._get_session()
            async with session.get(
                f"{GROQ_BASE_URL}/models",
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 401:
                    logger.warning("Groq: неверный api_key (401)")
                    return False
                ok = resp.status == 200
                if ok:
                    logger.info("Groq: доступен [модель=%s]", self.model)
                return ok
        except Exception as e:
            logger.warning("Groq: недоступен — %s", e)
            return False

    async def complete(self, system_prompt: str, user_prompt: str) -> tuple[str, dict]:
        """
        Отправить промпт, получить ответ.
        При 429 пробует следующую модель из _FALLBACK_MODELS.
        Возвращает (текст, метаданные).
        """
        if not self._is_configured():
            raise GroqError("Groq: api_key не задан. Добавь в config.local.yaml: groq.api_key")

        # Список: текущая модель первой, остальные fallback
        models = [self.model] + [m for m in _FALLBACK_MODELS if m != self.model]
        last_error: Exception | None = None

        for attempt, model in enumerate(models):
            if attempt > 0:
                logger.warning("Groq: %s вернул 429, пробую %s", models[attempt - 1], model)

            result = await self._try_complete(system_prompt, user_prompt, model)

            if isinstance(result, Exception):
                last_error = result
                if "429" in str(result):
                    continue
                raise result
            else:
                if model != self.model:
                    logger.info("Groq: успешно с моделью %s", model)
                return result

        raise last_error or GroqError("Groq: все модели вернули 429. Попробуй позже.")

    async def _try_complete(
        self, system_prompt: str, user_prompt: str, model: str
    ) -> tuple[str, dict] | Exception:
        """Одна попытка запроса. Возвращает результат или Exception."""
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

        logger.info("Groq: запрос [модель=%s]", model)
        start = time.monotonic()

        try:
            async with session.post(
                f"{GROQ_BASE_URL}/chat/completions",
                json=payload,
                headers=self._headers(),
            ) as resp:
                body = await resp.text()

                if resp.status == 401:
                    return GroqError("Неверный API ключ (401). Проверь groq.api_key")
                if resp.status == 429:
                    return GroqError(f"429: rate limit для {model}")
                if resp.status != 200:
                    return GroqError(f"HTTP {resp.status}: {body[:200]}")

                data = json.loads(body)

        except aiohttp.ClientConnectorError:
            return GroqError("Groq недоступен. Проверь интернет-соединение.")
        except aiohttp.ClientError as e:
            return GroqError(f"Сетевая ошибка: {e}")

        if "error" in data:
            err = data["error"]
            code = err.get("code", "")
            if code == "rate_limit_exceeded" or "429" in str(code):
                return GroqError(f"429: rate limit для {model}")
            return GroqError(f"API ошибка: {err.get('message', err)}")

        duration = time.monotonic() - start
        choice = data.get("choices", [{}])[0]
        content = choice.get("message", {}).get("content", "")
        usage = data.get("usage", {})

        logger.info(
            "Groq: ответ за %.1fс, токены: prompt=%d completion=%d",
            duration,
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
        )

        return content, {
            "duration_s": round(duration, 2),
            "tokens_prompt": usage.get("prompt_tokens", 0),
            "tokens_eval": usage.get("completion_tokens", 0),
            "tokens_total": usage.get("total_tokens", 0),
            "model": data.get("model", model),
        }

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
