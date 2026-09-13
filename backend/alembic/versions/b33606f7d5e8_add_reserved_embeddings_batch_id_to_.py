"""add reserved_embeddings_batch_id durable reservation ownership

Revision ID: b33606f7d5e8
Revises: 7e0e781f8791
Create Date: 2026-09-14 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b33606f7d5e8'
down_revision: Union[str, Sequence[str], None] = '7e0e781f8791'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Durable reservation ownership (Scaled Real-T7 Ingestion,
    Implementation Milestone 4's final correction, "Durable Reservation
    Ownership"). Purely additive: one new nullable column plus two CHECK
    constraints, no data migration, safe for existing rows (every
    existing row has `reserved_embeddings IS NULL`, which trivially
    satisfies both new constraints with the new column also NULL - see
    `app.models.content_identity_group.ContentIdentityGroup`'s docstring
    for the full invariant this closes)."""
    op.add_column(
        'content_identity_groups',
        sa.Column('reserved_embeddings_batch_id', sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        'fk_content_identity_groups_reserved_embeddings_batch_id',
        'content_identity_groups',
        'ingestion_batches',
        ['reserved_embeddings_batch_id'],
        ['id'],
    )
    op.create_check_constraint(
        'ck_content_identity_groups_reservation_ownership_consistent',
        'content_identity_groups',
        "(reserved_embeddings IS NULL AND reserved_embeddings_batch_id IS NULL) "
        "OR (reserved_embeddings IS NOT NULL AND reserved_embeddings_batch_id IS NOT NULL)",
    )
    op.create_check_constraint(
        'ck_content_identity_groups_reserved_embeddings_positive',
        'content_identity_groups',
        "reserved_embeddings IS NULL OR reserved_embeddings > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        'ck_content_identity_groups_reserved_embeddings_positive', 'content_identity_groups', type_='check'
    )
    op.drop_constraint(
        'ck_content_identity_groups_reservation_ownership_consistent', 'content_identity_groups', type_='check'
    )
    op.drop_constraint(
        'fk_content_identity_groups_reserved_embeddings_batch_id', 'content_identity_groups', type_='foreignkey'
    )
    op.drop_column('content_identity_groups', 'reserved_embeddings_batch_id')
