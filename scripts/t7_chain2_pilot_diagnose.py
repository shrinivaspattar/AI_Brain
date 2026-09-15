#!/usr/bin/env python3
"""M32 pilot diagnostic - read-only. Prints why a batch's
successful_ingestion_count came back lower than expected, by walking
the actual SourceInstance/ContentIdentityGroup/IngestionAttempt rows
for a given classification_run_id.

Usage:
    python scripts/t7_chain2_pilot_diagnose.py <classification_run_id>
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.models.content_identity_group import ContentIdentityGroup  # noqa: E402
from app.models.ingestion_attempt import IngestionAttempt  # noqa: E402
from app.models.source_instance import SourceInstance  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python t7_chain2_pilot_diagnose.py <classification_run_id>")
    run_id = int(sys.argv[1])

    engine = create_engine(make_url(settings.DATABASE_URL).set(database="aibrain"))
    with Session(engine) as db:
        instances = db.execute(
            select(SourceInstance).where(SourceInstance.classification_run_id == run_id)
        ).scalars().all()

        if not instances:
            print(f"No SourceInstance rows found for classification_run_id={run_id}")
            return

        for instance in instances:
            print(f"SourceInstance id={instance.id} content_identity_group_id={instance.content_identity_group_id}")

        group_ids = {i.content_identity_group_id for i in instances if i.content_identity_group_id is not None}
        for group_id in group_ids:
            group = db.get(ContentIdentityGroup, group_id)
            print(f"\nContentIdentityGroup id={group.id} pipeline_state={group.pipeline_state.value}")

            attempts = db.execute(
                select(IngestionAttempt)
                .where(IngestionAttempt.content_identity_group_id == group_id)
                .order_by(IngestionAttempt.id)
            ).scalars().all()
            for a in attempts:
                print(
                    f"  Attempt id={a.id} stage={a.attempted_stage.value} outcome={a.outcome.value} "
                    f"retryable={a.retryable} failure_code={a.failure_code.value if a.failure_code else None} "
                    f"failure_detail={a.failure_detail!r}"
                )

        instance_ids = [i.id for i in instances]
        instance_attempts = db.execute(
            select(IngestionAttempt)
            .where(IngestionAttempt.source_instance_id.in_(instance_ids))
            .order_by(IngestionAttempt.id)
        ).scalars().all()
        if instance_attempts:
            print("\nSource-instance-level attempts (archive/identity-resolution stage):")
            for a in instance_attempts:
                print(
                    f"  Attempt id={a.id} source_instance_id={a.source_instance_id} "
                    f"stage={a.attempted_stage.value} outcome={a.outcome.value} "
                    f"failure_code={a.failure_code.value if a.failure_code else None} "
                    f"failure_detail={a.failure_detail!r}"
                )


if __name__ == "__main__":
    main()
