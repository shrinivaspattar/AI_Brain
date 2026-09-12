"""add status to memories

Revision ID: bb3be4e51d72
Revises: db6a38db9323
Create Date: 2026-09-12 06:16:39.312734

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bb3be4e51d72'
down_revision: Union[str, Sequence[str], None] = 'db6a38db9323'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


memory_status = sa.Enum('PENDING', 'APPROVED', 'REJECTED', name='memory_status')


def upgrade() -> None:
    """Upgrade schema."""
    # ADD COLUMN (unlike CREATE TABLE) doesn't implicitly create the enum
    # type, so it has to be created explicitly first.
    memory_status.create(op.get_bind(), checkfirst=True)
    op.add_column(
        'memories',
        sa.Column('status', memory_status, server_default='APPROVED', nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('memories', 'status')
    memory_status.drop(op.get_bind(), checkfirst=True)
