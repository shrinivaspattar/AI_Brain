"""Real-database tests for the SourceInstance -> ProvenanceLink ->
ContentIdentityGroup -> Document -> DocumentChunk schema (frozen design,
AI_Brain_Architecture.md commits 29d6864 / 14b8063).

No T7 access of any kind: every DiscoveryRun/ClassificationRun/
SourceInstance/ProvenanceLink/ContentIdentityGroup row created here
uses entirely synthetic data (fake report hashes, fake T7-shaped paths
that are never opened as real files). This module proves the schema's
constraints and the services that maintain its invariants, against
real Postgres (`aibrain_test`), never mocks - matching this project's
standing rule that concurrency/constraint claims must be verified
against real infrastructure.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.classification.canonical_decision_service import CanonicalDecisionService
from app.classification.classification_run_service import ClassificationRunService
from app.classification.content_identity_service import ContentIdentityService
from app.classification.source_instance_service import ProvenanceStep, SourceInstanceService
from app.core.config import settings
from app.models.classification_run import ClassificationRun
from app.models.content_identity_group import (
    ContentIdentityAlgorithm,
    ContentIdentityGroup,
    ContentIdentityKind,
)
from app.models.discovery_run import DiscoveryRun, DiscoveryRunKind
from app.models.document import Document
from app.models.provenance_link import ProvenanceLink, ProvenanceLinkKind
from app.models.source_instance import CanonicalStatus, SourceInstance


def _engine():
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    return create_engine(database_url)


@pytest.fixture()
def db():
    """Real Postgres (`aibrain_test`), fully isolated per test.

    The services under test call `session.commit()` internally (that's
    real, correct behavior for production use) - so plain rollback-at-
    teardown would not undo anything. Binding the Session to a
    connection held in an outer transaction, with
    `join_transaction_mode="create_savepoint"`, makes every internal
    `commit()` only release a SAVEPOINT; the outer transaction is
    rolled back here, so no synthetic row from any test in this module
    is ever left behind in `aibrain_test`.
    """
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
    """A syntactically valid, but entirely synthetic, 64-hex-char
    string - never a hash of any real file."""
    return uuid.uuid4().hex + uuid.uuid4().hex


def _discovery_run(db: Session, kind: DiscoveryRunKind = DiscoveryRunKind.D1_DUPLICATE_ANALYSIS) -> DiscoveryRun:
    run = DiscoveryRun(
        run_kind=kind,
        source_root="/synthetic/not-a-real-t7-path",
        report_sha256=_unique_hash(),
        run_started_at=datetime.now(UTC) - timedelta(minutes=5),
        run_completed_at=datetime.now(UTC),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _classification_run(db: Session, discovery_run: DiscoveryRun) -> ClassificationRun:
    return ClassificationRunService(db).start_run(
        classifier_version="test-classifier-v1",
        d1_discovery_run_id=discovery_run.id,
    )


# -- DiscoveryRun / ClassificationRun -----------------------------------


def test_discovery_run_records_report_metadata(db: Session) -> None:
    run = _discovery_run(db)
    assert run.id is not None
    assert run.run_kind == DiscoveryRunKind.D1_DUPLICATE_ANALYSIS


def test_classification_run_requires_at_least_one_discovery_run_service_level(
    db: Session,
) -> None:
    with pytest.raises(ValueError, match="at least one DiscoveryRun"):
        ClassificationRunService(db).start_run(classifier_version="v1")


def test_classification_run_requires_at_least_one_discovery_run_db_level(
    db: Session,
) -> None:
    """Bypassing the service and inserting directly must still be
    rejected - the CHECK constraint is the real guarantee, the service
    is a convenience, not the only line of defense."""
    run = ClassificationRun(
        classifier_version="v1",
        started_at=datetime.now(UTC),
    )
    db.add(run)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_classification_run_accepts_only_d1(db: Session) -> None:
    d1 = _discovery_run(db)
    run = _classification_run(db, d1)
    assert run.d1_discovery_run_id == d1.id
    assert run.d0_discovery_run_id is None
    assert run.d2_discovery_run_id is None


# -- ContentIdentityGroup -------------------------------------------------


def test_content_identity_group_unique_per_identity_triple(db: Session) -> None:
    identity_hash = _unique_hash()
    service = ContentIdentityService(db)

    first = service.get_or_create_group(
        identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
        identity_algorithm=ContentIdentityAlgorithm.SHA256,
        identity_hash=identity_hash,
    )
    second = service.get_or_create_group(
        identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
        identity_algorithm=ContentIdentityAlgorithm.SHA256,
        identity_hash=identity_hash,
    )
    assert first.id == second.id

    count = db.execute(
        text(
            "SELECT COUNT(*) FROM content_identity_groups WHERE identity_hash = :h"
        ),
        {"h": identity_hash},
    ).scalar_one()
    assert count == 1


def test_content_identity_group_same_hash_different_kind_is_a_different_group(
    db: Session,
) -> None:
    """Proves the identity NAMESPACE is real: the same hex string under
    a different identity_kind must never be treated as the same
    identity - this is exactly the ambiguity round 4 of the design
    review was written to prevent."""
    identity_hash = _unique_hash()
    service = ContentIdentityService(db)

    extracted = service.get_or_create_group(
        identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
        identity_algorithm=ContentIdentityAlgorithm.SHA256,
        identity_hash=identity_hash,
    )
    source_bytes = service.get_or_create_group(
        identity_kind=ContentIdentityKind.SOURCE_BYTES,
        identity_algorithm=ContentIdentityAlgorithm.SHA256,
        identity_hash=identity_hash,
    )
    assert extracted.id != source_bytes.id


# -- SourceInstance + ProvenanceLink --------------------------------------


def test_source_instance_requires_at_least_one_link(db: Session) -> None:
    d1 = _discovery_run(db)
    run = _classification_run(db, d1)

    with pytest.raises(ValueError, match="at least one ProvenanceLink"):
        SourceInstanceService(db).create_instance(
            classification_run_id=run.id,
            root_t7_path="/synthetic/loose_file.txt",
            member_path=None,
            evidence_snapshot={"observed": True},
            chain=[],
        )


def test_source_instance_chain_root_must_be_t7_file(db: Session) -> None:
    d1 = _discovery_run(db)
    run = _classification_run(db, d1)

    with pytest.raises(ValueError, match="must be T7_FILE"):
        SourceInstanceService(db).create_instance(
            classification_run_id=run.id,
            root_t7_path="/synthetic/loose_file.txt",
            member_path=None,
            evidence_snapshot={},
            chain=[ProvenanceStep(kind=ProvenanceLinkKind.ARCHIVE_MEMBER, path="oops")],
        )


def test_source_instance_loose_file_creates_single_root_link(db: Session) -> None:
    d1 = _discovery_run(db)
    run = _classification_run(db, d1)

    instance = SourceInstanceService(db).create_instance(
        classification_run_id=run.id,
        root_t7_path="/synthetic/loose_file.txt",
        member_path=None,
        evidence_snapshot={"source_file_hash": _unique_hash()},
        chain=[ProvenanceStep(kind=ProvenanceLinkKind.T7_FILE, path="/synthetic/loose_file.txt")],
    )

    links = (
        db.query(ProvenanceLink)
        .filter(ProvenanceLink.source_instance_id == instance.id)
        .order_by(ProvenanceLink.sequence_index)
        .all()
    )
    assert len(links) == 1
    assert links[0].kind == ProvenanceLinkKind.T7_FILE
    assert links[0].parent_link_id is None
    assert instance.content_identity_group_id is None  # deferred/unknown at creation


def test_source_instance_nested_archive_creates_full_chain(db: Session) -> None:
    """A file two archives deep: t7_file -> archive_member -> archive_member,
    exactly the shape described in the frozen design."""
    d1 = _discovery_run(db)
    run = _classification_run(db, d1)

    instance = SourceInstanceService(db).create_instance(
        classification_run_id=run.id,
        root_t7_path="/synthetic/outer.zip",
        member_path="inner.7z//deep/file.txt",
        evidence_snapshot={},
        chain=[
            ProvenanceStep(kind=ProvenanceLinkKind.T7_FILE, path="/synthetic/outer.zip"),
            ProvenanceStep(kind=ProvenanceLinkKind.ARCHIVE_MEMBER, path="inner.7z"),
            ProvenanceStep(kind=ProvenanceLinkKind.ARCHIVE_MEMBER, path="deep/file.txt"),
        ],
    )

    links = (
        db.query(ProvenanceLink)
        .filter(ProvenanceLink.source_instance_id == instance.id)
        .order_by(ProvenanceLink.sequence_index)
        .all()
    )
    assert [link.sequence_index for link in links] == [0, 1, 2]
    assert links[0].parent_link_id is None
    assert links[1].parent_link_id == links[0].id
    assert links[2].parent_link_id == links[1].id


def test_provenance_link_root_shape_check_constraint_rejects_wrong_kind_at_zero(
    db: Session,
) -> None:
    """Bypassing SourceInstanceService's own validation and inserting
    directly: sequence_index=0 with kind=ARCHIVE_MEMBER must still be
    rejected by the database itself."""
    d1 = _discovery_run(db)
    run = _classification_run(db, d1)
    instance = SourceInstance(
        classification_run_id=run.id,
        root_t7_path="/synthetic/x.txt",
        evidence_snapshot={},
    )
    db.add(instance)
    db.flush()

    bad_link = ProvenanceLink(
        source_instance_id=instance.id,
        parent_link_id=None,
        sequence_index=0,
        kind=ProvenanceLinkKind.ARCHIVE_MEMBER,
        path="wrong",
    )
    db.add(bad_link)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_provenance_link_root_shape_check_constraint_rejects_orphan_member(
    db: Session,
) -> None:
    """The gap closed by review round 6: an ARCHIVE_MEMBER row at a
    non-zero sequence_index with NO parent must be rejected. The
    constraint's first version only forbade sequence_index=0 from
    being anything other than a parentless T7_FILE root - it did not
    require a non-root row to actually HAVE a parent, so a
    "sequence_index=1, kind=ARCHIVE_MEMBER, parent_link_id=NULL" row
    would previously have been silently accepted as a valid,
    structurally broken, parentless "orphan" link."""
    d1 = _discovery_run(db)
    run = _classification_run(db, d1)
    instance = SourceInstance(
        classification_run_id=run.id,
        root_t7_path="/synthetic/orphan.zip",
        evidence_snapshot={},
    )
    db.add(instance)
    db.flush()

    orphan_link = ProvenanceLink(
        source_instance_id=instance.id,
        parent_link_id=None,
        sequence_index=1,
        kind=ProvenanceLinkKind.ARCHIVE_MEMBER,
        path="orphan-member",
    )
    db.add(orphan_link)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_provenance_link_root_shape_check_constraint_accepts_a_valid_non_root_link(
    db: Session,
) -> None:
    """Confirms the tightened CHECK constraint does not also reject the
    legitimate shape: sequence_index > 0, kind=ARCHIVE_MEMBER, with an
    actual parent."""
    d1 = _discovery_run(db)
    run = _classification_run(db, d1)
    instance = SourceInstance(
        classification_run_id=run.id,
        root_t7_path="/synthetic/valid_chain.zip",
        evidence_snapshot={},
    )
    db.add(instance)
    db.flush()

    root_link = ProvenanceLink(
        source_instance_id=instance.id,
        parent_link_id=None,
        sequence_index=0,
        kind=ProvenanceLinkKind.T7_FILE,
        path="/synthetic/valid_chain.zip",
    )
    db.add(root_link)
    db.flush()

    child_link = ProvenanceLink(
        source_instance_id=instance.id,
        parent_link_id=root_link.id,
        sequence_index=1,
        kind=ProvenanceLinkKind.ARCHIVE_MEMBER,
        path="member.txt",
    )
    db.add(child_link)
    db.commit()  # must not raise

    db.refresh(child_link)
    assert child_link.parent_link_id == root_link.id


# -- Write-once content_identity_group_id ---------------------------------


def test_assign_content_identity_is_write_once(db: Session) -> None:
    d1 = _discovery_run(db)
    run = _classification_run(db, d1)
    instance = SourceInstanceService(db).create_instance(
        classification_run_id=run.id,
        root_t7_path="/synthetic/deferred.txt",
        member_path=None,
        evidence_snapshot={},
        chain=[ProvenanceStep(kind=ProvenanceLinkKind.T7_FILE, path="/synthetic/deferred.txt")],
    )
    assert instance.content_identity_group_id is None

    identity_service = ContentIdentityService(db)
    group_a = identity_service.get_or_create_group(
        identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
        identity_algorithm=ContentIdentityAlgorithm.SHA256,
        identity_hash=_unique_hash(),
    )
    identity_service.assign_content_identity(instance.id, group_a)

    db.refresh(instance)
    assert instance.content_identity_group_id == group_a.id

    group_b = identity_service.get_or_create_group(
        identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
        identity_algorithm=ContentIdentityAlgorithm.SHA256,
        identity_hash=_unique_hash(),
    )
    with pytest.raises(ValueError, match="write-once"):
        identity_service.assign_content_identity(instance.id, group_b)

    # Still the FIRST group - the second attempt affected zero rows.
    db.refresh(instance)
    assert instance.content_identity_group_id == group_a.id


# -- Canonical status -------------------------------------------------------


def test_canonical_status_requires_evidence_at_db_level(db: Session) -> None:
    """Bypassing CanonicalDecisionService entirely: setting CANONICAL
    with no reason/decided_by/decided_at must be rejected by the CHECK
    constraint itself, not merely by the service's own validation."""
    d1 = _discovery_run(db)
    run = _classification_run(db, d1)
    instance = SourceInstanceService(db).create_instance(
        classification_run_id=run.id,
        root_t7_path="/synthetic/needs_decision.txt",
        member_path=None,
        evidence_snapshot={},
        chain=[ProvenanceStep(kind=ProvenanceLinkKind.T7_FILE, path="/synthetic/needs_decision.txt")],
    )
    instance.canonical_status = CanonicalStatus.CANONICAL
    db.add(instance)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_canonical_decision_service_requires_reason_and_decided_by(db: Session) -> None:
    d1 = _discovery_run(db)
    run = _classification_run(db, d1)
    instance = SourceInstanceService(db).create_instance(
        classification_run_id=run.id,
        root_t7_path="/synthetic/needs_decision2.txt",
        member_path=None,
        evidence_snapshot={},
        chain=[ProvenanceStep(kind=ProvenanceLinkKind.T7_FILE, path="/synthetic/needs_decision2.txt")],
    )

    with pytest.raises(ValueError, match="reason and a decided_by"):
        CanonicalDecisionService(db).decide(
            instance, status=CanonicalStatus.CANONICAL, reason="", decided_by=""
        )


