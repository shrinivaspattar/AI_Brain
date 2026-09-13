"""add source_instance provenance_link content_identity_group discovery_run classification_run tables

Revision ID: b9a82d073399
Revises: ef65da409302
Create Date: 2026-09-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b9a82d073399'
down_revision: Union[str, Sequence[str], None] = 'ef65da409302'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema - purely additive: five new tables, one new
    nullable column on documents. No existing column is altered or
    dropped."""

    op.create_table(
        'discovery_runs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column(
            'run_kind',
            sa.Enum(
                'D0_INVENTORY',
                'D1_DUPLICATE_ANALYSIS',
                'D2_PROVENANCE_ANALYSIS',
                name='discovery_run_kind',
            ),
            nullable=False,
        ),
        sa.Column('source_root', sa.Text(), nullable=False),
        sa.Column('report_sha256', sa.String(length=64), nullable=False),
        sa.Column('run_started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('run_completed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_discovery_runs_id'), 'discovery_runs', ['id'], unique=False)

    op.create_table(
        'classification_runs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('classifier_version', sa.String(length=100), nullable=False),
        sa.Column('d0_discovery_run_id', sa.Integer(), nullable=True),
        sa.Column('d1_discovery_run_id', sa.Integer(), nullable=True),
        sa.Column('d2_discovery_run_id', sa.Integer(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['d0_discovery_run_id'], ['discovery_runs.id'], ),
        sa.ForeignKeyConstraint(['d1_discovery_run_id'], ['discovery_runs.id'], ),
        sa.ForeignKeyConstraint(['d2_discovery_run_id'], ['discovery_runs.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(
            "d0_discovery_run_id IS NOT NULL "
            "OR d1_discovery_run_id IS NOT NULL "
            "OR d2_discovery_run_id IS NOT NULL",
            name='ck_classification_runs_at_least_one_discovery_run',
        ),
    )
    op.create_index(op.f('ix_classification_runs_id'), 'classification_runs', ['id'], unique=False)
    op.create_index(op.f('ix_classification_runs_d0_discovery_run_id'), 'classification_runs', ['d0_discovery_run_id'], unique=False)
    op.create_index(op.f('ix_classification_runs_d1_discovery_run_id'), 'classification_runs', ['d1_discovery_run_id'], unique=False)
    op.create_index(op.f('ix_classification_runs_d2_discovery_run_id'), 'classification_runs', ['d2_discovery_run_id'], unique=False)

    op.create_table(
        'content_identity_groups',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column(
            'identity_kind',
            sa.Enum(
                'SOURCE_BYTES',
                'EXTRACTED_CONTENT',
                'NORMALIZED_CONTENT',
                name='content_identity_kind',
            ),
            nullable=False,
        ),
        sa.Column(
            'identity_algorithm',
            sa.Enum('SHA256', name='content_identity_algorithm'),
            nullable=False,
        ),
        sa.Column('identity_hash', sa.String(length=64), nullable=False),
        sa.Column(
            'pipeline_state',
            sa.Enum(
                'DISCOVERED',
                'CLASSIFIED',
                'EXTRACTING',
                'EXTRACTED',
                'NORMALIZED',
                'CHUNKED',
                'EMBEDDED',
                'INGESTED',
                'NEEDS_REVIEW',
                'UNSUPPORTED',
                'EXCLUDED',
                'FAILED',
                name='content_pipeline_state',
            ),
            server_default='DISCOVERED',
            nullable=False,
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'identity_kind', 'identity_algorithm', 'identity_hash',
            name='uq_content_identity_groups_identity',
        ),
    )
    op.create_index(op.f('ix_content_identity_groups_id'), 'content_identity_groups', ['id'], unique=False)

    op.create_table(
        'source_instances',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('classification_run_id', sa.Integer(), nullable=False),
        sa.Column('content_identity_group_id', sa.Integer(), nullable=True),
        sa.Column('root_t7_path', sa.Text(), nullable=False),
        sa.Column('member_path', sa.Text(), nullable=True),
        sa.Column('evidence_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            'canonical_status',
            sa.Enum('UNRESOLVED', 'CANONICAL', 'NON_CANONICAL', name='canonical_status'),
            server_default='UNRESOLVED',
            nullable=False,
        ),
        sa.Column('canonical_status_reason', sa.Text(), nullable=True),
        sa.Column('canonical_status_decided_by', sa.Text(), nullable=True),
        sa.Column('canonical_status_decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['classification_run_id'], ['classification_runs.id'], ),
        sa.ForeignKeyConstraint(['content_identity_group_id'], ['content_identity_groups.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(
            "canonical_status = 'UNRESOLVED' OR ("
            "canonical_status_reason IS NOT NULL "
            "AND canonical_status_decided_by IS NOT NULL "
            "AND canonical_status_decided_at IS NOT NULL"
            ")",
            name='ck_source_instances_canonical_status_requires_evidence',
        ),
    )
    op.create_index(op.f('ix_source_instances_id'), 'source_instances', ['id'], unique=False)
    op.create_index(op.f('ix_source_instances_classification_run_id'), 'source_instances', ['classification_run_id'], unique=False)
    op.create_index(op.f('ix_source_instances_content_identity_group_id'), 'source_instances', ['content_identity_group_id'], unique=False)

    op.create_table(
        'provenance_links',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('source_instance_id', sa.Integer(), nullable=False),
        sa.Column('parent_link_id', sa.Integer(), nullable=True),
        sa.Column('sequence_index', sa.Integer(), nullable=False),
        sa.Column(
            'kind',
            sa.Enum('T7_FILE', 'ARCHIVE_MEMBER', name='provenance_link_kind'),
            nullable=False,
        ),
        sa.Column('path', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['source_instance_id'], ['source_instances.id'], ),
        sa.ForeignKeyConstraint(['parent_link_id'], ['provenance_links.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'source_instance_id', 'sequence_index',
            name='uq_provenance_links_source_instance_id_sequence_index',
        ),
        sa.CheckConstraint(
            "(sequence_index = 0 AND kind = 'T7_FILE' AND parent_link_id IS NULL) "
            "OR (sequence_index > 0 AND kind = 'ARCHIVE_MEMBER' AND parent_link_id IS NOT NULL)",
            name='ck_provenance_links_root_shape',
        ),
    )
    op.create_index(op.f('ix_provenance_links_id'), 'provenance_links', ['id'], unique=False)
    op.create_index(op.f('ix_provenance_links_source_instance_id'), 'provenance_links', ['source_instance_id'], unique=False)

    op.add_column(
        'documents',
        sa.Column('content_identity_group_id', sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        'fk_documents_content_identity_group_id',
        'documents', 'content_identity_groups',
        ['content_identity_group_id'], ['id'],
    )
    op.create_unique_constraint(
        'uq_documents_content_identity_group_id',
        'documents', ['content_identity_group_id'],
    )


def downgrade() -> None:
    """Downgrade schema - reverses every step of upgrade(), in reverse
    order, including the enum types created for these tables (each is
    exclusively owned by a table dropped here, unlike e.g.
    dedup_plan_action_type which is shared and must survive elsewhere)."""

    op.drop_constraint('uq_documents_content_identity_group_id', 'documents', type_='unique')
    op.drop_constraint('fk_documents_content_identity_group_id', 'documents', type_='foreignkey')
    op.drop_column('documents', 'content_identity_group_id')

    op.drop_index(op.f('ix_provenance_links_source_instance_id'), table_name='provenance_links')
    op.drop_index(op.f('ix_provenance_links_id'), table_name='provenance_links')
    op.drop_table('provenance_links')

    op.drop_index(op.f('ix_source_instances_content_identity_group_id'), table_name='source_instances')
    op.drop_index(op.f('ix_source_instances_classification_run_id'), table_name='source_instances')
    op.drop_index(op.f('ix_source_instances_id'), table_name='source_instances')
    op.drop_table('source_instances')

    op.drop_index(op.f('ix_content_identity_groups_id'), table_name='content_identity_groups')
    op.drop_table('content_identity_groups')

    op.drop_index(op.f('ix_classification_runs_d2_discovery_run_id'), table_name='classification_runs')
    op.drop_index(op.f('ix_classification_runs_d1_discovery_run_id'), table_name='classification_runs')
    op.drop_index(op.f('ix_classification_runs_d0_discovery_run_id'), table_name='classification_runs')
    op.drop_index(op.f('ix_classification_runs_id'), table_name='classification_runs')
    op.drop_table('classification_runs')

    op.drop_index(op.f('ix_discovery_runs_id'), table_name='discovery_runs')
    op.drop_table('discovery_runs')

    postgresql.ENUM(name='provenance_link_kind').drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name='canonical_status').drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name='content_pipeline_state').drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name='content_identity_algorithm').drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name='content_identity_kind').drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name='discovery_run_kind').drop(op.get_bind(), checkfirst=True)
