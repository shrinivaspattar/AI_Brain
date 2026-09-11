"""add import_job_id to documents

Revision ID: fc108174d9a6
Revises: 9d33baea1ea3
Create Date: 2026-09-11 19:44:01.524013

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fc108174d9a6'
down_revision: Union[str, Sequence[str], None] = '9d33baea1ea3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('documents', sa.Column('import_job_id', sa.Integer(), nullable=True))
    op.create_index(op.f('ix_documents_import_job_id'), 'documents', ['import_job_id'], unique=False)
    op.create_foreign_key(
        'fk_documents_import_job_id_import_jobs',
        'documents',
        'import_jobs',
        ['import_job_id'],
        ['id'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        'fk_documents_import_job_id_import_jobs',
        'documents',
        type_='foreignkey',
    )
    op.drop_index(op.f('ix_documents_import_job_id'), table_name='documents')
    op.drop_column('documents', 'import_job_id')
