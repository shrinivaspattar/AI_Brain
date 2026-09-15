"""Real-database tests for Implementation Milestone 10 (Pipeline-to-
Retrieval Composition Proof). See the Post-M9 Architecture Review and
its M10 Design/Freeze for the frozen specification this milestone
implements.

This proves, for the first time, that content driven through the REAL
scaled ingestion pipeline (IdentityResolutionService ->
NormalizationService -> ChunkingService -> PipelineEmbeddingService)
produces a Document/DocumentChunk that the REAL, pre-existing
RetrievalService/ChatService subsystem can actually find and cite -
previously confirmed only by static code reading (the Post-M9
Architecture Review), never executed.

No mocking of production services: every pipeline/retrieval/chat
service used here is the real class. Only the two external-shaped
dependencies (the Ollama embedding backend and the Ollama chat model)
are replaced with deterministic test doubles, using exactly the same
FakeEmbeddingClient shape already established in
test_milestone_6_batch_integration_execution.py and the same
chat-reply shape (.content/.tool_calls) already established in
test_chat_service.py's own `_reply()` helper.

No IngestionBatch/classification_run_id scoping is used anywhere in
this file - batch mechanics were already exhaustively proven
independently across Milestones 1-9; this milestone tests DATA
compatibility between two subsystems, not batch mechanics. No T7
access of any kind: the source file is a plain .txt file under a
test's own tmp_path.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.classification.chunking_service import ChunkingService
from app.classification.identity_resolution_service import IdentityResolutionService
from app.classification.normalization_service import NormalizationService
from app.classification.pipeline_embedding_service import PipelineEmbeddingService
from app.core.config import settings
from app.models.classification_run import ClassificationRun
from app.models.content_identity_group import ContentPipelineState
from app.models.discovery_run import DiscoveryRun, DiscoveryRunKind
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.message import MessageRole
from app.models.source_instance import SourceInstance
from app.rag.retrieval_service import RetrievalService
from app.services.chat_service import ChatService

_EMBEDDING_DIMENSIONS = settings.EMBEDDING_DIMENSIONS


def _engine():
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    return create_engine(database_url)


@pytest.fixture()
def db():
    """Savepoint-isolated real Postgres session - this is a sequential
    composition proof, not a concurrency test, so the simple, single-
    threaded fixture already used throughout this project's suite is
    the correct (and sufficient) isolation mechanism here."""
    engine = _engine()
    connection = engine.connect()
    outer_transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    yield session
    session.close()
    outer_transaction.rollback()
    connection.close()
    engine.dispose()


def _unique_hash() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def _classification_run(db: Session) -> ClassificationRun:
    discovery = DiscoveryRun(
        run_kind=DiscoveryRunKind.D1_DUPLICATE_ANALYSIS,
        source_root="/synthetic/not-a-real-t7-path",
        report_sha256=_unique_hash(),
        run_started_at=datetime.now(UTC) - timedelta(minutes=5),
        run_completed_at=datetime.now(UTC),
    )
    db.add(discovery)
    db.commit()
    db.refresh(discovery)

    run = ClassificationRun(
        classifier_version="test-m10-composition-v1",
        d1_discovery_run_id=discovery.id,
        started_at=datetime.now(UTC),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _loose_instance(db: Session, run: ClassificationRun, path) -> SourceInstance:
    instance = SourceInstance(
        classification_run_id=run.id,
        root_t7_path=str(path),
        member_path=None,
        content_identity_group_id=None,
        evidence_snapshot={},
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return instance


class FakeEmbeddingClient:
    """Deterministic, no-network stand-in for EmbeddingClient - the
    exact shape already established in
    test_milestone_6_batch_integration_execution.py. Satisfies both
    PipelineEmbeddingService's and RetrievalService's identical
    duck-typed `embed(list[str]) -> list[list[float]]` interface with
    no shape mismatch (confirmed by the M10 design pass's direct
    reading of both call sites)."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(len(t) % 7) / 7.0] * _EMBEDDING_DIMENSIONS for t in texts]


class FakeChatClient:
    """Deterministic, no-network stand-in for ChatClient - matches the
    exact reply shape (.content / .tool_calls) test_chat_service.py's
    own `_reply()` helper already established, since `_run_tool_loop`
    (chat_service.py) reads `reply.tool_calls`/`reply.content`
    directly. `tool_calls=None` ensures the tool loop returns
    immediately on the first call - no tool execution, no further
    model calls, fully deterministic."""

    def __init__(self, reply_content: str):
        self.reply_content = reply_content
        self.calls: list[list[dict]] = []

    def chat(self, messages: list[dict], tools=None):
        self.calls.append(list(messages))
        return SimpleNamespace(content=self.reply_content, tool_calls=None)


