"""add MANUAL_ABORT batch stop reason

Revision ID: 7e0e781f8791
Revises: aa3ce58f6599
Create Date: 2026-09-14 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '7e0e781f8791'
down_revision: Union[str, Sequence[str], None] = 'aa3ce58f6599'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ORIGINAL_VALUES = (
    'SOURCE_WORK_EXHAUSTED', 'EXTRACTED_BYTES_ENVELOPE_EXHAUSTED',
    'EMBEDDINGS_ENVELOPE_EXHAUSTED', 'RUNTIME_BUDGET_EXCEEDED',
    'WORKSPACE_SOFT_STOP', 'POSTGRES_SOFT_STOP', 'MANUAL_PAUSE',
    'WORKSPACE_HARD_STOP', 'POSTGRES_HARD_STOP',
    'OLLAMA_PERSISTENTLY_UNREACHABLE', 'SAFETY_INVARIANT_VIOLATION_DETECTED',
)


def upgrade() -> None:
    """Adds the single new `MANUAL_ABORT` value to the already-live
    `batch_stop_reason` enum type (Implementation Milestone 3's final-
    correction pass - see `IngestionBatch.BatchStopReason`'s docstring
    for why this is a genuinely new, distinct reason rather than an
    overload of an existing one). Purely additive: no column, table, or
    CHECK constraint changes - the existing `ck_ingestion_batches_stop_
    reason_matches_status` constraint checks NULL-ness against `status`
    only, never against specific enum values, so it needs no change.

    `ALTER TYPE ... ADD VALUE` is safe to run inside Alembic's own
    migration transaction here because the new value is not read or
    written by this same migration - Postgres only forbids using a
    freshly-added enum value within the SAME transaction that added it,
    not merely committing the addition itself."""
    op.execute("ALTER TYPE batch_stop_reason ADD VALUE IF NOT EXISTS 'MANUAL_ABORT'")


def downgrade() -> None:
    """Postgres has no `ALTER TYPE ... DROP VALUE` - removing an enum
    value requires rebuilding the type from scratch. This will fail
    loudly (a real, honest failure, not a silent data loss) if any row
    already has `stop_reason = 'MANUAL_ABORT'` at downgrade time, since
    the final `USING` cast has nothing valid to map that value to -
    exactly the correct behavior: a downgrade that cannot represent
    existing data must refuse, never quietly drop it."""
    op.execute("ALTER TYPE batch_stop_reason RENAME TO batch_stop_reason_old")
    original_values_sql = ", ".join(f"'{value}'" for value in _ORIGINAL_VALUES)
    op.execute(f"CREATE TYPE batch_stop_reason AS ENUM ({original_values_sql})")
    op.execute(
        "ALTER TABLE ingestion_batches "
        "ALTER COLUMN stop_reason TYPE batch_stop_reason "
        "USING stop_reason::text::batch_stop_reason"
    )
    op.execute("DROP TYPE batch_stop_reason_old")
