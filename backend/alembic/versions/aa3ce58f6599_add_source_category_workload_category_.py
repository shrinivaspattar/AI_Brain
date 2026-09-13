"""add source_category workload_category risk_tier_estimated to source_instances

Revision ID: aa3ce58f6599
Revises: 68c99bcc283e
Create Date: 2026-09-13 22:27:23.955604

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'aa3ce58f6599'
down_revision: Union[str, Sequence[str], None] = '68c99bcc283e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Milestone 2 correction: source_category/workload_category/
    risk_tier_estimated must be dedicated, queryable SourceInstance
    columns per the frozen implementation design - not JSONB-only
    (evidence_snapshot) state. Purely additive: three new nullable
    columns. No existing column altered or dropped, no data migration,
    no real-T7 dependency. risk_tier_actual is deliberately not added
    yet - nothing computes it until a future archive-extraction
    milestone exists."""

    # Postgres requires the enum TYPE to exist before ADD COLUMN can
    # reference it (unlike CREATE TABLE, which auto-creates inline
    # enum types) - create each type explicitly first, then reference
    # it with create_type=False so add_column does not attempt to
    # create it a second time.
    postgresql.ENUM(
        'LOOSE_FILE', 'ARCHIVE', 'SPECIAL', name='source_category'
    ).create(op.get_bind(), checkfirst=True)
    postgresql.ENUM(
        'TEXT_DOCUMENT', 'STRUCTURED_DATA', 'MEDIA', 'CODE', 'SOFTWARE',
        'ENCRYPTED', 'CONTAINER', 'UNKNOWN',
        name='workload_category',
    ).create(op.get_bind(), checkfirst=True)
    postgresql.ENUM(
        'LOW', 'MEDIUM', 'HIGH', 'EXTREME', name='risk_tier_estimated'
    ).create(op.get_bind(), checkfirst=True)

    op.add_column(
        'source_instances',
        sa.Column(
            'source_category',
            postgresql.ENUM('LOOSE_FILE', 'ARCHIVE', 'SPECIAL', name='source_category', create_type=False),
            nullable=True,
        ),
    )
    op.add_column(
        'source_instances',
        sa.Column(
            'workload_category',
            postgresql.ENUM(
                'TEXT_DOCUMENT', 'STRUCTURED_DATA', 'MEDIA', 'CODE', 'SOFTWARE',
                'ENCRYPTED', 'CONTAINER', 'UNKNOWN',
                name='workload_category', create_type=False,
            ),
            nullable=True,
        ),
    )
    op.add_column(
        'source_instances',
        sa.Column(
            'risk_tier_estimated',
            postgresql.ENUM('LOW', 'MEDIUM', 'HIGH', 'EXTREME', name='risk_tier_estimated', create_type=False),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Reverses every step of upgrade(), in reverse order, including
    the enum types (each exclusively owned by these columns)."""

    op.drop_column('source_instances', 'risk_tier_estimated')
    op.drop_column('source_instances', 'workload_category')
    op.drop_column('source_instances', 'source_category')

    postgresql.ENUM(name='risk_tier_estimated').drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name='workload_category').drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name='source_category').drop(op.get_bind(), checkfirst=True)
