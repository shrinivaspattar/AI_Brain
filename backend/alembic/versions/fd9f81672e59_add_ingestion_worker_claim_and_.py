"""add ingestion worker claim fields and ingestion_attempts table

Revision ID: fd9f81672e59
Revises: b9a82d073399
Create Date: 2026-09-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'fd9f81672e59'
down_revision: Union[str, Sequence[str], None] = 'b9a82d073399'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema - purely additive: two new nullable columns each
    on content_identity_groups and source_instances, one new table.
    No existing column is altered or dropped."""

    op.add_column(
        'content_identity_groups',
        sa.Column('claimed_by', sa.Text(), nullable=True),
    )
    op.add_column(
        'content_identity_groups',
        sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        'source_instances',
        sa.Column('claimed_by', sa.Text(), nullable=True),
    )
    op.add_column(
        'source_instances',
        sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        'ingestion_attempts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column(
            'attempt_kind',
            sa.Enum('PIPELINE_ADVANCE', 'IDENTITY_RESOLUTION', name='ingestion_attempt_kind'),
            nullable=False,
        ),
        sa.Column('content_identity_group_id', sa.Integer(), nullable=True),
        sa.Column('source_instance_id', sa.Integer(), nullable=True),
        sa.Column(
            'attempted_stage',
            sa.Enum(
                'IDENTITY_RESOLUTION', 'EXTRACTING', 'NORMALIZING', 'CHUNKING', 'EMBEDDING',
                name='ingestion_attempt_stage',
            ),
            nullable=False,
        ),
        sa.Column(
            'outcome',
            sa.Enum('SUCCEEDED', 'FAILED', name='ingestion_attempt_outcome'),
            nullable=False,
        ),
        sa.Column(
            'failure_code',
            sa.Enum(
                'CORRUPT_INPUT', 'MALFORMED_ARCHIVE', 'OVERSIZED_OR_EXPANSION_LIMIT',
                'EXTRACTION_ERROR_OTHER', 'NORMALIZATION_ERROR', 'CHUNKING_ERROR',
                'EMBEDDING_UNAVAILABLE', 'T7_UNAVAILABLE', 'INSUFFICIENT_DISK_SPACE',
                'PERMISSION_DENIED', 'READ_ERROR_OTHER',
                name='ingestion_failure_code',
            ),
            nullable=True,
        ),
        sa.Column('failure_detail', sa.Text(), nullable=True),
        sa.Column('retryable', sa.Boolean(), nullable=True),
        sa.Column('worker_id', sa.Text(), nullable=False),
        sa.Column('attempted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['content_identity_group_id'], ['content_identity_groups.id'], ),
        sa.ForeignKeyConstraint(['source_instance_id'], ['source_instances.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(
            "(attempt_kind = 'PIPELINE_ADVANCE' "
            " AND content_identity_group_id IS NOT NULL "
            " AND source_instance_id IS NULL "
            " AND attempted_stage != 'IDENTITY_RESOLUTION') "
            "OR "
            "(attempt_kind = 'IDENTITY_RESOLUTION' "
            " AND source_instance_id IS NOT NULL "
            " AND content_identity_group_id IS NULL "
            " AND attempted_stage = 'IDENTITY_RESOLUTION')",
            name='ck_ingestion_attempts_parent_matches_kind',
        ),
        sa.CheckConstraint(
            "outcome = 'SUCCEEDED' OR ("
            "failure_code IS NOT NULL "
            "AND failure_detail IS NOT NULL "
            "AND retryable IS NOT NULL"
            ")",
            name='ck_ingestion_attempts_failure_requires_detail',
        ),
    )
    op.create_index(op.f('ix_ingestion_attempts_id'), 'ingestion_attempts', ['id'], unique=False)
    op.create_index(
        op.f('ix_ingestion_attempts_content_identity_group_id'),
        'ingestion_attempts', ['content_identity_group_id'], unique=False,
    )
    op.create_index(
        op.f('ix_ingestion_attempts_source_instance_id'),
        'ingestion_attempts', ['source_instance_id'], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema - reverses every step of upgrade(), in reverse
    order, including the enum types created for ingestion_attempts
    (each is exclusively owned by this table)."""

    op.drop_index(op.f('ix_ingestion_attempts_source_instance_id'), table_name='ingestion_attempts')
    op.drop_index(op.f('ix_ingestion_attempts_content_identity_group_id'), table_name='ingestion_attempts')
    op.drop_index(op.f('ix_ingestion_attempts_id'), table_name='ingestion_attempts')
    op.drop_table('ingestion_attempts')

    op.drop_column('source_instances', 'claimed_at')
    op.drop_column('source_instances', 'claimed_by')

    op.drop_column('content_identity_groups', 'claimed_at')
    op.drop_column('content_identity_groups', 'claimed_by')

    postgresql.ENUM(name='ingestion_failure_code').drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name='ingestion_attempt_outcome').drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name='ingestion_attempt_stage').drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name='ingestion_attempt_kind').drop(op.get_bind(), checkfirst=True)
