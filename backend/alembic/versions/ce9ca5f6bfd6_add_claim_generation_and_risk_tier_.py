"""add claim_generation and risk_tier_actual to source_instances

Revision ID: ce9ca5f6bfd6
Revises: b33606f7d5e8
Create Date: 2026-09-14 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'ce9ca5f6bfd6'
down_revision: Union[str, Sequence[str], None] = 'b33606f7d5e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Scaled Real-T7 Ingestion, Implementation Milestone 5 (Archive
    Processing / Extraction). Purely additive: two new nullable/
    defaulted columns on source_instances, no data migration, safe for
    existing rows.

    claim_generation: generalizes ContentIdentityGroup.claim_generation
    (Milestone 1) to SourceInstance, closing the ABA hole a delayed
    (not merely crashed) worker's own claim release could otherwise
    exploit - see the Milestone 5 design correction pass.

    risk_tier_actual: reuses the EXISTING risk_tier_estimated Postgres
    enum type (create_type=False - the type already exists from
    Milestone 2's migration `aa3ce58f6599`) - no new enum type created.
    """
    op.add_column(
        'source_instances',
        sa.Column('claim_generation', sa.Integer(), nullable=False, server_default='0'),
    )
    op.add_column(
        'source_instances',
        sa.Column(
            'risk_tier_actual',
            postgresql.ENUM(
                'LOW', 'MEDIUM', 'HIGH', 'EXTREME',
                name='risk_tier_estimated',
                create_type=False,
            ),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column('source_instances', 'risk_tier_actual')
    op.drop_column('source_instances', 'claim_generation')
