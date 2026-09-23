from __future__ import annotations

import tempfile

from faster_whisper import WhisperModel

from app.core.config import settings

# Loading a WhisperModel takes ~30-40s on this CPU-only machine - far too
# slow to redo per request. Cached at module level (keyed by model size) so
# every TranscriptionService instance in a running process reuses the same
# loaded model, the same way Ollama keeps a model resident between calls.
_model_cache: dict[str, WhisperModel] = {}


def _get_model(model_size: str) -> WhisperModel:
    if model_size not in _model_cache:
        _model_cache[model_size] = WhisperModel(model_size, device="cpu", compute_type="int8")
    return _model_cache[model_size]


class TranscriptionUnavailableError(RuntimeError):
    """Raised when the recorded audio can't be decoded or transcribed."""


class TranscriptionService:
    """One-shot voice-to-text for the chat composer, via faster-whisper.
    Entirely local - no network call, unlike web search - and the audio
    itself is never persisted: it exists only in a temp file for the
    duration of one transcribe() call, then is deleted. Not part of the
    Chain 1/2 ingestion pipeline or the searchable knowledge base, same
    separation of concerns as ChatAttachment."""

    def __init__(self, model_size: str | None = None, whisper_model: WhisperModel | None = None):
        self.model_size = model_size or settings.STT_MODEL_SIZE
        self._whisper_model = whisper_model

    def _model(self) -> WhisperModel:
        if self._whisper_model is not None:
            return self._whisper_model
        return _get_model(self.model_size)

    def transcribe(self, audio_bytes: bytes, suffix: str = ".webm") -> str:
        if not audio_bytes:
            return ""

        with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
            tmp.write(audio_bytes)
            tmp.flush()

            try:
                segments, _info = self._model().transcribe(tmp.name, beam_size=5)
                return " ".join(segment.text.strip() for segment in segments).strip()
            except Exception as exc:  # noqa: BLE001 - ctranslate2/av raise several distinct error types
                raise TranscriptionUnavailableError(str(exc)) from exc