def _run_pipeline_to_embedded_chunk(db: Session, tmp_path, content_text: str) -> tuple[Document, DocumentChunk, FakeEmbeddingClient]:
    """The frozen M10 call sequence: a real .txt file on disk, through
    IdentityResolutionService -> NormalizationService -> ChunkingService
    -> PipelineEmbeddingService, using only the real production
    services and one injected FakeEmbeddingClient. No IngestionBatch
    anywhere - classification_run_id is omitted at every claim call,
    preserving each service's own pre-Milestone-4/6 unscoped default
    behavior."""
    run = _classification_run(db)
    source_file = tmp_path / "source" / "notes.txt"
    source_file.parent.mkdir(parents=True)
    source_file.write_text(content_text)
    instance = _loose_instance(db, run, source_file)

    workspace_root = tmp_path / "workspace"

    resolved = IdentityResolutionService(db).resolve_next(worker_id="worker-resolve", workspace_root=workspace_root)
    assert resolved is not None
    assert resolved.id == instance.id
    group_id = resolved.content_identity_group_id
    assert group_id is not None

    normalized_group = NormalizationService(db).normalize_next(worker_id="worker-normalize", workspace_root=workspace_root)
    assert normalized_group is not None
    assert normalized_group.id == group_id
    assert normalized_group.pipeline_state == ContentPipelineState.NORMALIZED

    document = db.query(Document).filter(Document.content_identity_group_id == group_id).one()

    chunked_group = ChunkingService(db).chunk_next(worker_id="worker-chunk", workspace_root=workspace_root)
    assert chunked_group is not None
    assert chunked_group.id == group_id
    assert chunked_group.pipeline_state == ContentPipelineState.CHUNKED

    chunk = db.query(DocumentChunk).filter(DocumentChunk.document_id == document.id).one()
    assert chunk.embedding is None  # not yet embedded - proves the next step is what populates it

    fake_embedding_client = FakeEmbeddingClient()
    embedded_group = PipelineEmbeddingService(db, embedding_client=fake_embedding_client).embed_next(worker_id="worker-embed")
    assert embedded_group is not None
    assert embedded_group.id == group_id
    assert embedded_group.pipeline_state == ContentPipelineState.INGESTED

    db.refresh(chunk)
    assert chunk.embedding is not None
    assert chunk.content == content_text  # short text -> chunk_text() returns it verbatim (chunker.py)

    return document, chunk, fake_embedding_client


# ============================================================
# Test 1: Pipeline -> Retrieval
# ============================================================


def test_pipeline_output_is_retrievable_via_real_retrieval_service(db: Session, tmp_path) -> None:
    content_text = "AI_Brain scaled ingestion composition proof content for Milestone 10"
    document, chunk, fake_embedding_client = _run_pipeline_to_embedded_chunk(db, tmp_path, content_text)

    retrieval = RetrievalService(db, embedding_client=fake_embedding_client)
    results = retrieval.search(content_text, top_k=5)

    assert len(results) >= 1
    top = results[0]

    # Tied to the KNOWN ids/content this specific execution produced -
    # not merely "some result was returned."
    assert top.chunk.id == chunk.id
    assert top.document.id == document.id
    assert top.document.content_identity_group_id is not None
    assert top.document.content_identity_group_id == document.content_identity_group_id
    assert top.chunk.content == content_text
    # Identical query text -> identical FakeEmbeddingClient vector ->
    # exact-zero cosine distance (the only row present in this
    # transaction, so this also implicitly proves it is the top match).
    assert top.distance == pytest.approx(0.0, abs=1e-9)


# ============================================================
# Test 2: Pipeline -> Retrieval -> Chat
# ============================================================


def test_pipeline_output_is_consumed_by_chat_service_with_correct_citation(db: Session, tmp_path) -> None:
    content_text = "AI_Brain scaled ingestion composition proof content for chat consumption"
    document, chunk, fake_embedding_client = _run_pipeline_to_embedded_chunk(db, tmp_path, content_text)

    retrieval = RetrievalService(db, embedding_client=fake_embedding_client)
    fake_chat_client = FakeChatClient(reply_content="Based on your documents, here is the deterministic answer.")

    chat_service = ChatService(db, chat_client=fake_chat_client, retrieval_service=retrieval)
    assistant_message = chat_service.send_message(content_text)

    # Stable, architecture-level behavior only - never fragile generated prose.
    assert assistant_message.role == MessageRole.ASSISTANT
    assert assistant_message.content == "Based on your documents, here is the deterministic answer."
    assert len(fake_chat_client.calls) == 1

    # Proves ChatService actually consumed the pipeline-created content,
    # not merely that some empty/unrelated retrieval happened: the
    # ingested text must appear somewhere in the prompt sent to the
    # (fake) model.
    sent_messages = fake_chat_client.calls[0]
    assert any(content_text in message.get("content", "") for message in sent_messages)

    # Citation contract as it CURRENTLY exists (Document.source/title
    # only) - documented, not extended. The known provenance-richness
    # limitation (no SourceInstance/ProvenanceLink surfaced) remains
    # deferred to the separate Answer-Provenance Decision; this test
    # does not attempt to close that gap.
    assert assistant_message.citations is not None
    assert len(assistant_message.citations) >= 1
    citation = assistant_message.citations[0]
    assert citation["document_chunk_id"] == chunk.id
    assert citation["document_id"] == document.id
    assert citation["document_title"] == document.title
    assert citation["document_source"] == document.source
