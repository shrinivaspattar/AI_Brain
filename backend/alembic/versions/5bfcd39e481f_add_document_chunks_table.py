"""add document_chunks table

Revision ID: 5bfcd39e481f
Revises: fc108174d9a6
Create Date: 2026-09-11 19:48:31.292007

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import pgvector.sqlalchemy


# revision identifiers, used by Alembic.
revision: str = '5bfcd39e481f'
down_revision: Union[str, Sequence[str], None] = 'fc108174d9a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')

    op.create_table(
        'document_chunks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('document_id', sa.String(length=36), nullable=False),
        sa.Column('chunk_index', sa.Integer(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('embedding', pgvector.sqlalchemy.Vector(768), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['document_id'],
            ['documents.id'],
            name='fk_document_chunks_document_id_documents',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'document_id',
            'chunk_index',
            name='uq_document_chunks_document_id_chunk_index',
        ),
    )
    op.create_index(
        op.f('ix_document_chunks_document_id'),
        'document_chunks',
        ['document_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_document_chunks_id'),
        'document_chunks',
        ['id'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_document_chunks_id'), table_name='document_chunks')
    op.drop_index(op.f('ix_document_chunks_document_id'), table_name='document_chunks')
    op.drop_table('document_chunks')
    # Extension intentionally left in place: other tables/migrations may
    # depend on it, and CREATE EXTENSION IF NOT EXISTS is idempotent.
