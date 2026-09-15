#!/usr/bin/env python3
"""M32 pilot: one-off, explicit, attended reset of a single
ContentIdentityGroup stuck at FAILED back to CHUNKED, so
PipelineEmbeddingService can claim and retry it.

This is deliberately a manual, one-time administrative action, not a
new automatic retry mechanism - Model A's own design (Milestone 11)
explicitly leaves the decision "does this repeatedly-failing item need
intervention" to an attended human operator, never to a counter. This
script is that operator's explicit intervention for exactly one named
group, nothing more.

Usage:
    python scripts/t7_chain2_pilot_reset_group.py <content_identity_group_id>
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.models.content_identity_group import ContentIdentityGroup, ContentPipelineState  # noqa: E402
from app.models.ingestion_batch import IngestionBatch  # noqa: E402,F401 - registers the FK target table


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python t7_chain2_pilot_reset_group.py <content_identity_group_id>")
    group_id = int(sys.argv[1])

    engine = create_engine(make_url(settings.DATABASE_URL).set(database="aibrain"))
    with Session(engine) as db:
        group = db.get(ContentIdentityGroup, group_id)
        if group is None:
            raise SystemExit(f"ContentIdentityGroup {group_id} not found")

        print(f"Before: id={group.id} pipeline_state={group.pipeline_state.value} "
              f"claimed_by={group.claimed_by} claimed_at={group.claimed_at}")

        if group.pipeline_state != ContentPipelineState.FAILED:
            raise SystemExit(
                f"Refusing to reset: pipeline_state is {group.pipeline_state.value}, not FAILED. "
                "This script only handles the specific stuck-at-FAILED case."
            )

        group.pipeline_state = ContentPipelineState.CHUNKED
        group.claimed_by = None
        group.claimed_at = None
        db.commit()
        db.refresh(group)

        print(f"After:  id={group.id} pipeline_state={group.pipeline_state.value}")


if __name__ == "__main__":
    main()
