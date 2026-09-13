from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.provenance_link import ProvenanceLink, ProvenanceLinkKind
from app.models.source_instance import SourceInstance


@dataclass(slots=True, frozen=True)
class ProvenanceStep:
    """One step of an ancestry chain, root first."""

    kind: ProvenanceLinkKind
    path: str


class SourceInstanceService:
    """Creates a SourceInstance together with its full ProvenanceLink
    ancestry chain, atomically, in one transaction.

    Never touches the T7: `chain` and `evidence_snapshot` are supplied
    by the caller (the classification stage), already derived from
    already-produced D0/D1/D2 report data.
    """

    def __init__(self, db: Session):
        self.db = db

    def create_instance(
        self,
        *,
        classification_run_id: int,
        root_t7_path: str,
        member_path: str | None,
        evidence_snapshot: dict,
        chain: list[ProvenanceStep],
    ) -> SourceInstance:
        if not chain:
            raise ValueError(
                "A SourceInstance requires at least one ProvenanceLink "
                "(the t7_file root)."
            )
        if chain[0].kind != ProvenanceLinkKind.T7_FILE:
            raise ValueError("The first ProvenanceStep must be T7_FILE.")
        for step in chain[1:]:
            if step.kind != ProvenanceLinkKind.ARCHIVE_MEMBER:
                raise ValueError(
                    "Every ProvenanceStep after the root must be ARCHIVE_MEMBER."
                )

        instance = SourceInstance(
            classification_run_id=classification_run_id,
            root_t7_path=root_t7_path,
            member_path=member_path,
            evidence_snapshot=evidence_snapshot,
        )
        self.db.add(instance)
        self.db.flush()  # assigns instance.id without committing yet

        parent_link_id: int | None = None
        for sequence_index, step in enumerate(chain):
            link = ProvenanceLink(
                source_instance_id=instance.id,
                parent_link_id=parent_link_id,
                sequence_index=sequence_index,
                kind=step.kind,
                path=step.path,
            )
            self.db.add(link)
            self.db.flush()
            parent_link_id = link.id

        self.db.commit()
        self.db.refresh(instance)
        return instance
