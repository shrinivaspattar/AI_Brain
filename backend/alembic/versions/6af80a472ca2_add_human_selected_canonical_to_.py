"""add human selected canonical to duplicate reviews

Revision ID: 6af80a472ca2
Revises: a7ad86ac80d3
Create Date: 2026-09-12 12:16:02.802269

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6af80a472ca2'
down_revision: Union[str, Sequence[str], None] = 'a7ad86ac80d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Terminology refinement from design review: the system's suggestion
    # is a "recommendation," never a decision - renaming the enum label
    # makes that explicit in the schema itself, not just in code comments.
    op.execute(
        "ALTER TYPE duplicate_review_member_role "
        "RENAME VALUE 'PROPOSED_CANONICAL' TO 'RECOMMENDED_CANONICAL'"
    )

    op.add_column(
        'duplicate_reviews',
        sa.Column('human_selected_canonical_document_id', sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        'fk_duplicate_reviews_human_selected_canonical_document_id',
        'duplicate_reviews',
        'documents',
        ['human_selected_canonical_document_id'],
        ['id'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        'fk_duplicate_reviews_human_selected_canonical_document_id',
        'duplicate_reviews',
        type_='foreignkey',
    )
    op.drop_column('duplicate_reviews', 'human_selected_canonical_document_id')

    op.execute(
        "ALTER TYPE duplicate_review_member_role "
        "RENAME VALUE 'RECOMMENDED_CANONICAL' TO 'PROPOSED_CANONICAL'"
    )
