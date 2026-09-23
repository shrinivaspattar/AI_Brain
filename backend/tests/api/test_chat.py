from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.db.session import get_db
from app.embeddings.client import EmbeddingUnavailableError
from app.main import app
from app.services.chat_client import ChatUnavailableError


def test_send_message_returns_reply_with_citations() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.chat.ChatService") as service_class:
            message = MagicMock()
            message.id = 2
            message.conversation_id = "conv-1"
            message.role = "assistant"
            message.content = "According to [1], AI_Brain is offline-first."
            message.citations = [
                {
                    "document_chunk_id": 1,
                    "document_id": "doc-1",
                    "document_title": "notes.txt",
                    "document_source": "/documents/notes.txt",
                }
            ]
            message.created_at = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)

            service_class.return_value.send_message.return_value = message

            client = TestClient(app)

            response = client.post(
                "/chat",
                json={"message": "what is AI_Brain?"},
            )

            assert response.status_code == 200
            assert response.json() == {
                "conversation_id": "conv-1",
                "message": {
                    "id": 2,
                    "conversation_id": "conv-1",
                    "role": "assistant",
                    "content": "According to [1], AI_Brain is offline-first.",
                    "citations": [
                        {
                            "document_chunk_id": 1,
                            "document_id": "doc-1",
                            "document_title": "notes.txt",
                            "document_source": "/documents/notes.txt",
                            "source_occurrences": None,
                        }
                    ],
                    "created_at": "2026-08-12T10:00:00Z",
                },
            }

            service_class.return_value.send_message.assert_called_once_with(
                "what is AI_Brain?",
                conversation_id=None,
                top_k=5,
                attachment_ids=[],
                web_search=False,
            )

    finally:
        app.dependency_overrides.clear()


def test_send_message_returns_not_found_for_unknown_conversation() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.chat.ChatService") as service_class:
            service_class.return_value.send_message.side_effect = ValueError(
                "Conversation conv-404 not found"
            )

            client = TestClient(app)

            response = client.post(
                "/chat",
                json={"message": "hi", "conversation_id": "conv-404"},
            )

            assert response.status_code == 404
            assert response.json() == {
                "detail": "Conversation conv-404 not found"
            }

    finally:
        app.dependency_overrides.clear()


def test_send_message_returns_service_unavailable_when_ollama_fails() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.chat.ChatService") as service_class:
            service_class.return_value.send_message.side_effect = (
                ChatUnavailableError("connection refused")
            )

            client = TestClient(app)

            response = client.post("/chat", json={"message": "hi"})

            assert response.status_code == 503
            assert "connection refused" in response.json()["detail"]

    finally:
        app.dependency_overrides.clear()


def test_send_message_returns_service_unavailable_when_embedding_fails() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.chat.ChatService") as service_class:
            service_class.return_value.send_message.side_effect = (
                EmbeddingUnavailableError("connection refused")
            )

            client = TestClient(app)

            response = client.post("/chat", json={"message": "hi"})

            assert response.status_code == 503
            assert response.json() == {
                "detail": "Embedding model unavailable: connection refused"
            }

    finally:
        app.dependency_overrides.clear()


def test_get_conversation_messages_returns_history() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        conversation = MagicMock()
        db.get.return_value = conversation

        message = MagicMock()
        message.id = 1
        message.conversation_id = "conv-1"
        message.role = "user"
        message.content = "hello"
        message.citations = None
        message.created_at = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)

        db.scalars.return_value = [message]

        client = TestClient(app)

        response = client.get("/chat/conv-1")

        assert response.status_code == 200
        assert response.json() == [
            {
                "id": 1,
                "conversation_id": "conv-1",
                "role": "user",
                "content": "hello",
                "citations": None,
                "created_at": "2026-08-12T10:00:00Z",
            }
        ]

    finally:
        app.dependency_overrides.clear()


def test_get_conversation_messages_returns_not_found() -> None:
    db = MagicMock()
    db.get.return_value = None

    app.dependency_overrides[get_db] = lambda: db

    try:
        client = TestClient(app)

        response = client.get("/chat/does-not-exist")

        assert response.status_code == 404

    finally:
        app.dependency_overrides.clear()


# ---- selectable chat model ----

from app.api.chat import resolve_chat_model  # noqa: E402
from app.core.config import settings  # noqa: E402


def test_resolve_chat_model_passes_through_an_allowed_name() -> None:
    assert resolve_chat_model("qwen3:4b") in settings.available_chat_models()
    assert resolve_chat_model("qwen3:4b") == "qwen3:4b"


