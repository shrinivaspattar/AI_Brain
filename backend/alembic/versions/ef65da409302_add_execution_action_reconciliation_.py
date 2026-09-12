"""add execution action reconciliation and recovery_claimed_at

Revision ID: ef65da409302
Revises: 02467162321c
Create Date: 2026-09-12 19:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'ef65da409302'
down_revision: Union[str, Sequence[str], None] = '02467162321c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'dedup_executions',
        sa.Column('recovery_claimed_at', sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        'dedup_execution_action_reconciliations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('audit_id', sa.Integer(), nullable=False),
        sa.Column(
            'verified_result',
            postgresql.ENUM(
                'SUCCESS', 'PRECONDITION_FAILED', 'FAILED', 'NOT_ATTEMPTED', 'UNKNOWN',
                name='dedup_execution_action_result',
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column('verified_by', sa.Text(), nullable=False),
        sa.Column('verification_method', sa.Text(), nullable=False),
        sa.Column('observed_content_hash', sa.String(length=64), nullable=True),
        sa.Column('observed_file_size', sa.Integer(), nullable=True),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['audit_id'], ['dedup_execution_action_audits.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('audit_id', name='uq_dedup_execution_action_reconciliations_audit_id'),
    )
    op.create_index(
        op.f('ix_dedup_execution_action_reconciliations_audit_id'),
        'dedup_execution_action_reconciliations',
        ['audit_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_dedup_execution_action_reconciliations_id'),
        'dedup_execution_action_reconciliations',
        ['id'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f('ix_dedup_execution_action_reconciliations_id'),
        table_name='dedup_execution_action_reconciliations',
    )
    op.drop_index(
        op.f('ix_dedup_execution_action_reconciliations_audit_id'),
        table_name='dedup_execution_action_reconciliations',
    )
    op.drop_table('dedup_execution_action_reconciliations')
    op.drop_column('dedup_executions', 'recovery_claimed_at')
    # No DROP TYPE here: 'dedup_execution_action_result' is created
    # with create_type=False above (reused from the existing
    # dedup_execution_action_audits table) and remains owned by that
    # table's own migration - this downgrade must not touch it.
