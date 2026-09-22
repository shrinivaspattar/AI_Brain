from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.services.chat_attachment_service import (
    AttachmentTextExtractionError,
    AttachmentTooLargeError,
    ChatAttachmentService,
)


def _make_db_that_actually_stores(monkeypatch, saved: dict):
    """A MagicMock db whose add()/commit()/refresh() cooperate enough for
    ChatAttachmentService.save() to behave like it would against a real
    session, and whose query(...).filter(...).all() reads back whatever was
    added - covers save() and get_many() without a real database."""
    db = MagicMock()

    def add(obj):
        saved[obj.id] = obj

    db.add.side_effect = add
    db.refresh.side_effect = lambda obj: None

    def query(_model):
        q = MagicMock()

        def filt(_expr):
            q2 = MagicMock()
            q2.all.return_value = list(saved.values())
            return q2

        q.filter.side_effect = filt
        return q

    db.query.side_effect = query
    return db


def test_save_extracts_text_and_stores_the_file(tmp_path, monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "CHAT_UPLOADS_DIR", tmp_path)
    saved = {}
    db = _make_db_that_actually_stores(monkeypatch, saved)

    attachment = ChatAttachmentService(db).save("notes.txt", b"hello world")

    assert attachment.original_filename == "notes.txt"
    assert attachment.extracted_text == "hello world"
    assert attachment.truncated is False
    assert attachment.byte_size == len(b"hello world")
    assert Path(attachment.stored_path).read_bytes() == b"hello world"


def test_save_rejects_a_path_traversal_filename(tmp_path, monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "CHAT_UPLOADS_DIR", tmp_path)
    saved = {}
    db = _make_db_that_actually_stores(monkeypatch, saved)

    attachment = ChatAttachmentService(db).save("../../etc/passwd", b"not actually passwd")

    # only the final path component is ever used - it cannot escape its own upload folder
    assert attachment.original_filename == "passwd"
    assert str(tmp_path) in attachment.stored_path
    assert Path(attachment.stored_path).is_file()


def test_save_rejects_a_file_over_the_size_limit(tmp_path, monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "CHAT_UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(settings, "MAX_CHAT_ATTACHMENT_BYTES", 10)
    db = MagicMock()

    with pytest.raises(AttachmentTooLargeError):
        ChatAttachmentService(db).save("big.txt", b"x" * 11)

    db.add.assert_not_called()


def test_save_truncates_text_over_the_char_limit(tmp_path, monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "CHAT_UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(settings, "MAX_CHAT_ATTACHMENT_TEXT_CHARS", 5)
    saved = {}
    db = _make_db_that_actually_stores(monkeypatch, saved)

    attachment = ChatAttachmentService(db).save("notes.txt", b"hello world")

    assert attachment.extracted_text == "hello"
    assert attachment.truncated is True


def test_save_raises_when_the_file_cannot_be_read_as_text(tmp_path, monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "CHAT_UPLOADS_DIR", tmp_path)
    db = MagicMock()
    # a genuinely undecodable binary blob - not valid UTF-8, no extractor for .bin
    binary_content = bytes(range(256))

    with pytest.raises(AttachmentTextExtractionError):
        ChatAttachmentService(db).save("photo.bin", binary_content)

    db.add.assert_not_called()


def test_get_many_preserves_order_and_drops_unknown_ids(tmp_path, monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "CHAT_UPLOADS_DIR", tmp_path)
    saved = {}
    db = _make_db_that_actually_stores(monkeypatch, saved)
    service = ChatAttachmentService(db)
    a = service.save("a.txt", b"aaa")
    b = service.save("b.txt", b"bbb")

    result = service.get_many([b.id, "does-not-exist", a.id])

    assert [r.id for r in result] == [b.id, a.id]


def test_get_many_returns_empty_list_for_no_ids() -> None:
    assert ChatAttachmentService(MagicMock()).get_many([]) == []
