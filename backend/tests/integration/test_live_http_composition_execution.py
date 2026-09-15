"""Milestone 14: live HTTP composition proof.

Proves the boundary M10 and M12 each proved one side of, but that
nothing in this repository had proven together: content produced by
the real, unmodified Milestone 12 CLI (Chain 2's own operator
entrypoint) is genuinely retrievable and citable through the REAL
`POST /rag/search` and `POST /chat` HTTP endpoints - the same code path
an actual user hits - not merely through direct service composition
(Milestone 10) or the CLI's own plain-text report (Milestone 12).

M12 CLI (real, in-process `main()`)
    -> Chain 2 ingestion (real, unmodified BatchOrchestratorService)
    -> Document / DocumentChunk / embedding (real rows, real commit)
    -> real HTTP POST /rag/search
    -> real HTTP POST /chat
    -> citation

FAKE BOUNDARY, AND ONLY THAT BOUNDARY: `EmbeddingClient.embed` and
`ChatClient.chat` are monkeypatched at the class level - the one seam
common to every code path here (ingestion-side `PipelineEmbeddingService`
and query-side `RetrievalService` both call the same `EmbeddingClient.
embed`; `ChatService` calls the same `ChatClient.chat`). Neither
`RetrievalService` nor `ChatService` is mocked, faked, or bypassed -
both handlers in `app/api/rag.py`/`app/api/chat.py` construct them
exactly as they do in production, since neither has a dependency-
injection seam for its client and none is added here. This directly
reuses the `len(text) % 7`-bucket fake embedding scheme already
established since Milestone 6/M11's `FakeEmbeddingClient`, and the
`.content`/`.tool_calls` fake chat reply shape already established by
Milestone 10 and `test_chat_execution.py`.

Real-database, not-savepoint-isolated, exactly like `test_run_ingestion_
batch_cli_execution.py` (M12) and `test_import_jobs_api_execution.py`:
the CLI opens its own engine/connection, and `TestClient` requests run
through `app.dependency_overrides[get_db]` on a second, separate
connection - a savepoint on either would be invisible to the other, so
every row here is a real commit, cleaned up explicitly in FK-safe order
at the end of each test. No T7 access of any kind - the one source file
is synthetic, under a test's own tmp_path.
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.embeddings.client import EmbeddingClient
from app.main import app
from app.models.classification_run import ClassificationRun
from app.models.content_identity_group import ContentIdentityGroup
from app.models.conversation import Conversation
from app.models.discovery_run import DiscoveryRun, DiscoveryRunKind
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.ingestion_attempt import IngestionAttempt
from app.models.ingestion_batch import BatchStatus, IngestionBatch
from app.models.message import Message
from app.models.source_instance import SourceCategory, SourceInstance
from app.services.chat_client import ChatClient

AI_BRAIN_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = AI_BRAIN_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import run_ingestion_batch  # noqa: E402


def _engine():
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    return create_engine(database_url)


def _override_get_db(engine):
    def override():
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    return override


def _fake_embed(self, texts: list[str]) -> list[list[float]]:
    """The exact `len(t) % 7`-bucket scheme established by Milestone
    11's `FakeEmbeddingClient` - identical text length always yields
    an identical vector, so a query string equal to the ingested
    chunk's own content is provably nearest (cosine distance exactly
    0), regardless of what else may be in the table."""
    return [[float(len(t) % 7) / 7.0] * settings.EMBEDDING_DIMENSIONS for t in texts]


def _fake_chat(self, messages: list[dict], tools: list[dict] | None = None):
    """Mirrors Milestone 10's/`test_chat_execution.py`'s established
    fake-reply shape: a plain final answer, no tool call requested, so
    `ChatService._run_tool_loop` exits after exactly one iteration."""
    return SimpleNamespace(content="Answer grounded in the retrieved context.", tool_calls=None)


def _unique_hash() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def _seed_batch(db: Session, tmp_path: Path) -> tuple[IngestionBatch, str, int, int]:
    """Seeds one real, committed ClassificationRun/IngestionBatch/
    SourceInstance for one small, eligible, uniquely-marked .txt file -
    small enough that `chunk_text` (1000-char chunks) produces exactly
    one chunk equal to the file's own (stripped) content, so the
    resulting DocumentChunk.content is known exactly, not merely
    non-empty. Returns (batch, content_text, discovery_run_id,
    classification_run_id) for the caller to drive through the CLI and
    later clean up.
    """
    marker = uuid.uuid4().hex
    content_text = f"M14 live HTTP composition proof marker {marker}"

    source_path = tmp_path / "source" / "notes.txt"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(content_text)

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
        classifier_version="test-m14-http-composition-v1",
        d1_discovery_run_id=discovery.id,
        started_at=datetime.now(UTC),
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    batch = IngestionBatch(
        status=BatchStatus.RUNNING,
        stop_reason=None,
        classification_run_id=run.id,
        max_source_instances=1000,
        max_source_bytes=2_000_000_000,
        max_extracted_bytes=None,
        max_embeddings=5000,
        max_runtime_seconds=7200,
        eligible_source_count=1,
        policy_filtered_count=0,
        selectable_count=1,
        source_instances_selected=1,
        source_bytes_selected=len(content_text),
        extracted_bytes_consumed=0,
        embeddings_reserved=0,
        selection_fingerprint=_unique_hash(),
        selection_policy_version="batch-class-1-text-document-v1",
        ordering_version="lexicographic-path-v1",
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)

    instance = SourceInstance(
        classification_run_id=run.id,
        root_t7_path=str(source_path),
        evidence_snapshot={},
        source_category=SourceCategory.LOOSE_FILE,
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)

    return batch, content_text, discovery.id, run.id


def _cleanup(engine, *, discovery_run_id: int, classification_run_id: int, conversation_id: str | None) -> None:
    with Session(engine) as db:
        group_ids = [
            gid
            for (gid,) in db.execute(
                select(SourceInstance.content_identity_group_id).where(
                    SourceInstance.classification_run_id == classification_run_id,
                    SourceInstance.content_identity_group_id.is_not(None),
                )
            ).all()
        ]
        document_ids = (
            [
                did
                for (did,) in db.execute(
                    select(Document.id).where(Document.content_identity_group_id.in_(group_ids))
                ).all()
            ]
            if group_ids
            else []
        )

        if conversation_id is not None:
            db.execute(delete(Message).where(Message.conversation_id == conversation_id))
            db.execute(delete(Conversation).where(Conversation.id == conversation_id))

        if document_ids:
            db.execute(delete(DocumentChunk).where(DocumentChunk.document_id.in_(document_ids)))
            db.execute(delete(Document).where(Document.id.in_(document_ids)))

        db.execute(
            delete(IngestionAttempt).where(
                IngestionAttempt.source_instance_id.in_(
                    select(SourceInstance.id).where(SourceInstance.classification_run_id == classification_run_id)
                )
            )
        )
        if group_ids:
            db.execute(delete(IngestionAttempt).where(IngestionAttempt.content_identity_group_id.in_(group_ids)))
        db.execute(delete(SourceInstance).where(SourceInstance.classification_run_id == classification_run_id))
        if group_ids:
            db.execute(delete(ContentIdentityGroup).where(ContentIdentityGroup.id.in_(group_ids)))
        db.execute(delete(IngestionBatch).where(IngestionBatch.classification_run_id == classification_run_id))
        db.execute(delete(ClassificationRun).where(ClassificationRun.id == classification_run_id))
        db.execute(delete(DiscoveryRun).where(DiscoveryRun.id == discovery_run_id))
        db.commit()


def test_cli_ingested_content_is_top_result_via_real_rag_search_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(EmbeddingClient, "embed", _fake_embed)

    engine = _engine()
    discovery_run_id = None
    classification_run_id = None
    try:
        with Session(engine) as seed_db:
            batch, content_text, discovery_run_id, classification_run_id = _seed_batch(seed_db, tmp_path)
            batch_id = batch.id

        exit_code = run_ingestion_batch.main(
            ["--batch-id", str(batch_id), "--workspace-root", str(tmp_path / "workspace")]
        )
        assert exit_code == 0

        with Session(engine) as verify_db:
            instance = (
                verify_db.query(SourceInstance)
                .filter(SourceInstance.classification_run_id == classification_run_id)
                .one()
            )
            assert instance.content_identity_group_id is not None
            document = (
                verify_db.query(Document)
                .filter(Document.content_identity_group_id == instance.content_identity_group_id)
                .one()
            )
            chunk = verify_db.query(DocumentChunk).filter(DocumentChunk.document_id == document.id).one()
            assert chunk.content == content_text
            expected_chunk_id = chunk.id
            expected_document_id = document.id

        app.dependency_overrides[get_db] = _override_get_db(engine)
        try:
            client = TestClient(app)
            response = client.post("/rag/search", json={"query": content_text, "top_k": 1})
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        body = response.json()
        assert len(body["results"]) == 1
        top = body["results"][0]
        assert top["chunk_id"] == expected_chunk_id
        assert top["document_id"] == expected_document_id
        assert top["content"] == content_text
        assert top["score"] == pytest.approx(1.0, abs=1e-6)

    finally:
        if classification_run_id is not None:
            _cleanup(engine, discovery_run_id=discovery_run_id, classification_run_id=classification_run_id, conversation_id=None)
        engine.dispose()


def test_cli_ingested_content_is_cited_via_real_chat_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(EmbeddingClient, "embed", _fake_embed)
    monkeypatch.setattr(ChatClient, "chat", _fake_chat)

    engine = _engine()
    discovery_run_id = None
    classification_run_id = None
    conversation_id = None
    try:
        with Session(engine) as seed_db:
            batch, content_text, discovery_run_id, classification_run_id = _seed_batch(seed_db, tmp_path)
            batch_id = batch.id

        exit_code = run_ingestion_batch.main(
            ["--batch-id", str(batch_id), "--workspace-root", str(tmp_path / "workspace")]
        )
        assert exit_code == 0

        with Session(engine) as verify_db:
            instance = (
                verify_db.query(SourceInstance)
                .filter(SourceInstance.classification_run_id == classification_run_id)
                .one()
            )
            document = (
                verify_db.query(Document)
                .filter(Document.content_identity_group_id == instance.content_identity_group_id)
                .one()
            )
            chunk = verify_db.query(DocumentChunk).filter(DocumentChunk.document_id == document.id).one()
            expected_chunk_id = chunk.id
            expected_document_id = document.id
            expected_title = document.title
            expected_source = document.source
            expected_root_t7_path = instance.root_t7_path

        app.dependency_overrides[get_db] = _override_get_db(engine)
        try:
            client = TestClient(app)
            response = client.post("/chat", json={"message": content_text, "top_k": 1})
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        body = response.json()
        conversation_id = body["conversation_id"]
        citations = body["message"]["citations"]

        assert citations is not None
        assert len(citations) == 1
        citation = citations[0]

        # Milestone 22/23: the citation contract now also carries real
        # Chain 2 provenance (source_occurrences) alongside the four
        # original fields - the M10-era "nothing richer" finding this
        # comment used to record is now closed for Chain 2 content.
        assert set(citation.keys()) == {
            "document_chunk_id",
            "document_id",
            "document_title",
            "document_source",
            "source_occurrences",
        }
        assert citation["document_chunk_id"] == expected_chunk_id
        assert citation["document_id"] == expected_document_id
        assert citation["document_title"] == expected_title
        assert citation["document_source"] == expected_source
        assert citation["source_occurrences"] is not None
        assert len(citation["source_occurrences"]) == 1
        assert citation["source_occurrences"][0]["root_t7_path"] == expected_root_t7_path
        assert citation["source_occurrences"][0]["member_path"] is None

    finally:
        if classification_run_id is not None:
            _cleanup(
                engine,
                discovery_run_id=discovery_run_id,
                classification_run_id=classification_run_id,
                conversation_id=conversation_id,
            )
        engine.dispose()
