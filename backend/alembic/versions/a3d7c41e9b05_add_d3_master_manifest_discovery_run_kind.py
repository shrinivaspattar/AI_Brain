"""add D3_MASTER_MANIFEST to the discovery_run_kind enum

Revision ID: a3d7c41e9b05
Revises: ce9ca5f6bfd6
Create Date: 2026-09-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a3d7c41e9b05'
down_revision: Union[str, Sequence[str], None] = 'ce9ca5f6bfd6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Purely additive: one new value on an existing enum, no table or
    row is touched. D3_MASTER_MANIFEST identifies a checksum list of the
    cleaned master copy (decision 0003), as opposed to the D0/D1/D2
    reports made on the old, duplicated drive.

    ALTER TYPE ... ADD VALUE is run in an autocommit block so it never
    sits inside a transaction that also tries to use the new value."""
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE discovery_run_kind ADD VALUE IF NOT EXISTS 'D3_MASTER_MANIFEST'")


def downgrade() -> None:
    """PostgreSQL cannot drop a single value from an enum without
    recreating the type and rewriting every column that uses it, so this
    is deliberately a no-op: the extra value is harmless if unused. Rows
    that already use it would block a manual removal."""
    pass
