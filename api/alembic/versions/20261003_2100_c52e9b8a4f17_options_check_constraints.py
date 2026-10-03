"""options check: candidate guard and the compact snapshot payload comment

Revision ID: c52e9b8a4f17
Revises: a41f7c05d2e9
Create Date: 2026-10-03 21:00:00.000000+00:00

Milestone 5 writes the options columns of ``candidate`` exactly once. One rule about
them is cheap to enforce in the database:

* ``pending_has_no_verdict`` - a row that still carries the M4 marker
  ``status_reason = 'pending_options_check'`` has not been through the check, so it
  must not have a verdict. (The reverse is the M1 ``alert_has_options_verdict`` CHECK.)

(That every verdict other than ``ignore`` names its ``options_metric`` row is NOT a CHECK:
the shared fixtures build verdicts without a metric row, and the options job is the only
writer. It is asserted in ``tests/test_options_job.py``.)

``options_snapshot.payload`` was documented as the raw venue response; M5 stores a
compact form (see ``app.worker.feeds.deribit``), and the column comment now says so.
Written by hand: autogenerate does not compare CHECK constraints. No ``candidate`` row
has a verdict before this milestone, and the upgrade fails loudly if one violates the rule.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c52e9b8a4f17'
down_revision: Union[str, None] = 'a41f7c05d2e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_COMMENT = (
    'Compact venue book: currency, index_price and, per instrument with open interest above zero, '
    'the original Deribit keys. Enough to recompute every metric; not the full response.'
)
OLD_COMMENT = 'Raw venue response, unmodified, so a metric can be recomputed later.'


def upgrade() -> None:
    op.create_check_constraint(
        op.f('ck_candidate_pending_has_no_verdict'),
        'candidate',
        "status_reason IS DISTINCT FROM 'pending_options_check' OR options_verdict IS NULL",
    )
    op.alter_column(
        'options_snapshot',
        'payload',
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        existing_nullable=False,
        comment=NEW_COMMENT,
        existing_comment=OLD_COMMENT,
    )


def downgrade() -> None:
    op.alter_column(
        'options_snapshot',
        'payload',
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        existing_nullable=False,
        comment=OLD_COMMENT,
        existing_comment=NEW_COMMENT,
    )
    op.drop_constraint(op.f('ck_candidate_pending_has_no_verdict'), 'candidate', type_='check')
