from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from app.classification.deterministic_selector import (
    ORDERING_VERSION,
    CandidateObservation,
    SelectionEnvelope,
    classify,
    select as run_selection,
    select_archive_duplicate_deferrals,
)
from app.classification.policy_evaluator import BatchClassPolicy
from app.classification.selection_fingerprint import compute_selection_fingerprint
from app.models.classification_run import ClassificationRun
from app.models.discovery_run import DiscoveryRun
from app.models.ingestion_batch import IngestionBatch
from app.models.provenance_link import ProvenanceLink, ProvenanceLinkKind
from app.models.source_instance import SourceInstance

_LOCK_NAMESPACE = "ingestion_batch_creation:discovery_run"


def _advisory_lock_key(discovery_run_id: int) -> int:
    """Deterministic 64-bit signed key derived from a NAMESPACED string,
    never the raw discovery_run_id integer directly - avoids collision
    with any future, unrelated use of advisory locks elsewhere in this
    codebase that might otherwise share a small integer id space
    (frozen implementation design, point 4)."""
    namespaced = f"{_LOCK_NAMESPACE}:{discovery_run_id}"
    digest = hashlib.sha256(namespaced.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


class BatchCreationService:
    """Creates one IngestionBatch from an existing D0 DiscoveryRun's
    observed candidates, per "Scaled Real-T7 Ingestion - Implementation
    Design Pass" (`2fab4b3`) point 2.

    ATOMICITY, stated precisely: this service constructs ClassificationRun/
    SourceInstance/ProvenanceLink rows DIRECTLY (via `db.add()`/`db.flush()`),
    deliberately NOT by calling `ClassificationRunService.start_run()` or
    `SourceInstanceService.create_instance()` - both of those commit
    internally as their own, correct, established contract for their
    existing callers (each stage of the ingestion pipeline commits its
    own unit of work). Reusing them here would call `db.commit()` once
    per selected candidate, which would (a) release the advisory lock
    below at the FIRST commit rather than at the end of this whole
    operation, and (b) leave committed, orphaned SourceInstance rows if
    a LATER step (e.g. IngestionBatch creation) then failed - exactly
    the partial-membership outcome the frozen design forbids. This is a
    deliberate, minimal duplication of row-construction logic for the
    single-link-chain (root-only, no archive nesting) case this
    milestone's candidates are always in - it does not modify, and is
    not a comment on, those existing services, which remain correct and
    unchanged for their own callers.

    Only a single `db.commit()` exists in this service, at the very
    end, once every row (ClassificationRun, every selected
    SourceInstance + its one-link ProvenanceLink chain, and the
    IngestionBatch itself) has been added and flushed. Any exception
    anywhere before that point leaves zero durable side effects.

    NOT built from a real D0/D1 report in this milestone: `candidates`
    is supplied by the caller (today's D0 report format has no full
    per-file enumeration to read - see `CandidateObservation`'s
    docstring). No archive is ever opened, and no archive member is
    ever materialized here - a selected archive remains a container-only
    SourceInstance (member materialization is a future, archive-
    extraction milestone's concern).
    """

    def __init__(self, db: Session):
        self.db = db

    def create_batch(
        self,
        *,
        discovery_run: DiscoveryRun,
        candidates: list[CandidateObservation],
        policy: BatchClassPolicy,
        max_source_instances: int,
        max_source_bytes: int,
        max_extracted_bytes: int | None,
        max_embeddings: int,
        max_runtime_seconds: int,
        classifier_version: str,
    ) -> IngestionBatch | None:
        try:
            self.db.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _advisory_lock_key(discovery_run.id)},
            )

            already_materialized = self._already_materialized_paths(discovery_run.id)
            eligible_observations = [
                c
                for c in candidates
                if (c.root_t7_path, c.member_path) not in already_materialized
            ]

            classified = [classify(observation) for observation in eligible_observations]

            deferred_paths = select_archive_duplicate_deferrals(classified)
            classified = [
                c for c in classified if c.observation.root_t7_path not in deferred_paths
            ]

            envelope = SelectionEnvelope(
                max_source_instances=max_source_instances,
                max_source_bytes=max_source_bytes,
            )
            result = run_selection(classified, policy, envelope)

            if not result.selected:
                self.db.rollback()
                return None

            classification_run = ClassificationRun(
                classifier_version=classifier_version,
                d0_discovery_run_id=discovery_run.id,
                started_at=datetime.now(UTC),
            )
            self.db.add(classification_run)
            self.db.flush()  # assigns classification_run.id, no commit

            for candidate in result.selected:
                instance = SourceInstance(
                    classification_run_id=classification_run.id,
                    root_t7_path=candidate.observation.root_t7_path,
                    member_path=candidate.observation.member_path,
                    # Dedicated, queryable classification state - the
                    # frozen model, not a JSONB-only representation
                    # (corrected per review; see SourceInstance's class
                    # docstring for the immutability/nullability contract).
                    source_category=candidate.source_category,
                    workload_category=candidate.workload_category,
                    risk_tier_estimated=candidate.risk_tier_estimated,
                    # evidence_snapshot holds ONLY supporting evidence -
                    # never a second copy of the classification decision
                    # itself, which would create two sources of truth.
                    evidence_snapshot={
                        "d0_declared_size_bytes": candidate.observation.declared_size_bytes,
                        "selection_policy_version": policy.selection_policy_version,
                        "d1_duplicate_group_id": candidate.observation.d1_duplicate_group_id,
                    },
                )
                self.db.add(instance)
                self.db.flush()  # assigns instance.id, no commit

                root_link = ProvenanceLink(
                    source_instance_id=instance.id,
                    parent_link_id=None,
                    sequence_index=0,
                    kind=ProvenanceLinkKind.T7_FILE,
                    path=candidate.observation.root_t7_path,
                )
                self.db.add(root_link)
                self.db.flush()

            fingerprint = compute_selection_fingerprint(
                d0_report_sha256=discovery_run.report_sha256,
                selection_policy_version=policy.selection_policy_version,
                ordering_version=ORDERING_VERSION,
                max_source_instances=max_source_instances,
                max_source_bytes=max_source_bytes,
                max_extracted_bytes=max_extracted_bytes,
                max_embeddings=max_embeddings,
                max_runtime_seconds=max_runtime_seconds,
                selected_paths=[
                    (c.observation.root_t7_path, c.observation.member_path)
                    for c in result.selected
                ],
            )

            batch = IngestionBatch(
                classification_run_id=classification_run.id,
                max_source_instances=max_source_instances,
                max_source_bytes=max_source_bytes,
                max_extracted_bytes=max_extracted_bytes,
                max_embeddings=max_embeddings,
                max_runtime_seconds=max_runtime_seconds,
                eligible_source_count=result.eligible_count,
                policy_filtered_count=result.policy_filtered_count,
                selectable_count=result.selectable_count,
                source_instances_selected=len(result.selected),
                source_bytes_selected=result.source_bytes_selected,
                selection_fingerprint=fingerprint,
                selection_policy_version=policy.selection_policy_version,
                ordering_version=ORDERING_VERSION,
            )
            self.db.add(batch)
            self.db.commit()
            self.db.refresh(batch)
            return batch
        except Exception:
            self.db.rollback()
            raise

    def _already_materialized_paths(self, discovery_run_id: int) -> set[tuple[str, str | None]]:
        """Discovery-run-SCOPED eligibility, per the frozen architecture:
        NOT "no SourceInstance exists for this path anywhere, ever" -
        only observations already materialized under a ClassificationRun
        tied to THIS SAME DiscoveryRun. A different DiscoveryRun's own,
        later ClassificationRun may legitimately re-observe the exact
        same real path (path != physical occurrence != content
        identity) - it simply is not excluded by this query at all,
        since it is scoped to a different discovery_run_id entirely."""
        rows = self.db.execute(
            select(SourceInstance.root_t7_path, SourceInstance.member_path)
            .join(ClassificationRun, SourceInstance.classification_run_id == ClassificationRun.id)
            .where(
                or_(
                    ClassificationRun.d0_discovery_run_id == discovery_run_id,
                    ClassificationRun.d1_discovery_run_id == discovery_run_id,
                    ClassificationRun.d2_discovery_run_id == discovery_run_id,
                )
            )
        ).all()
        return {(row[0], row[1]) for row in rows}
