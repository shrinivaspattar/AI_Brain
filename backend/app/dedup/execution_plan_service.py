import hashlib
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dedup.review_service import DedupReviewService
from app.models.dedup_execution_plan import (
    DedupExecutionPlan,
    DedupExecutionPlanAction,
    DedupPlanActionType,
    DedupPlanStatus,
)
from app.models.dedup_review import DuplicateReviewStatus
from app.models.document import Document

DEFAULT_LIST_LIMIT = 100
_HASH_READ_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class FileObservation:
    exists: bool
    content_hash: str | None
    file_size: int | None


def _observe_file(path_str: str) -> FileObservation:
    """Read a file's current state from disk right now: does it exist,
    and if so, its SHA-256 and size. A plain read - this never writes,
    moves, renames, or deletes anything.

    Deliberately not a reuse of DocumentIngestor's private _hash_file
    (app/ingestion/document_ingestor.py): that helper swallows every
    failure into a bare None and never reports size or existence
    separately, but staleness comparison needs all three distinguished
    (missing vs. unreadable vs. present-but-changed).
    """
    path = Path(path_str)

    if not path.is_file():
        return FileObservation(exists=False, content_hash=None, file_size=None)

    digest = hashlib.sha256()

    try:
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(_HASH_READ_CHUNK_SIZE), b""):
                digest.update(block)
        size = path.stat().st_size
    except OSError:
        return FileObservation(exists=False, content_hash=None, file_size=None)

    return FileObservation(exists=True, content_hash=digest.hexdigest(), file_size=size)


def _type_matches(path_str: str, source_type: str) -> bool:
    """A cheap extra safety dimension beyond hash/size: does the file
    currently at this path even look like the same *kind* of file
    (matching Document.source_type)? A hash/size collision on a
    completely different file type is astronomically unlikely, but
    this check is nearly free and catches a very different real-world
    case - the path being reused for an unrelated file after the
    original was replaced. Suffix comparison only, mirroring the same
    prefix/suffix dispatch text_extractor.py already uses.
    """
    suffix = Path(path_str).suffix.lstrip(".").lower()
    return suffix == source_type.lower()


@dataclass(frozen=True)
class ActionValidity:
    action_id: int
    document_id: str
    source_path: str
    document_exists: bool
    path_changed: bool
    exists_now: bool
    type_matches: bool
    hash_matches: bool
    size_matches: bool
    is_valid: bool


@dataclass(frozen=True)
class PlanValidity:
    """The result of re-checking a plan against the filesystem right
    now. `is_valid` is the single answer a future executor must check
    before acting: False means ABORT - do not guess, do not substitute
    another file, do not continue partially.
    """

    plan_id: int
    canonical_document_exists: bool
    canonical_path_changed: bool
    canonical_exists_now: bool
    canonical_type_matches: bool
    canonical_hash_matches: bool
    canonical_size_matches: bool
    canonical_valid: bool
    actions: list[ActionValidity]
    is_valid: bool


