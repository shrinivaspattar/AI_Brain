from unittest.mock import MagicMock

import numpy as np
import pytest

from app.services.speech_service import SpeechService, SpeechUnavailableError


def test_synthesize_returns_wav_bytes() -> None:
    kokoro = MagicMock()
    kokoro.create.return_value = (np.zeros(2400, dtype=np.float32), 24000)

    service = SpeechService(kokoro=kokoro)
    audio = service.synthesize("hello there")

    kokoro.create.assert_called_once_with("hello there", voice="af_heart", speed=1.0, lang="en-us")
    assert isinstance(audio, bytes)
    assert audio[:4] == b"RIFF"  # WAV container header


def test_synthesize_uses_configured_voice() -> None:
    kokoro = MagicMock()
    kokoro.create.return_value = (np.zeros(100, dtype=np.float32), 24000)

    service = SpeechService(voice="am_adam", kokoro=kokoro)
    service.synthesize("hi")

    kokoro.create.assert_called_once_with("hi", voice="am_adam", speed=1.0, lang="en-us")


def test_synthesize_raises_speech_unavailable_on_model_error() -> None:
    kokoro = MagicMock()
    kokoro.create.side_effect = RuntimeError("model not found")

    service = SpeechService(kokoro=kokoro)

    with pytest.raises(SpeechUnavailableError):
        service.synthesize("hello")