def test_canonical_decision_service_records_a_valid_decision(db: Session) -> None:
    d1 = _discovery_run(db)
    run = _classification_run(db, d1)
    instance = SourceInstanceService(db).create_instance(
        classification_run_id=run.id,
        root_t7_path="/synthetic/decide_me.txt",
        member_path=None,
        evidence_snapshot={},
        chain=[ProvenanceStep(kind=ProvenanceLinkKind.T7_FILE, path="/synthetic/decide_me.txt")],
    )

    decided = CanonicalDecisionService(db).decide(
        instance,
        status=CanonicalStatus.NON_CANONICAL,
        reason="known-stale backup fragment",
        decided_by="human:test-suite",
    )
    assert decided.canonical_status == CanonicalStatus.NON_CANONICAL
    assert decided.canonical_status_reason == "known-stale backup fragment"
    assert decided.canonical_status_decided_at is not None


def test_marking_one_instance_canonical_does_not_affect_a_sibling(db: Session) -> None:
    """The specific invariant review round 4 required: NON_CANONICAL
    never follows automatically from a sibling being marked CANONICAL."""
    d1 = _discovery_run(db)
    run = _classification_run(db, d1)
    service = SourceInstanceService(db)

    a = service.create_instance(
        classification_run_id=run.id,
        root_t7_path="/synthetic/sibling_a.txt",
        member_path=None,
        evidence_snapshot={},
        chain=[ProvenanceStep(kind=ProvenanceLinkKind.T7_FILE, path="/synthetic/sibling_a.txt")],
    )
    b = service.create_instance(
        classification_run_id=run.id,
        root_t7_path="/synthetic/sibling_b.txt",
        member_path=None,
        evidence_snapshot={},
        chain=[ProvenanceStep(kind=ProvenanceLinkKind.T7_FILE, path="/synthetic/sibling_b.txt")],
    )

    CanonicalDecisionService(db).decide(
        a, status=CanonicalStatus.CANONICAL, reason="picked", decided_by="human:test-suite"
    )

    db.refresh(b)
    assert b.canonical_status == CanonicalStatus.UNRESOLVED


# -- Document.content_identity_group_id -----------------------------------


def test_document_content_identity_group_id_is_unique(db: Session) -> None:
    group = ContentIdentityService(db).get_or_create_group(
        identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
        identity_algorithm=ContentIdentityAlgorithm.SHA256,
        identity_hash=_unique_hash(),
    )

    doc_a = Document(
        title="a.txt",
        source="/documents/imports/1/a.txt",
        source_type="txt",
        content_identity_group_id=group.id,
    )
    db.add(doc_a)
    db.commit()

    doc_b = Document(
        title="b.txt",
        source="/documents/imports/1/b.txt",
        source_type="txt",
        content_identity_group_id=group.id,
    )
    db.add(doc_b)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
