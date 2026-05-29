"""
voice/salute_tts.py — SberSaluteSpeech TTS движок.

Аутентификация: OAuth2 Bearer token (живёт 30 мин, обновляется автоматически).
Синтез: POST /rest/v1/text:synthesize → WAV24 → aplay.

Доступные голоса:
  Nec_24000 — женский (Наталья)
  Bys_24000 — мужской (Борис)
  May_24000 — женский (Майя)
  Tur_24000 — мужской (Тур)
  Ost_24000 — мужской (Остап)
  Pon_24000 — мужской (Понт)
"""

import asyncio
import logging
import os
import tempfile
import time
import uuid

import aiohttp

logger = logging.getLogger(__name__)

_AUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
_TTS_URL  = "https://smartspeech.sber.ru/rest/v1/text:synthesize"


class SaluteTTS:
    def __init__(self, config: dict):
        cfg = config.get("salute_speech", {})
        self._credentials: str = cfg.get("credentials", "")  # base64 ключ из ЛК
        self._voice:       str = cfg.get("voice", "Nec_24000")
        self._scope:       str = cfg.get("scope", "SALUTE_SPEECH_PERS")

        self._token: str = ""
        self._token_expires_at: int = 0  # миллисекунды epoch

        # Персистентные сессии — TCP соединение переиспользуется между запросами
        self._connector: aiohttp.TCPConnector | None = None
        self._session:   aiohttp.ClientSession | None = None

    # ─── Публичный API ────────────────────────────────────────────────────────

    async def speak(self, text: str) -> None:
        """Синтезировать и воспроизвести текст."""
        if not text.strip():
            return
        tmp_path = None
        try:
            token = await self._get_token()
            audio = await self._synthesize(token, text)

            fd, tmp_str = tempfile.mkstemp(suffix=".wav", prefix="pc-assistant-salute-")
            tmp_path = tmp_str
            with os.fdopen(fd, "wb") as f:
                f.write(audio)

            proc = await asyncio.create_subprocess_exec(
                "aplay", tmp_str,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=60)
            logger.info("salute_tts: озвучено %d симв", len(text))

        except asyncio.TimeoutError:
            logger.warning("salute_tts: aplay timeout")
        except Exception as e:
            logger.error("salute_tts: ошибка — %s", e, exc_info=True)
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    async def warmup(self) -> None:
        """Получить токен и прогреть TCP-соединение к TTS endpoint."""
        try:
            await self._get_token()
            # Прогреваем соединение к smartspeech.sber.ru — синтезируем пустую фразу
            session = await self._get_session()
            headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/text"}
            params  = {"voice": self._voice, "format": "wav16", "language": "ru-RU"}
            async with session.post(
                _TTS_URL, headers=headers, params=params,
                data=" ".encode("utf-8"),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                await resp.read()  # установить соединение, прогреть keep-alive
            logger.info("salute_tts: warmup завершён (токен + TCP соединение готовы)")
        except Exception as e:
            logger.warning("salute_tts: warmup ошибка — %s", e)

    # ─── Сессия ──────────────────────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        """Персистентная сессия — TCP keep-alive между запросами."""
        if self._session is None or self._session.closed:
            self._connector = aiohttp.TCPConnector(ssl=False, limit=4, keepalive_timeout=60)
            self._session   = aiohttp.ClientSession(connector=self._connector)
        return self._session

    async def close(self) -> None:
        """Закрыть сессию при завершении."""
        if self._session and not self._session.closed:
            await self._session.close()

    # ─── Авторизация ─────────────────────────────────────────────────────────

    async def _get_token(self) -> str:
        """Вернуть действующий токен, обновить если истёк (буфер 60 сек)."""
        now_ms = int(time.time() * 1000)
        if self._token and now_ms < self._token_expires_at - 60_000:
            return self._token

        logger.debug("salute_tts: запрашиваю новый токен...")
        # Токен берём отдельной сессией (другой хост — ngw.devices.sberbank.ru)
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            headers = {
                "Authorization":  f"Basic {self._credentials}",
                "RqUID":          str(uuid.uuid4()),
                "Content-Type":   "application/x-www-form-urlencoded",
            }
            async with session.post(
                _AUTH_URL,
                headers=headers,
                data=f"scope={self._scope}",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        self._token = data["access_token"]
        self._token_expires_at = data["expires_at"]
        logger.info(
            "salute_tts: токен получен, истекает через %.0f мин",
            (self._token_expires_at - int(time.time() * 1000)) / 60_000,
        )
        return self._token

    # ─── Синтез ──────────────────────────────────────────────────────────────

    async def _synthesize(self, token: str, text: str) -> bytes:
        """POST текст → получить WAV bytes (переиспользует TCP соединение)."""
        session = await self._get_session()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/text",
        }
        params = {
            "voice":    self._voice,
            "format":   "wav16",
            "language": "ru-RU",
        }
        async with session.post(
            _TTS_URL,
            headers=headers,
            params=params,
            data=text.encode("utf-8"),
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"SaluteSpeech TTS HTTP {resp.status}: {body[:200]}")
            return await resp.read()
