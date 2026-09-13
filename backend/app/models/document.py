from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )

    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    source: Mapped[str] = mapped_column(
        String(1024),
        nullable=False,
    )

    source_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    # SHA-256 hex digest of the file's raw bytes, for exact-duplicate
    # detection. Nullable: only computed when ingestion has real file
    # access (DocumentIngestor); a document created via POST /documents
    # (metadata only, no guaranteed file access) may not have one.
    # Deliberately not unique - duplicates are exactly what this is for.
    content_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
    )

    import_job_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("import_jobs.id"),
        nullable=True,
        index=True,
    )

    # One ContentIdentityGroup represents one ingestible content
    # identity, and at most one derived Document currently represents
    # that identity - the UNIQUE constraint is a statement about how
    # many Documents exist TODAY for a given identity (one, at most),
    # not a claim that two semantically different representations can
    # never relate to each other. A future logical-document layer, if
    # built, would need a new mapping table above this one, not a
    # weaker constraint here. Nullable because existing rows (from
    # prior personal-corpus import testing, if any) predate this
    # column and this migration is purely additive - new ingestion
    # code should always populate it going forward. `content_hash`
    # above is kept, not removed; its value must always equal the
    # owning group's identity_hash (documented invariant, denormalized
    # for self-description, not DB-enforced in this milestone).
    content_identity_group_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("content_identity_groups.id"),
        nullable=True,
        unique=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )
