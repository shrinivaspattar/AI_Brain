"""Milestone 23: provenance-rich citations using ALL source occurrences
(Model A, frozen by the Milestone 22 design/freeze).

Covers the scenarios not already exercised by the existing retrieval/
composition/mixed-chain suites (which already prove the single-
occurrence Chain 1/Chain 2 cases end-to-end - see test_pipeline_
retrieval_composition_execution.py, test_live_http_composition_
execution.py, test_mixed_chain_retrieval_execution.py, each extended
this milestone with source_occurrences assertions):

- multiple SourceInstances converging on one ContentIdentityGroup are
  ALL returned, in deterministic SourceInstance.id order, with none
  selected as "the" source (canonical_status plays no role);
- nested archive ancestry is preserved, in sequence_index order, only
  when genuinely nested (more than one archive level) - a single-level
  occurrence's archive_ancestry is None, per the frozen design;
- the batched provenance lookup issues a constant number of additional
  SQL statements regardless of how many groups/occurrences are
  involved - never one query per chunk/document/SourceInstance;
- /rag/search and /chat expose byte-identical source_occurrences data
  for the same content, proving there is only one provenance
  implementation, not two.

No T7 access of any kind - every path is synthetic. Real, non-
savepoint database, matching this milestone's own established
provenance-fixture convention (direct SourceInstanceService/
ContentIdentityService construction, bypassing the full batch/CLI
pipeline - already exhaustively proven elsewhere, e.g. Milestone 6/9).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, event, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.classification.content_identity_service import ContentIdentityService
from app.classification.source_instance_service import ProvenanceStep, SourceInstanceService
from app.core.config import settings
from app.db.session import get_db
from app.embeddings.client import EmbeddingClient
from app.main import app
from app.models.classification_run import ClassificationRun
from app.models.content_identity_group import (
    ContentIdentityAlgorithm,
    ContentIdentityGroup,
    ContentIdentityKind,
    ContentPipelineState,
)
from app.models.discovery_run import DiscoveryRun, DiscoveryRunKind
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.ingestion_batch import IngestionBatch  # noqa: F401 - registers the FK target table
from app.models.provenance_link import ProvenanceLink, ProvenanceLinkKind
from app.models.source_instance import SourceCategory, SourceInstance
from app.rag.retrieval_service import RetrievalService
from app.services.chat_client import ChatClient


def _engine():
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    return create_engine(database_url)


def _unique_hash() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def _fake_embedding_for(text: str) -> list[float]:
    """The established content-hash-based deterministic scheme
    (`test_import_execution.py`'s own `_fake_embedding_for`) - NOT the
    coarser length-bucket scheme used elsewhere. `aibrain_test` has no
    per-test isolation for this file's real-committing sessions and
    accumulates rows across every run in this long-lived local
    database; a length-only bucket (only 7 distinct vectors) produces
    genuine, deterministic ties (correctly won by M15's own tie-break)
    against unrelated accumulated content, which is exactly the
    instability this file's own tests must not be exposed to. Hashing
    the text keeps embeddings effectively unique without needing a
    real model."""
    dimensions = settings.EMBEDDING_DIMENSIONS
    vector = [0.0] * dimensions

    digest = hashlib.sha256(text.encode("utf-8")).digest()
    for index, byte in enumerate(digest):
        vector[index % dimensions] += (byte / 255.0) - 0.5
    return vector


class FakeEmbeddingClient:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [_fake_embedding_for(t) for t in texts]


def _override_get_db(engine):
    def override():
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    return override


def _discovery_and_run(db: Session) -> ClassificationRun:
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
        classifier_version="test-m23-provenance-v1",
        d1_discovery_run_id=discovery.id,
        started_at=datetime.now(UTC),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _group_and_document(db: Session, content_text: str) -> tuple[ContentIdentityGroup, Document, DocumentChunk]:
    """Mirrors NormalizationService/ChunkingService/PipelineEmbeddingService's
    own end-state exactly (a group at INGESTED, one Document, one embedded
    DocumentChunk) - constructed directly since this file tests the
    retrieval-layer provenance fan-out, not the pipeline itself (already
    exhaustively proven in Milestones 6/9/10/14)."""
    group = ContentIdentityService(db).get_or_create_group(
        identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
        identity_algorithm=ContentIdentityAlgorithm.SHA256,
        identity_hash=_unique_hash(),
        initial_pipeline_state=ContentPipelineState.INGESTED,
    )
    document = Document(
        title="provenance-test.txt",
        source=f"/workspace/{group.id}/provenance-test.txt",
        source_type="txt",
        content_hash=group.identity_hash,
        content_identity_group_id=group.id,
    )
    db.add(document)
    db.commit()
    db.refresh(document)

    chunk = DocumentChunk(
        document_id=document.id,
        chunk_index=0,
        content=content_text,
        embedding=FakeEmbeddingClient().embed([content_text])[0],
    )
    db.add(chunk)
    db.commit()
    db.refresh(chunk)
    return group, document, chunk


def _cleanup(db: Session, *, run_ids: list[int], discovery_ids: list[int], group_ids: list[int]) -> None:
    document_ids = (
        [did for (did,) in db.execute(select(Document.id).where(Document.content_identity_group_id.in_(group_ids))).all()]
        if group_ids
        else []
    )
    if document_ids:
        db.execute(delete(DocumentChunk).where(DocumentChunk.document_id.in_(document_ids)))
        db.execute(delete(Document).where(Document.id.in_(document_ids)))
    if run_ids:
        db.execute(
            delete(ProvenanceLink).where(
                ProvenanceLink.source_instance_id.in_(
                    select(SourceInstance.id).where(SourceInstance.classification_run_id.in_(run_ids))
                )
            )
        )
        db.execute(delete(SourceInstance).where(SourceInstance.classification_run_id.in_(run_ids)))
    if group_ids:
        db.execute(delete(ContentIdentityGroup).where(ContentIdentityGroup.id.in_(group_ids)))
    if run_ids:
        db.execute(delete(ClassificationRun).where(ClassificationRun.id.in_(run_ids)))
    if discovery_ids:
        db.execute(delete(DiscoveryRun).where(DiscoveryRun.id.in_(discovery_ids)))
    db.commit()


# -- C: multiple occurrences, all returned, none selected -----------------


def test_multiple_source_instances_converge_and_all_are_returned_without_selection() -> None:
    engine = _engine()
    content_text = f"M23 multi-occurrence proof {uuid.uuid4().hex}"

    with Session(engine) as db:
        run = _discovery_and_run(db)
        group, document, chunk = _group_and_document(db, content_text)

        instance_a = SourceInstanceService(db).create_instance(
            classification_run_id=run.id,
            root_t7_path="/synthetic/copy_a.txt",
            member_path=None,
            evidence_snapshot={},
            chain=[ProvenanceStep(kind=ProvenanceLinkKind.T7_FILE, path="/synthetic/copy_a.txt")],
            source_category=SourceCategory.LOOSE_FILE,
        )
        instance_b = SourceInstanceService(db).create_instance(
            classification_run_id=run.id,
            root_t7_path="/synthetic/copy_b.txt",
            member_path=None,
            evidence_snapshot={},
            chain=[ProvenanceStep(kind=ProvenanceLinkKind.T7_FILE, path="/synthetic/copy_b.txt")],
            source_category=SourceCategory.LOOSE_FILE,
        )
        ContentIdentityService(db).assign_content_identity(instance_a.id, group)
        ContentIdentityService(db).assign_content_identity(instance_b.id, group)

        # Deliberately mark one CANONICAL - Milestone 21/22 established
        # this must play NO role in occurrence selection or filtering.
        from app.classification.canonical_decision_service import CanonicalDecisionService
        from app.models.source_instance import CanonicalStatus

        CanonicalDecisionService(db).decide(
            instance_a, status=CanonicalStatus.CANONICAL, reason="test", decided_by="human:test-suite"
        )

        try:
            lower_id, higher_id = sorted([instance_a.id, instance_b.id])
            expected_paths_in_order = {instance_a.id: "/synthetic/copy_a.txt", instance_b.id: "/synthetic/copy_b.txt"}

            retrieval = RetrievalService(db, embedding_client=FakeEmbeddingClient())
            results = retrieval.search(content_text, top_k=1)

            assert len(results) == 1
            occurrences = results[0].source_occurrences
            assert occurrences is not None
            assert len(occurrences) == 2

            # Deterministic SourceInstance.id ordering - not canonical
            # status, not insertion order, not path.
            assert [o.root_t7_path for o in occurrences] == [
                expected_paths_in_order[lower_id],
                expected_paths_in_order[higher_id],
            ]

            # Never marked as "the" source: the payload carries no
            # canonical/authority field of any kind for either occurrence.
            for occurrence in occurrences:
                assert not hasattr(occurrence, "canonical_status")
                assert not hasattr(occurrence, "is_canonical")

        finally:
            _cleanup(db, run_ids=[run.id], discovery_ids=[run.d1_discovery_run_id], group_ids=[group.id])


# -- D: nested archive ancestry --------------------------------------------


def test_nested_archive_ancestry_is_preserved_in_sequence_order() -> None:
    engine = _engine()
    content_text = f"M23 nested ancestry proof {uuid.uuid4().hex}"

    with Session(engine) as db:
        run = _discovery_and_run(db)
        group, document, chunk = _group_and_document(db, content_text)

        # Genuinely nested: T7_FILE -> ARCHIVE_MEMBER (outer.zip) ->
        # ARCHIVE_MEMBER (inner.zip's own member) - three links, more
        # than the two a single-level archive member would have.
        nested_instance = SourceInstanceService(db).create_instance(
            classification_run_id=run.id,
            root_t7_path="/synthetic/outer.zip",
            member_path="inner.zip/leaf.txt",
            evidence_snapshot={},
            chain=[
                ProvenanceStep(kind=ProvenanceLinkKind.T7_FILE, path="/synthetic/outer.zip"),
                ProvenanceStep(kind=ProvenanceLinkKind.ARCHIVE_MEMBER, path="inner.zip"),
                ProvenanceStep(kind=ProvenanceLinkKind.ARCHIVE_MEMBER, path="inner.zip/leaf.txt"),
            ],
            source_category=SourceCategory.LOOSE_FILE,
        )
        # A second, single-level (not nested) instance in the SAME
        # group, to contrast: its archive_ancestry must be None.
        single_level_instance = SourceInstanceService(db).create_instance(
            classification_run_id=run.id,
            root_t7_path="/synthetic/plain.zip",
            member_path="leaf.txt",
            evidence_snapshot={},
            chain=[
                ProvenanceStep(kind=ProvenanceLinkKind.T7_FILE, path="/synthetic/plain.zip"),
                ProvenanceStep(kind=ProvenanceLinkKind.ARCHIVE_MEMBER, path="leaf.txt"),
            ],
            source_category=SourceCategory.LOOSE_FILE,
        )
        ContentIdentityService(db).assign_content_identity(nested_instance.id, group)
        ContentIdentityService(db).assign_content_identity(single_level_instance.id, group)

        try:
            retrieval = RetrievalService(db, embedding_client=FakeEmbeddingClient())
            results = retrieval.search(content_text, top_k=1)

            occurrences_by_path = {o.root_t7_path: o for o in results[0].source_occurrences}

            nested = occurrences_by_path["/synthetic/outer.zip"]
            assert nested.member_path == "inner.zip/leaf.txt"
            assert nested.archive_ancestry is not None
            assert [step.path for step in nested.archive_ancestry] == [
                "/synthetic/outer.zip",
                "inner.zip",
                "inner.zip/leaf.txt",
            ]
            assert [step.kind for step in nested.archive_ancestry] == [
                "t7_file",
                "archive_member",
                "archive_member",
            ]

            single_level = occurrences_by_path["/synthetic/plain.zip"]
            assert single_level.member_path == "leaf.txt"
            # Not nested (only one archive level) - root_t7_path/
            # member_path alone already fully describe it.
            assert single_level.archive_ancestry is None

        finally:
            _cleanup(db, run_ids=[run.id], discovery_ids=[run.d1_discovery_run_id], group_ids=[group.id])


# -- H: batched provenance lookup, O(1) query count ------------------------


def test_provenance_lookup_query_count_is_constant_not_per_row() -> None:
    """Proves the batching claim empirically (real SQL statement count),
    not merely by code inspection - three distinct groups, one with two
    converged SourceInstances, should cost exactly two additional
    queries (one SourceInstance IN-query, one ProvenanceLink IN-query),
    never one query per matched document/chunk/instance."""
    engine = _engine()
    run_ids: list[int] = []
    discovery_ids: list[int] = []
    group_ids: list[int] = []

    with Session(engine) as db:
        try:
            queries = []
            for i in range(3):
                run = _discovery_and_run(db)
                run_ids.append(run.id)
                discovery_ids.append(run.d1_discovery_run_id)
                content_text = f"M23 query-count proof item {i} {uuid.uuid4().hex}"
                group, document, chunk = _group_and_document(db, content_text)
                group_ids.append(group.id)
                queries.append(content_text)

                instance = SourceInstanceService(db).create_instance(
                    classification_run_id=run.id,
                    root_t7_path=f"/synthetic/item_{i}.txt",
                    member_path=None,
                    evidence_snapshot={},
                    chain=[ProvenanceStep(kind=ProvenanceLinkKind.T7_FILE, path=f"/synthetic/item_{i}.txt")],
                )
                ContentIdentityService(db).assign_content_identity(instance.id, group)

                if i == 0:
                    # One group with TWO converged instances - the
                    # fan-out this test exists to bound.
                    instance_extra = SourceInstanceService(db).create_instance(
                        classification_run_id=run.id,
                        root_t7_path=f"/synthetic/item_{i}_copy.txt",
                        member_path=None,
                        evidence_snapshot={},
                        chain=[ProvenanceStep(kind=ProvenanceLinkKind.T7_FILE, path=f"/synthetic/item_{i}_copy.txt")],
                    )
                    ContentIdentityService(db).assign_content_identity(instance_extra.id, group)

            retrieval = RetrievalService(db, embedding_client=FakeEmbeddingClient())

            statement_count = 0

            def _count(*args, **kwargs):
                nonlocal statement_count
                statement_count += 1

            event.listen(db.get_bind(), "before_cursor_execute", _count)
            try:
                for content_text in queries:
                    statement_count = 0
                    results = retrieval.search(content_text, top_k=3)
                    assert len(results) == 3
                    # 1 main distance query + 1 SourceInstance batch query
                    # + 1 ProvenanceLink batch query = 3, regardless of
                    # top_k or how many occurrences any one group has.
                    assert statement_count == 3
            finally:
                event.remove(db.get_bind(), "before_cursor_execute", _count)

        finally:
            _cleanup(db, run_ids=run_ids, discovery_ids=discovery_ids, group_ids=group_ids)


# -- F/G: /rag/search and /chat expose identical provenance data -----------


def test_rag_search_and_chat_expose_byte_identical_source_occurrences() -> None:
    engine = _engine()
    content_text = f"M23 same-shape proof {uuid.uuid4().hex}"
    run_id = None
    discovery_id = None
    group_id = None

    def _fake_embed(self, texts):
        return FakeEmbeddingClient().embed(texts)

    def _fake_chat(self, messages, tools=None):
        return SimpleNamespace(content="Answer.", tool_calls=None)

    with Session(engine) as db:
        run = _discovery_and_run(db)
        run_id, discovery_id = run.id, run.d1_discovery_run_id
        group, document, chunk = _group_and_document(db, content_text)
        group_id = group.id

        instance = SourceInstanceService(db).create_instance(
            classification_run_id=run.id,
            root_t7_path="/synthetic/same_shape.txt",
            member_path=None,
            evidence_snapshot={},
            chain=[ProvenanceStep(kind=ProvenanceLinkKind.T7_FILE, path="/synthetic/same_shape.txt")],
        )
        ContentIdentityService(db).assign_content_identity(instance.id, group)

    conversation_id = None
    import pytest as _pytest

    monkeypatch = _pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(EmbeddingClient, "embed", _fake_embed)
        monkeypatch.setattr(ChatClient, "chat", _fake_chat)

        app.dependency_overrides[get_db] = _override_get_db(engine)
        try:
            client = TestClient(app)
            search_response = client.post("/rag/search", json={"query": content_text, "top_k": 1})
            chat_response = client.post("/chat", json={"message": content_text, "top_k": 1})
        finally:
            app.dependency_overrides.clear()

        assert search_response.status_code == 200
        assert chat_response.status_code == 200

        search_occurrences = search_response.json()["results"][0]["source_occurrences"]
        chat_body = chat_response.json()
        conversation_id = chat_body["conversation_id"]
        chat_occurrences = chat_body["message"]["citations"][0]["source_occurrences"]

        assert search_occurrences is not None
        assert search_occurrences == chat_occurrences

    finally:
        monkeypatch.undo()
        with Session(engine) as db:
            if conversation_id is not None:
                from app.models.conversation import Conversation
                from app.models.message import Message

                db.execute(delete(Message).where(Message.conversation_id == conversation_id))
                db.execute(delete(Conversation).where(Conversation.id == conversation_id))
                db.commit()
            _cleanup(
                db,
                run_ids=[run_id] if run_id else [],
                discovery_ids=[discovery_id] if discovery_id else [],
                group_ids=[group_id] if group_id else [],
            )
