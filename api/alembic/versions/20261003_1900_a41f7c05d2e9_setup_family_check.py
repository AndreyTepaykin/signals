"""freeze the setup_family vocabulary

Revision ID: a41f7c05d2e9
Revises: 1c329301cc23
Create Date: 2026-10-03 19:00:00.000000+00:00

``candidate.setup_family`` was free text because the spec names the setup
family as a pool dimension without enumerating it. Milestone 4 freezes the list
in ``app.core.constants.SETUP_FAMILIES`` (so it enters ``rule_hash``), and this
revision adds the matching CHECK. Written by hand: autogenerate does not compare
CHECK constraints. The ``candidate`` table held no rows when this was written;
if it ever does, the upgrade fails loudly on any value outside the vocabulary
instead of rewriting history.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a41f7c05d2e9'
down_revision: Union[str, None] = '1c329301cc23'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_COMMENT = 'Pool key dimension. Closed vocabulary frozen by M4: breakout | pullback | squeeze | reversion.'
OLD_COMMENT = 'Pool key dimension. Open vocabulary: M4 owns and must freeze it.'


def upgrade() -> None:
    op.create_check_constraint(
        op.f('ck_candidate_setup_family'),
        'candidate',
        "setup_family IN ('breakout', 'pullback', 'squeeze', 'reversion')",
    )
    op.alter_column(
        'candidate',
        'setup_family',
        existing_type=sa.Text(),
        existing_nullable=False,
        comment=NEW_COMMENT,
        existing_comment=OLD_COMMENT,
    )


def downgrade() -> None:
    op.alter_column(
        'candidate',
        'setup_family',
        existing_type=sa.Text(),
        existing_nullable=False,
        comment=OLD_COMMENT,
        existing_comment=NEW_COMMENT,
    )
    op.drop_constraint(op.f('ck_candidate_setup_family'), 'candidate', type_='check')
