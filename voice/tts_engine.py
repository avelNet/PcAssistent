"""
voice/tts_engine.py — синтез речи через Piper TTS или espeak-ng.

Pipeline (Piper):
  текст → piper --output-raw → aplay -r 22050 -f S16_LE -t raw -

Fallback (espeak-ng):
  текст → espeak-ng -v ru

Оба варианта полностью офлайн.
"""

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class TTSEngine:
    def __init__(self, config: dict):
        voice_cfg = config.get("voice", {})
        self.enabled:     bool = voice_cfg.get("enabled", False)
        self.engine:      str  = voice_cfg.get("engine", "piper")
        self.model:       str  = voice_cfg.get("model", "ru_RU-ruslan-medium")
        self.model_dir:   Path = Path(voice_cfg.get("model_dir", "~/.local/share/piper")).expanduser()
        self.fallback:    str  = voice_cfg.get("fallback_engine", "espeak-ng")

    def _model_path(self) -> Path:
        """Путь к .onnx модели Piper."""
        return self.model_dir / f"{self.model}.onnx"

    async def speak(self, text: str) -> None:
        """Озвучить текст. Не бросает исключений — ошибка только в лог."""
        if not self.enabled or not text.strip():
            return

        if self.engine == "piper" and self._model_path().exists():
            await self._speak_piper(text)
        else:
            if self.engine == "piper":
                logger.warning(
                    "tts: модель Piper не найдена: %s — используем %s",
                    self._model_path(), self.fallback,
                )
            await self._speak_fallback(text)

    async def _speak_piper(self, text: str) -> None:
        """
        Piper → aplay pipeline.
        echo text | piper --model MODEL --output-raw | aplay -r 22050 -f S16_LE -t raw -
        """
        try:
            piper_proc = await asyncio.create_subprocess_exec(
                "piper",
                "--model", str(self._model_path()),
                "--output-raw",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            aplay_proc = await asyncio.create_subprocess_exec(
                "aplay", "-r", "22050", "-f", "S16_LE", "-t", "raw", "-",
                stdin=piper_proc.stdout,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            # Подаём текст в piper
            await piper_proc.communicate(input=text.encode("utf-8"))
            await asyncio.wait_for(aplay_proc.wait(), timeout=60)
            logger.debug("tts: piper озвучил %d символов", len(text))
        except asyncio.TimeoutError:
            logger.warning("tts: piper timeout")
        except FileNotFoundError as e:
            logger.warning("tts: piper или aplay не найден — %s", e)
        except Exception as e:
            logger.error("tts: piper ошибка — %s", e)

    async def _speak_fallback(self, text: str) -> None:
        """espeak-ng -v ru — запасной вариант."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.fallback, "-v", "ru", text,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=30)
            logger.debug("tts: %s озвучил %d символов", self.fallback, len(text))
        except asyncio.TimeoutError:
            logger.warning("tts: %s timeout", self.fallback)
        except FileNotFoundError:
            logger.warning("tts: %s не установлен. Установи: sudo apt install espeak-ng", self.fallback)
        except Exception as e:
            logger.error("tts: %s ошибка — %s", self.fallback, e)

    async def check_availability(self) -> str:
        """
        Проверить что доступно: 'piper' | 'espeak' | 'none'.
        Используется в --check команде.
        """
        if self._model_path().exists():
            return "piper"
        try:
            proc = await asyncio.create_subprocess_exec(
                self.fallback, "--version",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            return "espeak"
        except FileNotFoundError:
            return "none"
