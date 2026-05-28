"""
voice/tts_engine.py — синтез речи.

Поддерживаемые движки:
  supertonic  — нейросетевой TTS (supertone-inc/supertonic), русский голос F1/M1
                модель (~358 MB) скачивается автоматически при первом запуске
  piper       — офлайн Piper TTS, pipeline: piper --output-raw | aplay
  espeak-ng   — запасной вариант, системный пакет

Приоритет при engine="supertonic":
  supertonic → espeak-ng (если импорт не удался)

Приоритет при engine="piper":
  piper (если .onnx файл существует) → espeak-ng
"""

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class TTSEngine:
    def __init__(self, config: dict):
        voice_cfg = config.get("voice", {})
        self.enabled:     bool = voice_cfg.get("enabled", False)
        self.engine:      str  = voice_cfg.get("engine", "supertonic")
        # piper-specific
        self.model:       str  = voice_cfg.get("model", "ru_RU-irina-medium")
        self.model_dir:   Path = Path(voice_cfg.get("model_dir", "~/.local/share/piper")).expanduser()
        # supertonic-specific
        self.voice_style: str  = voice_cfg.get("voice_style", "F1")
        self.lang:        str  = voice_cfg.get("lang", "ru")
        # silero-specific
        self.silero_speaker:     str  = voice_cfg.get("silero_speaker", "xenia")
        self.silero_sample_rate: int  = voice_cfg.get("silero_sample_rate", 48000)
        # fallback
        self.fallback:    str  = voice_cfg.get("fallback_engine", "espeak-ng")

        self._supertonic_instance: Optional[Any] = None
        self._silero_instance:     Optional[Any] = None

    # ─────────────────────────────────────────────────────── public API ──

    async def speak(self, text: str) -> None:
        """Озвучить текст. Не бросает исключений — ошибка только в лог."""
        if not self.enabled or not text.strip():
            return

        if self.engine == "silero":
            await self._speak_silero(text)
        elif self.engine == "supertonic":
            await self._speak_supertonic(text)
        elif self.engine == "piper" and self._model_path().exists():
            await self._speak_piper(text)
        else:
            if self.engine == "piper":
                logger.warning(
                    "tts: модель Piper не найдена: %s — используем %s",
                    self._model_path(), self.fallback,
                )
            await self._speak_fallback(text)

    async def check_availability(self) -> str:
        """
        Проверить что доступно: 'supertonic' | 'piper' | 'espeak' | 'none'.
        Используется в --check команде.
        """
        try:
            import supertonic  # noqa: F401
            return "supertonic"
        except ImportError:
            pass
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

    # ─────────────────────────────────────────────────── silero engine ──

    async def _get_silero(self) -> Any:
        """Lazy-инициализация Silero v4 (модель грузится один раз ~40MB)."""
        if self._silero_instance is None:
            loop = asyncio.get_event_loop()

            def _init():
                import torch
                model, _ = torch.hub.load(
                    repo_or_dir="snakers4/silero-models",
                    model="silero_tts",
                    language="ru",
                    speaker="v4_ru",
                    verbose=False,
                    trust_repo=True,
                )
                return model

            logger.info("tts: инициализация Silero v4 (первый запуск — загрузка модели)...")
            self._silero_instance = await loop.run_in_executor(None, _init)
            logger.info("tts: Silero готов (голос=%s)", self.silero_speaker)
        return self._silero_instance

    async def _speak_silero(self, text: str) -> None:
        """Silero: синтез в thread-executor → WAV → aplay."""
        tmp_path: Optional[Path] = None
        try:
            model = await self._get_silero()
            loop  = asyncio.get_event_loop()

            sr = self.silero_sample_rate
            speaker = self.silero_speaker

            def _synthesize():
                import numpy as np
                import scipy.io.wavfile as wavfile
                import tempfile
                import os

                audio = model.apply_tts(text=text, speaker=speaker, sample_rate=sr)
                data  = (audio.numpy() * 32767).astype(np.int16)
                fd, path = tempfile.mkstemp(suffix=".wav", prefix="pc-assistant-silero-")
                os.close(fd)
                wavfile.write(path, sr, data)
                duration = len(data) / sr
                return path, duration

            tmp_str, dur_sec = await asyncio.wait_for(
                loop.run_in_executor(None, _synthesize),
                timeout=60,
            )
            tmp_path = Path(tmp_str)

            aplay_proc = await asyncio.create_subprocess_exec(
                "aplay", tmp_str,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(aplay_proc.wait(), timeout=dur_sec + 10)
            logger.debug("tts: silero озвучил %d символов (%.1f сек)", len(text), dur_sec)

        except asyncio.TimeoutError:
            logger.warning("tts: silero timeout")
        except ImportError as e:
            logger.warning("tts: silero зависимость не установлена — %s. Fallback.", e)
            await self._speak_fallback(text)
        except FileNotFoundError:
            logger.warning("tts: aplay не найден — sudo apt install alsa-utils")
        except Exception as e:
            logger.error("tts: silero ошибка — %s", e, exc_info=True)
        finally:
            if tmp_path and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    # ──────────────────────────────────────────────── supertonic engine ──

    async def _get_supertonic(self) -> Any:
        """Lazy-инициализация Supertonic TTS (модель грузится один раз)."""
        if self._supertonic_instance is None:
            loop = asyncio.get_event_loop()

            def _init():
                from supertonic import TTS  # type: ignore
                return TTS(auto_download=True)

            logger.info("tts: инициализация Supertonic (первый запуск — загрузка модели ~358 MB)...")
            self._supertonic_instance = await loop.run_in_executor(None, _init)
            logger.info("tts: Supertonic готов")
        return self._supertonic_instance

    async def _speak_supertonic(self, text: str) -> None:
        """
        Supertonic: синтез в thread-executor → WAV во временный файл → aplay.
        Синтез CPU/GPU-bound, поэтому не блокируем event loop.
        """
        tmp_path: Optional[Path] = None
        try:
            tts = await self._get_supertonic()
            loop = asyncio.get_event_loop()

            # 1. Синтез (CPU-bound — в отдельном потоке)
            voice_style = tts.get_voice_style(self.voice_style)

            def _synthesize():
                return tts.synthesize(text=text, voice_style=voice_style, lang=self.lang)

            wav, duration = await asyncio.wait_for(
                loop.run_in_executor(None, _synthesize),
                timeout=120,
            )

            # 2. Сохранить WAV во временный файл
            fd, tmp_str = tempfile.mkstemp(suffix=".wav", prefix="pc-assistant-tts-")
            tmp_path = Path(tmp_str)
            import os
            os.close(fd)

            await loop.run_in_executor(None, lambda: tts.save_audio(wav, str(tmp_path)))

            # 3. Воспроизвести через aplay
            dur_sec: float = duration[0] if hasattr(duration, "__len__") else float(duration)
            aplay_timeout = max(dur_sec + 10, 30)

            aplay_proc = await asyncio.create_subprocess_exec(
                "aplay", str(tmp_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(aplay_proc.wait(), timeout=aplay_timeout)
            logger.debug("tts: supertonic озвучил %d символов (%.1f сек)", len(text), dur_sec)

        except asyncio.TimeoutError:
            logger.warning("tts: supertonic timeout")
        except ImportError:
            logger.warning("tts: supertonic не установлен — pip install supertonic. Fallback на %s", self.fallback)
            await self._speak_fallback(text)
        except FileNotFoundError as e:
            logger.warning("tts: aplay не найден — %s. Установи: sudo apt install alsa-utils", e)
        except Exception as e:
            logger.error("tts: supertonic ошибка — %s", e, exc_info=True)
        finally:
            if tmp_path and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    # ──────────────────────────────────────────────────── piper engine ──

    def _model_path(self) -> Path:
        """Путь к .onnx модели Piper."""
        return self.model_dir / f"{self.model}.onnx"

    async def _speak_piper(self, text: str) -> None:
        """
        Piper → aplay: сначала получаем raw audio из piper, затем воспроизводим через aplay.
        asyncio StreamReader не поддерживает fileno(), поэтому пайп делаем вручную.
        """
        try:
            # 1. Piper: текст → raw PCM
            piper_proc = await asyncio.create_subprocess_exec(
                "piper",
                "--model", str(self._model_path()),
                "--output-raw",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            audio_data, _ = await asyncio.wait_for(
                piper_proc.communicate(input=text.encode("utf-8")),
                timeout=60,
            )

            if not audio_data:
                logger.warning("tts: piper вернул пустой аудио")
                return

            # 2. aplay: воспроизводим raw PCM
            aplay_proc = await asyncio.create_subprocess_exec(
                "aplay", "-r", "22050", "-f", "S16_LE", "-t", "raw", "-",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(
                aplay_proc.communicate(input=audio_data),
                timeout=60,
            )
            logger.debug("tts: piper озвучил %d символов", len(text))
        except asyncio.TimeoutError:
            logger.warning("tts: piper timeout")
        except FileNotFoundError as e:
            logger.warning("tts: piper или aplay не найден — %s", e)
        except Exception as e:
            logger.error("tts: piper ошибка — %s", e)

    # ─────────────────────────────────────────────────── fallback engine ──

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
