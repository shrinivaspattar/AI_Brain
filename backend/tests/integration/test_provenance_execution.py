from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.conversation import Conversation
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.import_job import ImportJob, ImportStatus
from app.models.message import Message, MessageRole
from app.provenance.service import ProvenanceService


def test_trace_document_against_real_database() -> None:
    """Proves the provenance chain actually holds together against a real
    database: ImportJob -> Document -> DocumentChunk, and separately
    Document -> (cited in) -> Message -> Conversation, all traced from a
    single Document row.
    """
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    engine = create_engine(database_url)

    with Session(engine) as db:
        import_job = ImportJob(
            name="Provenance Integration Test",
            source_path="/provenance-test/source",
            source_type="filesystem",
            status=ImportStatus.COMPLETED,
        )
        db.add(import_job)
        db.commit()
        db.refresh(import_job)

        document = Document(
            title="charter.txt",
            source="/provenance-test/charter.txt",
            source_type="txt",
            content_hash="provenance-test-hash",
            import_job_id=import_job.id,
        )
        db.add(document)
        db.commit()
        db.refresh(document)

        chunk = DocumentChunk(
            document_id=document.id,
            chunk_index=0,
            content="The charter states...",
        )
        db.add(chunk)
        db.commit()
        db.refresh(chunk)

        conversation = Conversation()
        db.add(conversation)
        db.commit()
        db.refresh(conversation)

        citing_message = Message(
            conversation_id=conversation.id,
            role=MessageRole.ASSISTANT,
            content="Per the charter [1]...",
            citations=[
                {
                    "document_chunk_id": chunk.id,
                    "document_id": document.id,
                    "document_title": document.title,
                    "document_source": document.source,
                }
            ],
            created_at=datetime.now(UTC),
        )
        other_message = Message(
            conversation_id=conversation.id,
            role=MessageRole.ASSISTANT,
            content="Unrelated reply, no citations.",
            citations=None,
            created_at=datetime.now(UTC),
        )
        db.add_all([citing_message, other_message])
        db.commit()
        db.refresh(citing_message)

        try:
            service = ProvenanceService(db)
            provenance = service.trace_document(document.id)

            assert provenance.document.id == document.id
            assert provenance.import_job is not None
            assert provenance.import_job.id == import_job.id
            assert provenance.chunk_count == 1
            assert len(provenance.cited_in) == 1
            assert provenance.cited_in[0].message_id == citing_message.id
            assert provenance.cited_in[0].conversation_id == conversation.id

        finally:
            db.query(Message).filter(
                Message.conversation_id == conversation.id
            ).delete(synchronize_session=False)
            db.query(Conversation).filter(
                Conversation.id == conversation.id
            ).delete(synchronize_session=False)
            db.query(DocumentChunk).filter(
                DocumentChunk.document_id == document.id
            ).delete(synchronize_session=False)
            db.query(Document).filter(Document.id == document.id).delete(
                synchronize_session=False
            )
            db.query(ImportJob).filter(ImportJob.id == import_job.id).delete(
                synchronize_session=False
            )
            db.commit()


def test_trace_document_with_no_import_job_or_citations_against_real_database() -> None:
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    engine = create_engine(database_url)

    with Session(engine) as db:
        document = Document(
            title="orphan.txt",
            source="/provenance-test/orphan.txt",
            source_type="txt",
        )
        db.add(document)
        db.commit()
        db.refresh(document)

        try:
            service = ProvenanceService(db)
            provenance = service.trace_document(document.id)

            assert provenance.import_job is None
            assert provenance.chunk_count == 0
            assert provenance.cited_in == []

        finally:
            db.query(Document).filter(Document.id == document.id).delete(
                synchronize_session=False
            )
            db.commit()
