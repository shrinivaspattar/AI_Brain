from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import CheckConstraint, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class ProvenanceLinkKind(str, Enum):
    T7_FILE = "t7_file"
    ARCHIVE_MEMBER = "archive_member"


class ProvenanceLink(Base):
    """One node in a SourceInstance's archive-ancestry chain: T7 file ->
    archive -> nested archive -> member -> ... A real, queryable
    structure (self-referential `parent_link_id` + `sequence_index`)
    replacing an earlier, rejected "opaque archive_chain string list"
    design - "every SourceInstance whose chain passed through this
    archive path" is a plain indexed query
    (`WHERE path = :archive_path`), not a JSONB containment scan.

    Fully immutable once written: an ancestry chain is a fixed
    historical fact about one observation, never edited after creation.

    sequence_index=0 is always the T7_FILE root with no parent; every
    other position is an ARCHIVE_MEMBER WITH a parent - both halves are
    enforced by the CHECK constraint below (an earlier version of this
    constraint only forbade a non-root row from being T7_FILE-with-
    no-parent, but did not require a non-root row to actually HAVE a
    parent - that gap is closed here: an ARCHIVE_MEMBER row with
    parent_link_id IS NULL is now rejected at any sequence_index).

    Two related invariants are NOT DB-enforced (a single-row CHECK
    constraint cannot see sibling rows), and are instead guaranteed
    only by the one creation path that exists in this codebase
    (SourceInstanceService.create_instance, which always builds a
    chain in order, root first, each link's parent set to the
    immediately-preceding link's id):
    - that `parent_link_id` actually points to another link belonging
      to the SAME `source_instance_id`, at a strictly lower
      `sequence_index` (not a link from a different instance, and not
      a later one);
    - that `sequence_index` values are contiguous (0, 1, 2, ...) with
      no gaps.
    A direct INSERT that bypasses SourceInstanceService could violate
    either of these without tripping a database error - this is a
    named, accepted limitation of this milestone's schema, not an
    oversight, matching this project's existing precedent for
    cross-row invariants a single CHECK constraint cannot express
    (e.g. DuplicateReview's un-enforced "at least one member").
    """

    __tablename__ = "provenance_links"
    __table_args__ = (
        UniqueConstraint(
            "source_instance_id",
            "sequence_index",
            name="uq_provenance_links_source_instance_id_sequence_index",
        ),
        CheckConstraint(
            "(sequence_index = 0 AND kind = 'T7_FILE' AND parent_link_id IS NULL) "
            "OR (sequence_index > 0 AND kind = 'ARCHIVE_MEMBER' AND parent_link_id IS NOT NULL)",
            name="ck_provenance_links_root_shape",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    source_instance_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("source_instances.id"),
        nullable=False,
        index=True,
    )

    parent_link_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("provenance_links.id"),
        nullable=True,
    )

    # 0 for the root link, increasing per nesting level - makes
    # "immediate container" / "nesting depth" queries index-friendly
    # without a recursive CTE every time.
    sequence_index: Mapped[int] = mapped_column(Integer, nullable=False)

    kind: Mapped[ProvenanceLinkKind] = mapped_column(
        SQLEnum(ProvenanceLinkKind, name="provenance_link_kind"),
        nullable=False,
    )

    # T7 path for a t7_file link; member-internal path for an
    # archive_member link.
    path: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