def test_resolve_chat_model_falls_back_for_none_or_unknown_names() -> None:
    assert resolve_chat_model(None) is None
    assert resolve_chat_model("") is None
    assert resolve_chat_model("some-model-nobody-configured") is None


def test_list_available_models_returns_the_configured_allowlist_and_default() -> None:
    response = TestClient(app).get("/chat/models")

    assert response.status_code == 200
    body = response.json()
    assert body["default"] == settings.CHAT_MODEL
    assert settings.CHAT_MODEL in body["models"]
    assert body["models"] == settings.available_chat_models()


def test_send_message_uses_the_requested_model() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.chat.ChatService") as service_class, patch("app.api.chat.ChatClient") as client_class:
            message = MagicMock()
            message.id = 2
            message.conversation_id = "conv-1"
            message.role = "assistant"
            message.content = "hi"
            message.citations = None
            message.created_at = datetime.now(UTC)
            service_class.return_value.send_message.return_value = message

            response = TestClient(app).post(
                "/chat", json={"message": "hi", "model": "qwen3:4b"}
            )

        assert response.status_code == 200
        client_class.assert_called_once_with(model="qwen3:4b")
    finally:
        app.dependency_overrides.clear()


def test_send_message_ignores_an_unknown_model_name() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.chat.ChatService") as service_class, patch("app.api.chat.ChatClient") as client_class:
            message = MagicMock()
            message.id = 2
            message.conversation_id = "conv-1"
            message.role = "assistant"
            message.content = "hi"
            message.citations = None
            message.created_at = datetime.now(UTC)
            service_class.return_value.send_message.return_value = message

            TestClient(app).post("/chat", json={"message": "hi", "model": "not-a-real-model"})

        client_class.assert_called_once_with(model=None)
    finally:
        app.dependency_overrides.clear()


# ---- conversation list (sidebar) ----

def test_list_conversations_returns_most_recently_active_first() -> None:
    from datetime import UTC, datetime, timedelta

    from app.db.session import get_db as real_get_db  # noqa: F401  (imported for clarity only)
    from app.models.conversation import Conversation

    older = Conversation(
        id="conv-old", title="Older chat",
        created_at=datetime.now(UTC) - timedelta(hours=2),
        updated_at=datetime.now(UTC) - timedelta(hours=2),
    )
    newer = Conversation(
        id="conv-new", title="Newer chat",
        created_at=datetime.now(UTC) - timedelta(hours=1),
        updated_at=datetime.now(UTC),
    )

    db = MagicMock()
    db.scalars.return_value = [newer, older]  # already the order a real ORDER BY desc would give
    app.dependency_overrides[get_db] = lambda: db

    try:
        response = TestClient(app).get("/chat/conversations")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert [c["id"] for c in body] == ["conv-new", "conv-old"]
    assert body[0]["title"] == "Newer chat"


# ---- file/document attachments ----

def test_upload_attachment_returns_extracted_text_summary(tmp_path, monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "CHAT_UPLOADS_DIR", tmp_path)
    db = MagicMock()

    with patch("app.api.chat.ChatAttachmentService") as service_class:
        saved = MagicMock()
        saved.id = "att-1"
        saved.original_filename = "notes.txt"
        saved.byte_size = 11
        saved.extracted_text = "hello world"
        saved.truncated = False
        service_class.return_value.save.return_value = saved

        app.dependency_overrides[get_db] = lambda: db
        try:
            response = TestClient(app).post(
                "/chat/attachments",
                files={"file": ("notes.txt", b"hello world", "text/plain")},
            )
        finally:
            app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "id": "att-1",
        "filename": "notes.txt",
        "byte_size": 11,
        "extracted_chars": 11,
        "truncated": False,
    }


