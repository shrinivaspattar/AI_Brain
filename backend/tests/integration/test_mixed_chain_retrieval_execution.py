"""Milestone 18: mixed Chain 1 + Chain 2 corpus retrieval proof.

Proves that genuine Chain 1 (`ImportJob`/`ImportJobService`) and genuine
Chain 2 (`IngestionBatch`/`BatchOrchestratorService`, driven through the
real Milestone 12 CLI) content can coexist in the same `aibrain_test`
database and are BOTH correctly retrievable/citable through the real
`RetrievalService`, the real `POST /rag/search`, and the real
`POST /chat` - closing the "missing proof" the post-M13/M17 reviews
repeatedly named: retrieval/chat's chain-agnosticism was argued from
code (`RetrievalService`/`ChatService` reference neither `import_job_id`
nor `content_identity_group_id` anywhere), never directly observed with
both chains' content present together.

This is a PROOF milestone: no production code is modified. Both
ingestion paths are exercised through their own genuine, already-
established entry points - `ImportJobService.execute_job()` (matching
`test_import_execution.py`'s own convention) and
`run_ingestion_batch.main()` (matching `test_live_http_composition_
execution.py`'s own convention) - never by manually constructing final
`Document`/`DocumentChunk` rows.

FAKE EMBEDDING SCHEME: a single, shared, content-hash-based scheme
(copied from `test_import_execution.py`'s own `_fake_embedding_for`,
not the length-bucket scheme used elsewhere) is used for BOTH chains in
this file specifically because two independently-authored, uniquely-
marked strings must never collide by coincidence the way two same-
length strings could under a length-only scheme - unambiguous cross-
chain identification is the entire point of this proof. Chain 1 uses
this via direct constructor injection (`ImportJobService`'s own
`embedding_client` parameter, exactly as `test_import_execution.py`
already does); Chain 2 uses it via a class-level `EmbeddingClient.embed`
monkeypatch (exactly as Milestone 14 already established), since
`BatchOrchestratorService`/`PipelineEmbeddingService` construct their
own `EmbeddingClient()` with no injection seam. `ChatClient.chat` is
monkeypatched the same way, matching Milestone 14/16's own convention.

Real-database, not-savepoint-isolated - exactly like `test_import_
execution.py`, `test_run_ingestion_batch_cli_execution.py`, and
`test_live_http_composition_execution.py`: the CLI and the HTTP
TestClient each open their own connection, so every row here is a real
commit, cleaned up explicitly in FK-safe order. No T7 access of any
kind - both sources are synthetic, under a test's own tmp_path.
"""

from __future__ import annotations

import hashlib
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

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
from app.models.import_job import ImportJob
from app.models.message import Message
from app.models.source_instance import SourceCategory, SourceInstance
from app.services.chat_client import ChatClient
from app.services.import_job_service import ImportJobService

AI_BRAIN_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = AI_BRAIN_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import run_ingestion_batch  # noqa: E402


def _engine():
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    return create_engine(database_url)


def _unique_hash() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def _fake_embedding_for(text: str) -> list[float]:
    """Content-hash-based, not length-based - see module docstring for
    why this scheme (copied from `test_import_execution.py`) is used
    here instead of the length-bucket `FakeEmbeddingClient` scheme."""
    dimensions = settings.EMBEDDING_DIMENSIONS
    vector = [0.0] * dimensions

    digest = hashlib.sha256(text.encode("utf-8")).digest()
    for index, byte in enumerate(digest):
        vector[index % dimensions] += (byte / 255.0) - 0.5

    return vector


def _fake_embedding_client() -> MagicMock:
    """For direct constructor injection into `ImportJobService` -
    matches `test_import_execution.py`'s own `fake_embedding_client()`."""
    client = MagicMock()
    client.embed.side_effect = lambda texts: [_fake_embedding_for(t) for t in texts]
    return client


def _fake_embed_classmethod(self, texts: list[str]) -> list[list[float]]:
    """Class-level monkeypatch target for `EmbeddingClient.embed` - the
    seam Chain 2's CLI/orchestrator and the HTTP layer's own freshly-
    constructed `RetrievalService(db)` both go through, with no
    constructor injection available (Milestone 14's established
    technique)."""
    if not texts:
        return []
    return [_fake_embedding_for(t) for t in texts]


def _fake_chat(self, messages: list[dict], tools: list[dict] | None = None):
    return SimpleNamespace(content="Answer grounded in the retrieved context.", tool_calls=None)


def _override_get_db(engine):
    def override():
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    return override


# -- Chain 1 seeding (genuine ImportJobService.execute_job path) ---------


