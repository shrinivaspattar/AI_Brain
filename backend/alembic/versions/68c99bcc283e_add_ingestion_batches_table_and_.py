"""add ingestion_batches table and reservation fencing primitives

Revision ID: 68c99bcc283e
Revises: fd9f81672e59
Create Date: 2026-09-13 21:22:19.209992

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '68c99bcc283e'
down_revision: Union[str, Sequence[str], None] = 'fd9f81672e59'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Schema/model foundation for Scaled Real-T7 Ingestion (Implementation
    Milestone 1 - schema only, no BatchCreationService/PolicyEvaluator/
    resource guard/reservation lifecycle/batch-aware worker claiming
    exists yet). Purely additive: one new table, two new nullable/
    defaulted columns on content_identity_groups, one new unique index
    on source_instances. No existing column is altered or dropped, no
    data migration, no real-T7 dependency."""

    op.create_table(
        'ingestion_batches',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('classification_run_id', sa.Integer(), nullable=False),
        sa.Column(
            'status',
            sa.Enum('PLANNED', 'RUNNING', 'PAUSED', 'COMPLETED', 'ABORTED', name='batch_status'),
            nullable=False,
            server_default='PLANNED',
        ),
        sa.Column(
            'stop_reason',
            sa.Enum(
                'SOURCE_WORK_EXHAUSTED', 'EXTRACTED_BYTES_ENVELOPE_EXHAUSTED',
                'EMBEDDINGS_ENVELOPE_EXHAUSTED', 'RUNTIME_BUDGET_EXCEEDED',
                'WORKSPACE_SOFT_STOP', 'POSTGRES_SOFT_STOP', 'MANUAL_PAUSE',
                'WORKSPACE_HARD_STOP', 'POSTGRES_HARD_STOP',
                'OLLAMA_PERSISTENTLY_UNREACHABLE', 'SAFETY_INVARIANT_VIOLATION_DETECTED',
                name='batch_stop_reason',
            ),
            nullable=True,
        ),
        sa.Column('stop_reason_detail', sa.Text(), nullable=True),
        sa.Column('review_required', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('max_source_instances', sa.Integer(), nullable=False),
        sa.Column('max_source_bytes', sa.BigInteger(), nullable=False),
        sa.Column('max_extracted_bytes', sa.BigInteger(), nullable=True),
        sa.Column('max_embeddings', sa.Integer(), nullable=False),
        sa.Column('max_runtime_seconds', sa.Integer(), nullable=False),
        sa.Column('eligible_source_count', sa.Integer(), nullable=False),
        sa.Column('policy_filtered_count', sa.Integer(), nullable=False),
        sa.Column('selectable_count', sa.Integer(), nullable=False),
        sa.Column('source_instances_selected', sa.Integer(), nullable=False),
        sa.Column('source_bytes_selected', sa.BigInteger(), nullable=False),
        sa.Column('selection_fingerprint', sa.String(length=64), nullable=False),
        sa.Column('selection_policy_version', sa.String(length=200), nullable=False),
        sa.Column('ordering_version', sa.String(length=200), nullable=False),
        sa.Column('extracted_bytes_consumed', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('embeddings_reserved', sa.Integer(), nullable=False, server_default='0'),
        sa.Column(
            'monotonic_runtime_seconds_consumed', sa.Float(), nullable=False, server_default='0'
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['classification_run_id'], ['classification_runs.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('classification_run_id', name='uq_ingestion_batches_classification_run_id'),
        sa.CheckConstraint(
            "(status IN ('PLANNED', 'RUNNING') AND stop_reason IS NULL) "
            "OR (status IN ('PAUSED', 'COMPLETED', 'ABORTED') AND stop_reason IS NOT NULL)",
            name='ck_ingestion_batches_stop_reason_matches_status',
        ),
        sa.CheckConstraint('max_source_instances > 0', name='ck_ingestion_batches_max_source_instances_positive'),
        sa.CheckConstraint('max_source_bytes > 0', name='ck_ingestion_batches_max_source_bytes_positive'),
        sa.CheckConstraint(
            'max_extracted_bytes IS NULL OR max_extracted_bytes >= 0',
            name='ck_ingestion_batches_max_extracted_bytes_non_negative',
        ),
        sa.CheckConstraint('max_embeddings > 0', name='ck_ingestion_batches_max_embeddings_positive'),
        sa.CheckConstraint('max_runtime_seconds > 0', name='ck_ingestion_batches_max_runtime_seconds_positive'),
        sa.CheckConstraint(
            'eligible_source_count >= 0', name='ck_ingestion_batches_eligible_source_count_non_negative'
        ),
        sa.CheckConstraint(
            'policy_filtered_count >= 0', name='ck_ingestion_batches_policy_filtered_count_non_negative'
        ),
        sa.CheckConstraint('selectable_count >= 0', name='ck_ingestion_batches_selectable_count_non_negative'),
        sa.CheckConstraint(
            'source_instances_selected >= 0',
            name='ck_ingestion_batches_source_instances_selected_non_negative',
        ),
        sa.CheckConstraint(
            'source_bytes_selected >= 0', name='ck_ingestion_batches_source_bytes_selected_non_negative'
        ),
        sa.CheckConstraint(
            'extracted_bytes_consumed >= 0', name='ck_ingestion_batches_extracted_bytes_consumed_non_negative'
        ),
        sa.CheckConstraint(
            'embeddings_reserved >= 0', name='ck_ingestion_batches_embeddings_reserved_non_negative'
        ),
        sa.CheckConstraint(
            'monotonic_runtime_seconds_consumed >= 0',
            name='ck_ingestion_batches_monotonic_runtime_non_negative',
        ),
        sa.CheckConstraint(
            'embeddings_reserved <= max_embeddings',
            name='ck_ingestion_batches_embeddings_reserved_within_envelope',
        ),
        sa.CheckConstraint(
            'max_extracted_bytes IS NULL OR extracted_bytes_consumed <= max_extracted_bytes',
            name='ck_ingestion_batches_extracted_bytes_within_envelope',
        ),
    )
    op.create_index(op.f('ix_ingestion_batches_id'), 'ingestion_batches', ['id'], unique=False)

    op.add_column(
        'content_identity_groups',
        sa.Column('claim_generation', sa.Integer(), nullable=False, server_default='0'),
    )
    op.add_column(
        'content_identity_groups',
        sa.Column('reserved_embeddings', sa.Integer(), nullable=True),
    )

    op.execute(
        "CREATE UNIQUE INDEX uq_source_instances_run_path_member "
        "ON source_instances (classification_run_id, root_t7_path, COALESCE(member_path, ''))"
    )


def downgrade() -> None:
    """Reverses every step of upgrade(), in reverse order, including the
    enum types created for ingestion_batches (each exclusively owned by
    this table)."""

    op.execute("DROP INDEX IF EXISTS uq_source_instances_run_path_member")

    op.drop_column('content_identity_groups', 'reserved_embeddings')
    op.drop_column('content_identity_groups', 'claim_generation')

    op.drop_index(op.f('ix_ingestion_batches_id'), table_name='ingestion_batches')
    op.drop_table('ingestion_batches')

    postgresql.ENUM(name='batch_stop_reason').drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name='batch_status').drop(op.get_bind(), checkfirst=True)
