"""add chat_attachments table

Revision ID: f1e2d3c4b5a6
Revises: a3d7c41e9b05
Create Date: 2026-09-22 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f1e2d3c4b5a6'
down_revision: Union[str, Sequence[str], None] = 'a3d7c41e9b05'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Purely additive: one new table for files uploaded directly into a
    chat turn (see docs/designs and app/models/chat_attachment.py). Not
    linked to any existing table - it is a completely separate, ephemeral
    concept from the Chain 1/2 ingestion pipeline."""
    op.create_table(
        'chat_attachments',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('original_filename', sa.String(length=255), nullable=False),
        sa.Column('stored_path', sa.Text(), nullable=False),
        sa.Column('byte_size', sa.Integer(), nullable=False),
        sa.Column('extracted_text', sa.Text(), nullable=True),
        sa.Column('truncated', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('chat_attachments')
