from unittest.mock import MagicMock

import pytest

from app.services.transcription_service import TranscriptionService, TranscriptionUnavailableError


def _segment(text: str) -> MagicMock:
    segment = MagicMock()
    segment.text = text
    return segment


def test_transcribe_joins_segment_text() -> None:
    whisper_model = MagicMock()
    whisper_model.transcribe.return_value = ([_segment(" Hello "), _segment("world ")], MagicMock())

    service = TranscriptionService(whisper_model=whisper_model)
    text = service.transcribe(b"fake-audio-bytes")

    assert text == "Hello world"
    _args, kwargs = whisper_model.transcribe.call_args
    assert kwargs["beam_size"] == 5


def test_transcribe_returns_empty_string_for_empty_audio() -> None:
    whisper_model = MagicMock()

    service = TranscriptionService(whisper_model=whisper_model)
    text = service.transcribe(b"")

    assert text == ""
    whisper_model.transcribe.assert_not_called()


def test_transcribe_writes_audio_to_a_temp_file() -> None:
    whisper_model = MagicMock()
    whisper_model.transcribe.return_value = ([], MagicMock())
    captured_path = {}

    def fake_transcribe(path, **kwargs):
        with open(path, "rb") as f:
            captured_path["content"] = f.read()
        return [], MagicMock()

    whisper_model.transcribe.side_effect = fake_transcribe

    service = TranscriptionService(whisper_model=whisper_model)
    service.transcribe(b"some-audio-bytes", suffix=".webm")

    assert captured_path["content"] == b"some-audio-bytes"


def test_transcribe_raises_transcription_unavailable_on_model_error() -> None:
    whisper_model = MagicMock()
    whisper_model.transcribe.side_effect = RuntimeError("decode failed")

    service = TranscriptionService(whisper_model=whisper_model)

    with pytest.raises(TranscriptionUnavailableError):
        service.transcribe(b"fake-audio-bytes")


def test_transcribe_uses_configured_model_size(monkeypatch) -> None:
    from app.services import transcription_service as module

    monkeypatch.setattr(module, "_model_cache", {})
    created_with = {}

    class FakeWhisperModel:
        def __init__(self, size, **kwargs):
            created_with["size"] = size

        def transcribe(self, *args, **kwargs):
            return [], MagicMock()

    monkeypatch.setattr(module, "WhisperModel", FakeWhisperModel)

    service = TranscriptionService(model_size="tiny")
    service.transcribe(b"fake-audio-bytes")

    assert created_with["size"] == "tiny"