def test_upload_attachment_reports_a_file_too_large_as_422() -> None:
    from app.services.chat_attachment_service import AttachmentTooLargeError

    app.dependency_overrides[get_db] = lambda: MagicMock()
    try:
        with patch("app.api.chat.ChatAttachmentService") as service_class:
            service_class.return_value.save.side_effect = AttachmentTooLargeError("too big")
            response = TestClient(app).post(
                "/chat/attachments",
                files={"file": ("big.txt", b"x", "text/plain")},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert response.json()["detail"] == "too big"


def test_upload_attachment_reports_unreadable_content_as_422() -> None:
    from app.services.chat_attachment_service import AttachmentTextExtractionError

    app.dependency_overrides[get_db] = lambda: MagicMock()
    try:
        with patch("app.api.chat.ChatAttachmentService") as service_class:
            service_class.return_value.save.side_effect = AttachmentTextExtractionError("nope")
            response = TestClient(app).post(
                "/chat/attachments",
                files={"file": ("photo.bin", b"\x00\x01", "application/octet-stream")},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert response.json()["detail"] == "nope"


def test_send_message_passes_attachment_ids_through() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.chat.ChatService") as service_class:
            message = MagicMock()
            message.id = 2
            message.conversation_id = "conv-1"
            message.role = "assistant"
            message.content = "hi"
            message.citations = None
            message.created_at = datetime.now(UTC)
            service_class.return_value.send_message.return_value = message

            TestClient(app).post("/chat", json={"message": "hi", "attachment_ids": ["att-1", "att-2"]})

        service_class.return_value.send_message.assert_called_once_with(
            "hi", conversation_id=None, top_k=5, attachment_ids=["att-1", "att-2"], web_search=False
        )
    finally:
        app.dependency_overrides.clear()


def test_send_message_passes_web_search_through() -> None:
    db = MagicMock()
    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.chat.ChatService") as service_class:
            message = MagicMock()
            message.id = 2
            message.conversation_id = "conv-1"
            message.role = "assistant"
            message.content = "hi"
            message.citations = None
            message.created_at = datetime.now(UTC)
            service_class.return_value.send_message.return_value = message

            TestClient(app).post("/chat", json={"message": "what's new?", "web_search": True})

        service_class.return_value.send_message.assert_called_once_with(
            "what's new?", conversation_id=None, top_k=5, attachment_ids=[], web_search=True
        )
    finally:
        app.dependency_overrides.clear()


def test_list_available_models_reports_web_search_enabled(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "WEB_SEARCH_ENABLED", True)

    response = TestClient(app).get("/chat/models")

    assert response.json()["web_search_enabled"] is True


def test_list_available_models_reports_voice_enabled(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "VOICE_ENABLED", True)

    response = TestClient(app).get("/chat/models")

    assert response.json()["voice_enabled"] is True


def test_transcribe_audio_returns_503_when_voice_disabled(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "VOICE_ENABLED", False)

    response = TestClient(app).post(
        "/chat/transcribe",
        files={"file": ("recording.webm", b"fake-audio-bytes", "audio/webm")},
    )

    assert response.status_code == 503


def test_transcribe_audio_returns_text_when_enabled(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "VOICE_ENABLED", True)

    with patch("app.api.chat.TranscriptionService") as service_class:
        service_class.return_value.transcribe.return_value = "hello world"

        response = TestClient(app).post(
            "/chat/transcribe",
            files={"file": ("recording.webm", b"fake-audio-bytes", "audio/webm")},
        )

        assert response.status_code == 200
        assert response.json() == {"text": "hello world"}
        service_class.return_value.transcribe.assert_called_once_with(b"fake-audio-bytes", suffix=".webm")


def test_transcribe_audio_returns_503_on_transcription_failure(monkeypatch) -> None:
    from app.core.config import settings
    from app.services.transcription_service import TranscriptionUnavailableError

    monkeypatch.setattr(settings, "VOICE_ENABLED", True)

    with patch("app.api.chat.TranscriptionService") as service_class:
        service_class.return_value.transcribe.side_effect = TranscriptionUnavailableError("boom")

        response = TestClient(app).post(
            "/chat/transcribe",
            files={"file": ("recording.webm", b"fake-audio-bytes", "audio/webm")},
        )

        assert response.status_code == 503


def test_speak_text_returns_503_when_voice_disabled(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "VOICE_ENABLED", False)

    response = TestClient(app).post("/chat/speak", json={"text": "hello"})

    assert response.status_code == 503


def test_speak_text_returns_audio_when_enabled(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "VOICE_ENABLED", True)

    with patch("app.api.chat.SpeechService") as service_class:
        service_class.return_value.synthesize.return_value = b"RIFF....WAVEfmt "

        response = TestClient(app).post("/chat/speak", json={"text": "hello there"})

        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/wav"
        assert response.content == b"RIFF....WAVEfmt "
        service_class.return_value.synthesize.assert_called_once_with("hello there")


def test_speak_text_returns_503_on_synthesis_failure(monkeypatch) -> None:
    from app.core.config import settings
    from app.services.speech_service import SpeechUnavailableError

    monkeypatch.setattr(settings, "VOICE_ENABLED", True)

    with patch("app.api.chat.SpeechService") as service_class:
        service_class.return_value.synthesize.side_effect = SpeechUnavailableError("boom")

        response = TestClient(app).post("/chat/speak", json={"text": "hello"})

        assert response.status_code == 503
