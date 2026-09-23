"""add messages.context_sources

Revision ID: b2c9f4a1d6e8
Revises: f1e2d3c4b5a6
Create Date: 2026-09-23 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b2c9f4a1d6e8'
down_revision: Union[str, Sequence[str], None] = 'f1e2d3c4b5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Purely additive: one nullable column on the existing messages table,
    for the UI's context/privacy indicator (see app/models/message.py).
    No backfill - existing rows get NULL, meaning "unknown", not "none"."""
    op.add_column(
        'messages',
        sa.Column('context_sources', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('messages', 'context_sources')
