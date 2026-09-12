from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from app.models.document import Document
from app.models.import_job import ImportJob
from app.models.message import Message, MessageRole
from app.provenance.service import ProvenanceService


def test_trace_document_raises_for_missing_document() -> None:
    db = MagicMock()
    db.get.return_value = None

    service = ProvenanceService(db)

    with pytest.raises(ValueError, match="not found"):
        service.trace_document("missing-id")


def test_trace_document_returns_full_chain() -> None:
    db = MagicMock()

    document = Document(
        id="doc-1",
        title="a.txt",
        source="/documents/a.txt",
        source_type="txt",
        import_job_id=5,
    )
    import_job = ImportJob(
        id=5, name="Job", source_path="/src", source_type="filesystem"
    )

    def get_side_effect(model, pk):
        if model is Document:
            return document
        if model is ImportJob:
            return import_job
        return None

    db.get.side_effect = get_side_effect
    db.scalar.return_value = 3

    citing_message = Message(
        id=10,
        conversation_id="conv-1",
        role=MessageRole.ASSISTANT,
        content="reply",
        citations=[{"document_id": "doc-1", "document_chunk_id": 1}],
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    unrelated_message = Message(
        id=11,
        conversation_id="conv-2",
        role=MessageRole.ASSISTANT,
        content="other reply",
        citations=[{"document_id": "doc-2"}],
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    db.scalars.return_value = [citing_message, unrelated_message]

    service = ProvenanceService(db)
    provenance = service.trace_document("doc-1")

    assert provenance.document is document
    assert provenance.import_job is import_job
    assert provenance.chunk_count == 3
    assert len(provenance.cited_in) == 1
    assert provenance.cited_in[0].message_id == 10
    assert provenance.cited_in[0].conversation_id == "conv-1"


def test_trace_document_handles_no_import_job_and_no_citations() -> None:
    db = MagicMock()

    document = Document(
        id="doc-1",
        title="a.txt",
        source="/documents/a.txt",
        source_type="txt",
        import_job_id=None,
    )
    db.get.return_value = document
    db.scalar.return_value = 0
    db.scalars.return_value = []

    service = ProvenanceService(db)
    provenance = service.trace_document("doc-1")

    assert provenance.import_job is None
    assert provenance.chunk_count == 0
    assert provenance.cited_in == []


def test_trace_document_tolerates_json_null_citations() -> None:
    """Regression test: a JSONB column storing Python None round-trips as
    a JSON `null`, not a SQL NULL - so `Message.citations.is_not(None)`
    at the query level doesn't guarantee every returned row's
    `.citations` is non-None. The scan must re-check in Python instead
    of assuming the query already filtered these out.
    """
    db = MagicMock()

    document = Document(
        id="doc-1", title="a.txt", source="/documents/a.txt", source_type="txt"
    )
    db.get.return_value = document
    db.scalar.return_value = 0

    message_with_null_citations = Message(
        id=10,
        conversation_id="conv-1",
        role=MessageRole.ASSISTANT,
        content="no citations here",
        citations=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db.scalars.return_value = [message_with_null_citations]

    service = ProvenanceService(db)
    provenance = service.trace_document("doc-1")

    assert provenance.cited_in == []


def test_trace_document_ignores_zero_chunk_count_of_none() -> None:
    db = MagicMock()

    document = Document(
        id="doc-1",
        title="a.txt",
        source="/documents/a.txt",
        source_type="txt",
    )
    db.get.return_value = document
    db.scalar.return_value = None
    db.scalars.return_value = []

    service = ProvenanceService(db)
    provenance = service.trace_document("doc-1")

    assert provenance.chunk_count == 0
