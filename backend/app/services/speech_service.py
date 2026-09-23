from __future__ import annotations

import io

import soundfile as sf
from kokoro_onnx import Kokoro

from app.core.config import settings

# Loading Kokoro reads its ~340MB of model/voice weights from disk - cheap
# compared to Whisper's one-time download (~1s once the files are local),
# but still not something to redo per request. Cached at module level, the
# same pattern as transcription_service._model_cache.
_kokoro_cache: Kokoro | None = None


def _get_kokoro() -> Kokoro:
    global _kokoro_cache
    if _kokoro_cache is None:
        _kokoro_cache = Kokoro(str(settings.TTS_MODEL_PATH), str(settings.TTS_VOICES_PATH))
    return _kokoro_cache


class SpeechUnavailableError(RuntimeError):
    """Raised when the model files are missing or synthesis fails."""


class SpeechService:
    """Text-to-speech for reading an assistant reply aloud, via Kokoro-onnx.
    Entirely local - no network call - and nothing is persisted: audio is
    generated fresh per request and returned directly, the reverse of
    TranscriptionService."""

    def __init__(self, voice: str | None = None, kokoro: Kokoro | None = None):
        self.voice = voice or settings.TTS_VOICE
        self._kokoro = kokoro

    def _model(self) -> Kokoro:
        if self._kokoro is not None:
            return self._kokoro
        return _get_kokoro()

    def synthesize(self, text: str) -> bytes:
        try:
            samples, sample_rate = self._model().create(text, voice=self.voice, speed=1.0, lang="en-us")
            buffer = io.BytesIO()
            sf.write(buffer, samples, sample_rate, format="WAV")
            return buffer.getvalue()
        except Exception as exc:  # noqa: BLE001 - onnxruntime/soundfile raise several distinct error types
            raise SpeechUnavailableError(str(exc)) from exc
