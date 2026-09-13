from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.content_identity_group import (
    ContentIdentityAlgorithm,
    ContentIdentityGroup,
    ContentIdentityKind,
    ContentPipelineState,
)
from app.models.source_instance import SourceInstance

# The one, specific UNIQUE constraint this race-recovery path is
# entitled to swallow. Any other IntegrityError (a FK violation, an
# unrelated unique constraint, a NOT NULL violation surfaced late,
# etc.) is a real bug and must propagate, never be silently
# reinterpreted as "lost the identity race."
_IDENTITY_UNIQUE_CONSTRAINT_NAME = "uq_content_identity_groups_identity"


class ContentIdentityService:
    """Establishes and assigns content identity (identity layer 2).

    `get_or_create_group` is the concurrency-critical operation this
    whole design depends on: two concurrent callers racing to establish
    the SAME (identity_kind, identity_algorithm, identity_hash) must
    converge on ONE ContentIdentityGroup row, never create two. This is
    NOT proven merely by the presence of the UNIQUE constraint - it is
    proven by a real database concurrency test (see
    tests/integration/test_content_identity_concurrency.py) that races
    two genuinely concurrent sessions against the same identity and
    asserts both converge on the same row.

    The approach: check-then-insert, relying on the database's own
    UNIQUE constraint as the actual race-safety mechanism (not a
    Python-side lock, which cannot protect against two separate
    processes/connections). Whichever transaction's INSERT commits
    first wins; the loser's INSERT fails with IntegrityError, at which
    point it re-queries and returns the winner's row instead of
    raising. Both callers succeed; both get the same group.

    TRANSACTION CONTRACT: this method calls `session.commit()` (and,
    on the race path, `session.rollback()`) directly - it assumes it
    owns the ENTIRE current transaction on the given Session, exactly
    like every other service in this codebase (DocumentService.
    create_document, SourceInstanceService.create_instance, etc. all
    do the same). A `rollback()` here discards ALL uncommitted work on
    that session, not just this method's own attempted INSERT - so
    this method must only ever be called as its own, standalone unit
    of work on a session with no other pending uncommitted changes. It
    is NOT safe to call in the middle of a larger, not-yet-committed
    transaction on the same session without first wrapping it in its
    own SAVEPOINT (e.g. `session.begin_nested()`) - no caller in this
    codebase currently does that, so no such wrapping is added here;
    a future caller that needs atomic composition across multiple
    classification operations must add that nesting itself rather than
    assume this method provides it.

    ERROR SCOPE: only an IntegrityError raised specifically by
    `uq_content_identity_groups_identity` is treated as "lost the
    race." Any other IntegrityError (a foreign-key violation, a
    different unique constraint, anything else) is re-raised
    unchanged - this method never guesses that an unrelated database
    error means "someone else already has this identity."
    """

    def __init__(self, db: Session):
        self.db = db

    def get_or_create_group(
        self,
        *,
        identity_kind: ContentIdentityKind,
        identity_algorithm: ContentIdentityAlgorithm,
        identity_hash: str,
        initial_pipeline_state: ContentPipelineState = ContentPipelineState.DISCOVERED,
    ) -> ContentIdentityGroup:
        """`initial_pipeline_state` (added for the Controlled Ingestion
        Implementation gate) is used ONLY when this call actually
        creates a new row - it has no effect when an existing group is
        found and returned. Defaults to `DISCOVERED` (the model
        column's own default), preserving every existing caller's
        behavior unchanged. The ingestion pipeline passes `EXTRACTED`
        here specifically: by the time identity resolution or archive
        extraction has computed a hash, the underlying bytes have
        necessarily already been read - there is no separate
        "extraction" work left to claim for such a group, so it starts
        life past that step rather than needing a redundant EXTRACTING
        claim for work that's already done.
        """
        existing = self._find(identity_kind, identity_algorithm, identity_hash)
        if existing is not None:
            return existing

        group = ContentIdentityGroup(
            identity_kind=identity_kind,
            identity_algorithm=identity_algorithm,
            identity_hash=identity_hash,
            pipeline_state=initial_pipeline_state,
        )
        self.db.add(group)
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()

            constraint_name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            if constraint_name != _IDENTITY_UNIQUE_CONSTRAINT_NAME:
                # A different constraint failed (or the driver didn't
                # expose diagnostics) - this is a real bug, not a lost
                # identity race. Never reinterpret it as one.
                raise

            # Lost the race: another transaction's INSERT committed
            # first and now owns this identity. Not an error from the
            # caller's point of view - fetch and return the winner.
            winner = self._find(identity_kind, identity_algorithm, identity_hash)
            if winner is None:
                # Genuinely unexpected: the unique violation implies a
                # matching row exists, so failing to find it means
                # something other than this race caused the
                # IntegrityError. Surface the real problem.
                raise
            return winner

        self.db.refresh(group)
        return group

    def _find(
        self,
        identity_kind: ContentIdentityKind,
        identity_algorithm: ContentIdentityAlgorithm,
        identity_hash: str,
    ) -> ContentIdentityGroup | None:
        statement = select(ContentIdentityGroup).where(
            ContentIdentityGroup.identity_kind == identity_kind,
            ContentIdentityGroup.identity_algorithm == identity_algorithm,
            ContentIdentityGroup.identity_hash == identity_hash,
        )
        return self.db.scalars(statement).first()

    def assign_content_identity(
        self,
        source_instance_id: int,
        group: ContentIdentityGroup,
    ) -> None:
        """Sets SourceInstance.content_identity_group_id - WRITE-ONCE.

        Implemented as an UPDATE guarded by `WHERE content_identity_
        group_id IS NULL`, not a plain ORM attribute assignment: under
        Postgres's default READ COMMITTED isolation, this WHERE clause
        is re-evaluated against the latest committed row at UPDATE
        time, so a second attempt to set an already-set instance
        affects zero rows rather than silently overwriting the first
        value. A rowcount of zero means either the instance was already
        assigned or does not exist - both are refused identically,
        never silently.

        Under genuine concurrent contention (two sessions racing to
        assign DIFFERENT groups to the SAME instance), Postgres's
        row-level write lock serializes the two UPDATE statements: the
        second one to reach the row blocks until the first commits,
        then re-evaluates its WHERE clause against the now-committed
        (no longer NULL) value and affects zero rows - see
        test_source_instance_write_once_under_real_concurrent_sessions
        for a real two-thread proof, not just the sequential
        same-thread check elsewhere in this test suite.

        Same transaction contract as get_or_create_group (see that
        method's docstring): this commits the current transaction on
        `self.db` directly and must be called as its own standalone
        unit of work.
        """
        statement = (
            update(SourceInstance)
            .where(
                SourceInstance.id == source_instance_id,
                SourceInstance.content_identity_group_id.is_(None),
            )
            .values(content_identity_group_id=group.id)
        )
        result = self.db.execute(statement)
        self.db.commit()

        if result.rowcount == 0:
            raise ValueError(
                f"SourceInstance {source_instance_id} already has a "
                "content_identity_group_id set (write-once), or does not exist."
            )