class DedupExecutionPlanService:
    """Turns an already-APPROVED DuplicateReview into a persisted,
    inspectable dry-run execution plan - what *would* happen, to which
    files, and why. No executor exists anywhere in this codebase:
    generating a plan reads files (to observe their real current state)
    but never writes, moves, deletes, renames, quarantines, or
    overwrites anything.

    This is the DRY-RUN EXECUTION PLAN stage, strictly downstream of
    approval:

        Detection -> Recommendation -> Human Review -> Approval
            -> Dry-run Execution Plan (this service)
            -> Explicit Execution Authorization (not built)
            -> Filesystem Execution (not built)

    Deliberately not an extension of DeduplicationService.
    plan_exact_duplicate_cleanup (app/dedup/service.py): that method
    operates on raw, un-reviewed detection output (the Recommendation
    stage) and is stateless by design. This service operates strictly
    on an already-APPROVED DuplicateReview carrying an explicit
    human_selected_canonical_document_id (the Approval stage) and
    persists an immutable, auditable record of what it observed.
    Extending the former to also do the latter would collapse two
    stages this architecture deliberately keeps separate - detection
    recommendations and human decisions are not the same kind of fact.
    """

    def __init__(self, db: Session):
        self.db = db
        self.review_service = DedupReviewService(db)

    def generate_plan_for_review(self, review_id: int) -> DedupExecutionPlan:
        """Snapshot the current filesystem state for every document in
        an approved review's finding, and persist it as a new plan.

        Every call creates a brand-new row - regenerating a plan (e.g.
        after an earlier one went stale) is expected, not an error.
        Raises ValueError for every case where a plan cannot honestly
        be produced: review missing, not approved, approved without an
        explicit canonical choice (the near-duplicate "no decision yet"
        case), or a canonical that somehow isn't a member of its own
        review.
        """
        review = self.review_service.get_review(review_id)
        if review is None:
            raise ValueError(f"Duplicate review {review_id} not found")

        if review.status != DuplicateReviewStatus.APPROVED:
            raise ValueError(
                f"Duplicate review {review_id} is not approved "
                f"(status={review.status.value}) - a dry-run execution "
                "plan can only be generated for an approved review"
            )

        if review.human_selected_canonical_document_id is None:
            raise ValueError(
                f"Duplicate review {review_id} was approved without an "
                "explicit human-selected canonical document - it does not "
                "contain a decision sufficient to generate an execution "
                "plan (this is expected for a near-duplicate review "
                "approved with no canonical chosen; a canonical must never "
                "be inferred)"
            )

        pairs = self.review_service.get_review_members_with_documents(review_id)
        if not pairs:
            raise ValueError(
                f"Duplicate review {review_id} has no members - cannot "
                "generate a plan"
            )

        documents_by_id = {document.id: document for _member, document in pairs}
        canonical_document = documents_by_id.get(
            review.human_selected_canonical_document_id
        )
        if canonical_document is None:
            raise ValueError(
                f"'{review.human_selected_canonical_document_id}' is the "
                f"human-selected canonical for review {review_id} but is "
                "not one of its members - refusing to generate a plan "
                "against inconsistent data"
            )

        canonical_observation = _observe_file(canonical_document.source)

        plan = DedupExecutionPlan(
            review_id=review.id,
            canonical_document_id=canonical_document.id,
            canonical_source_path=canonical_document.source,
            canonical_observed_exists=canonical_observation.exists,
            canonical_observed_content_hash=canonical_observation.content_hash,
            canonical_observed_file_size=canonical_observation.file_size,
            status=DedupPlanStatus.GENERATED,
        )
        self.db.add(plan)
        self.db.flush()

        for document in documents_by_id.values():
            if document.id == canonical_document.id:
                continue

            observation = _observe_file(document.source)

            self.db.add(
                DedupExecutionPlanAction(
                    plan_id=plan.id,
                    document_id=document.id,
                    action=DedupPlanActionType.DELETE,
                    source_path=document.source,
                    target_document_id=canonical_document.id,
                    target_path=canonical_document.source,
                    observed_exists=observation.exists,
                    observed_content_hash=observation.content_hash,
                    observed_file_size=observation.file_size,
                    reason=(
                        f"Proposed for removal as a duplicate of the "
                        f"human-approved canonical '{canonical_document.title}' "
                        f"(document {canonical_document.id}), per duplicate "
                        f"review #{review.id}."
                    ),
                )
            )

        try:
            self.db.commit()
            self.db.refresh(plan)
            return plan
        except Exception:
            self.db.rollback()
            raise

    def get_plan(self, plan_id: int) -> DedupExecutionPlan | None:
        return self.db.get(DedupExecutionPlan, plan_id)

    def list_plans(
        self,
        review_id: int | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[DedupExecutionPlan]:
        statement = (
            select(DedupExecutionPlan)
            .order_by(DedupExecutionPlan.created_at.desc())
            .limit(limit)
        )

        if review_id is not None:
            statement = statement.where(DedupExecutionPlan.review_id == review_id)

        return list(self.db.scalars(statement))

    def get_plan_actions_with_documents(
        self,
        plan_id: int,
    ) -> list[tuple[DedupExecutionPlanAction, Document]]:
        actions = list(
            self.db.scalars(
                select(DedupExecutionPlanAction).where(
                    DedupExecutionPlanAction.plan_id == plan_id
                )
            )
        )

        if not actions:
            return []

        document_ids = {action.document_id for action in actions}
        documents = {
            document.id: document
            for document in self.db.scalars(
                select(Document).where(Document.id.in_(document_ids))
            )
        }

        return [
            (action, documents[action.document_id])
            for action in actions
            if action.document_id in documents
        ]

    def check_plan_validity(self, plan_id: int) -> PlanValidity:
        """Re-read the filesystem RIGHT NOW and compare it against what
        this plan observed at generation time. This is the mandatory
        checkpoint a future executor must pass before acting on a plan:
        if anything has changed - or the plan was already generated
        against a missing/unreadable file - the plan is invalid and
        must never be executed. Nothing here is cached or trusted from
        an earlier call; every invocation re-reads disk and the current
        Document rows from scratch.

        Checks, per file (path/type/size/hash, all "at minimum" per the
        design spec) plus two dimensions the naive version of this
        check would miss:
        - the Document row itself might have been deleted since the
          plan was generated (`document_exists`) - silently skipping a
          vanished document, the way listing/display endpoints
          reasonably do, would hide exactly the kind of change this
          check exists to catch.
        - the Document's `source` might have been updated to point
          somewhere else since generation (`path_changed`) - the plan
          only ever reads its own frozen path, so a changed live path
          must be flagged rather than silently ignored.
        """
        plan = self._get_plan_or_raise(plan_id)

        canonical_document = self.db.get(Document, plan.canonical_document_id)
        canonical_document_exists = canonical_document is not None
        canonical_path_changed = (
            not canonical_document_exists
            or canonical_document.source != plan.canonical_source_path
        )
        canonical_now = _observe_file(plan.canonical_source_path)
        canonical_type_matches = canonical_document_exists and _type_matches(
            plan.canonical_source_path, canonical_document.source_type
        )
        canonical_hash_matches = (
            canonical_now.exists
            and plan.canonical_observed_exists
            and canonical_now.content_hash == plan.canonical_observed_content_hash
        )
        canonical_size_matches = (
            canonical_now.exists
            and plan.canonical_observed_exists
            and canonical_now.file_size == plan.canonical_observed_file_size
        )
        canonical_valid = (
            canonical_document_exists
            and not canonical_path_changed
            and canonical_now.exists
            and plan.canonical_observed_exists
            and canonical_type_matches
            and canonical_hash_matches
            and canonical_size_matches
        )

        actions = list(
            self.db.scalars(
                select(DedupExecutionPlanAction).where(
                    DedupExecutionPlanAction.plan_id == plan_id
                )
            )
        )

        action_results = []
        for action in actions:
            document = self.db.get(Document, action.document_id)
            document_exists = document is not None
            path_changed = not document_exists or document.source != action.source_path

            now = _observe_file(action.source_path)
            type_matches = document_exists and _type_matches(
                action.source_path, document.source_type
            )
            hash_matches = (
                now.exists
                and action.observed_exists
                and now.content_hash == action.observed_content_hash
            )
            size_matches = (
                now.exists
                and action.observed_exists
                and now.file_size == action.observed_file_size
            )
            is_valid = (
                document_exists
                and not path_changed
                and now.exists
                and action.observed_exists
                and type_matches
                and hash_matches
                and size_matches
            )

            action_results.append(
                ActionValidity(
                    action_id=action.id,
                    document_id=action.document_id,
                    source_path=action.source_path,
                    document_exists=document_exists,
                    path_changed=path_changed,
                    exists_now=now.exists,
                    type_matches=type_matches,
                    hash_matches=hash_matches,
                    size_matches=size_matches,
                    is_valid=is_valid,
                )
            )

        return PlanValidity(
            plan_id=plan.id,
            canonical_document_exists=canonical_document_exists,
            canonical_path_changed=canonical_path_changed,
            canonical_exists_now=canonical_now.exists,
            canonical_type_matches=canonical_type_matches,
            canonical_hash_matches=canonical_hash_matches,
            canonical_size_matches=canonical_size_matches,
            canonical_valid=canonical_valid,
            actions=action_results,
            is_valid=canonical_valid and all(a.is_valid for a in action_results),
        )

    def _get_plan_or_raise(self, plan_id: int) -> DedupExecutionPlan:
        plan = self.get_plan(plan_id)

        if plan is None:
            raise ValueError(f"Dedup execution plan {plan_id} not found")

        return plan