def _run_chain1_import(db: Session, tmp_path: Path, content_text: str) -> tuple[int, str]:
    """Mirrors `test_import_execution.py`'s own genuine-ingestion
    convention exactly: a real `ImportJob` row, a real source directory
    on disk, a real `ImportJobService.execute_job()` call. Returns
    (import_job_id, source_path) for later lookup/cleanup."""
    source_dir = tmp_path / "chain1_source"
    source_dir.mkdir(parents=True, exist_ok=True)
    source_path = source_dir / "chain1_notes.txt"
    source_path.write_text(content_text)

    job = ImportJob(
        name="M18 mixed-chain proof - Chain 1",
        source_path=str(source_dir),
        source_type="filesystem",
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    service = ImportJobService(
        db,
        ingestion_dir=tmp_path / "chain1_imports",
        embedding_client=_fake_embedding_client(),
    )
    service.execute_job(job.id)

    return job.id, str(source_path)


def _cleanup_chain1(db: Session, import_job_id: int) -> None:
    document_ids = list(db.scalars(select(Document.id).where(Document.import_job_id == import_job_id)))
    if document_ids:
        db.execute(delete(DocumentChunk).where(DocumentChunk.document_id.in_(document_ids)))
        db.execute(delete(Document).where(Document.id.in_(document_ids)))
    db.execute(delete(ImportJob).where(ImportJob.id == import_job_id))
    db.commit()


# -- Chain 2 seeding (genuine BatchOrchestratorService via M12 CLI) ------


def _seed_chain2_batch(db: Session, tmp_path: Path, content_text: str) -> tuple[int, int, int]:
    """Mirrors `test_live_http_composition_execution.py`'s own seeding
    convention. Returns (batch_id, discovery_run_id, classification_run_id)."""
    source_path = tmp_path / "chain2_source" / "chain2_notes.txt"
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
        classifier_version="test-m18-mixed-chain-v1",
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

    return batch.id, discovery.id, run.id


def _cleanup_chain2(db: Session, *, discovery_run_id: int, classification_run_id: int) -> None:
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


def _cleanup_conversation(db: Session, conversation_id: str | None) -> None:
    if conversation_id is None:
        return
    db.execute(delete(Message).where(Message.conversation_id == conversation_id))
    db.execute(delete(Conversation).where(Conversation.id == conversation_id))
    db.commit()


def test_mixed_chain_1_and_chain_2_content_both_retrievable_and_citable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(EmbeddingClient, "embed", _fake_embed_classmethod)
    monkeypatch.setattr(ChatClient, "chat", _fake_chat)

    engine = _engine()
    import_job_id: int | None = None
    discovery_run_id: int | None = None
    classification_run_id: int | None = None
    conversation_ids: list[str] = []

    chain1_marker = uuid.uuid4().hex
    chain1_content = f"Chain 1 mixed-corpus proof marker {chain1_marker}"

    chain2_marker = uuid.uuid4().hex
    chain2_content = f"Chain 2 mixed-corpus proof marker {chain2_marker}"

    try:
        # -- CHAIN 1 EXECUTION PROOF -----------------------------------
        with Session(engine) as db:
            import_job_id, chain1_source_path = _run_chain1_import(db, tmp_path, chain1_content)

        with Session(engine) as db:
            chain1_document = db.scalars(
                select(Document).where(Document.source == chain1_source_path)
            ).one()
            assert chain1_document.import_job_id == import_job_id
            assert chain1_document.content_identity_group_id is None

            chain1_chunks = list(
                db.scalars(select(DocumentChunk).where(DocumentChunk.document_id == chain1_document.id))
            )
            assert len(chain1_chunks) == 1
            assert chain1_chunks[0].content == chain1_content
            assert chain1_chunks[0].embedding is not None
            assert len(chain1_chunks[0].embedding) == settings.EMBEDDING_DIMENSIONS

            chain1_document_id = chain1_document.id
            chain1_chunk_id = chain1_chunks[0].id
            chain1_title = chain1_document.title
            chain1_source = chain1_document.source

        # -- CHAIN 2 EXECUTION PROOF -----------------------------------
        with Session(engine) as db:
            batch_id, discovery_run_id, classification_run_id = _seed_chain2_batch(
                db, tmp_path, chain2_content
            )

        exit_code = run_ingestion_batch.main(
            ["--batch-id", str(batch_id), "--workspace-root", str(tmp_path / "chain2_workspace")]
        )
        assert exit_code == 0

        with Session(engine) as db:
            chain2_instance = (
                db.query(SourceInstance)
                .filter(SourceInstance.classification_run_id == classification_run_id)
                .one()
            )
            assert chain2_instance.content_identity_group_id is not None

            chain2_document = db.scalars(
                select(Document).where(
                    Document.content_identity_group_id == chain2_instance.content_identity_group_id
                )
            ).one()
            assert chain2_document.content_identity_group_id is not None
            assert chain2_document.import_job_id is None

            chain2_chunks = list(
                db.scalars(select(DocumentChunk).where(DocumentChunk.document_id == chain2_document.id))
            )
            assert len(chain2_chunks) == 1
            assert chain2_chunks[0].content == chain2_content
            assert chain2_chunks[0].embedding is not None
            assert len(chain2_chunks[0].embedding) == settings.EMBEDDING_DIMENSIONS

            chain2_document_id = chain2_document.id
            chain2_chunk_id = chain2_chunks[0].id
            chain2_title = chain2_document.title
            chain2_source = chain2_document.source

        # -- SHARED DATABASE PROOF: both coexist simultaneously --------
        with Session(engine) as db:
            assert db.get(Document, chain1_document_id) is not None
            assert db.get(Document, chain2_document_id) is not None
            assert chain1_document_id != chain2_document_id

        # -- DIRECT RetrievalService PROOF (real service, not mocked) --
        from app.rag.retrieval_service import RetrievalService

        with Session(engine) as db:
            retrieval = RetrievalService(db)

            chain1_results = retrieval.search(chain1_content, top_k=1)
            assert len(chain1_results) == 1
            assert chain1_results[0].chunk.id == chain1_chunk_id
            assert chain1_results[0].document.id == chain1_document_id
            assert chain1_results[0].distance == pytest.approx(0.0, abs=1e-6)

            chain2_results = retrieval.search(chain2_content, top_k=1)
            assert len(chain2_results) == 1
            assert chain2_results[0].chunk.id == chain2_chunk_id
            assert chain2_results[0].document.id == chain2_document_id
            assert chain2_results[0].distance == pytest.approx(0.0, abs=1e-6)

        # -- HTTP POST /rag/search PROOF --------------------------------
        app.dependency_overrides[get_db] = _override_get_db(engine)
        try:
            client = TestClient(app)

            response1 = client.post("/rag/search", json={"query": chain1_content, "top_k": 1})
            assert response1.status_code == 200
            body1 = response1.json()
            assert len(body1["results"]) == 1
            assert body1["results"][0]["chunk_id"] == chain1_chunk_id
            assert body1["results"][0]["document_id"] == chain1_document_id
            assert body1["results"][0]["content"] == chain1_content

            response2 = client.post("/rag/search", json={"query": chain2_content, "top_k": 1})
            assert response2.status_code == 200
            body2 = response2.json()
            assert len(body2["results"]) == 1
            assert body2["results"][0]["chunk_id"] == chain2_chunk_id
            assert body2["results"][0]["document_id"] == chain2_document_id
            assert body2["results"][0]["content"] == chain2_content
        finally:
            app.dependency_overrides.clear()

        # -- HTTP POST /chat PROOF ---------------------------------------
        app.dependency_overrides[get_db] = _override_get_db(engine)
        try:
            client = TestClient(app)

            chat1 = client.post("/chat", json={"message": chain1_content, "top_k": 1})
            assert chat1.status_code == 200
            chat1_body = chat1.json()
            conversation_ids.append(chat1_body["conversation_id"])
            citations1 = chat1_body["message"]["citations"]
            assert citations1 is not None and len(citations1) == 1
            assert set(citations1[0].keys()) == {
                "document_chunk_id",
                "document_id",
                "document_title",
                "document_source",
            }
            assert citations1[0]["document_chunk_id"] == chain1_chunk_id
            assert citations1[0]["document_id"] == chain1_document_id
            assert citations1[0]["document_title"] == chain1_title
            assert citations1[0]["document_source"] == chain1_source

            chat2 = client.post("/chat", json={"message": chain2_content, "top_k": 1})
            assert chat2.status_code == 200
            chat2_body = chat2.json()
            conversation_ids.append(chat2_body["conversation_id"])
            citations2 = chat2_body["message"]["citations"]
            assert citations2 is not None and len(citations2) == 1
            assert set(citations2[0].keys()) == {
                "document_chunk_id",
                "document_id",
                "document_title",
                "document_source",
            }
            assert citations2[0]["document_chunk_id"] == chain2_chunk_id
            assert citations2[0]["document_id"] == chain2_document_id
            assert citations2[0]["document_title"] == chain2_title
            assert citations2[0]["document_source"] == chain2_source
        finally:
            app.dependency_overrides.clear()

        # -- PROVENANCE ASSERTIONS (database level, both directions) ---
        with Session(engine) as db:
            refreshed_chain1 = db.get(Document, chain1_document_id)
            assert refreshed_chain1.import_job_id is not None
            assert refreshed_chain1.content_identity_group_id is None

            refreshed_chain2 = db.get(Document, chain2_document_id)
            assert refreshed_chain2.content_identity_group_id is not None
            assert refreshed_chain2.import_job_id is None

    finally:
        with Session(engine) as db:
            for conversation_id in conversation_ids:
                _cleanup_conversation(db, conversation_id)
            if import_job_id is not None:
                _cleanup_chain1(db, import_job_id)
            if classification_run_id is not None:
                _cleanup_chain2(
                    db,
                    discovery_run_id=discovery_run_id,
                    classification_run_id=classification_run_id,
                )
        engine.dispose()
