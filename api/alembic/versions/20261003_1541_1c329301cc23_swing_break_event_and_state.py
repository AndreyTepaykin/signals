"""swing break event and state

Revision ID: 1c329301cc23
Revises: 7cd7d7f2e800
Create Date: 2026-10-03 15:41:12.104470+00:00

The swing break is split into two facts. ``feature_row.swing_break`` becomes
``swing_break_state`` (the 4h close is still beyond the broken swing and the
chandelier stop is not breached: the direction permission for the 15m
triggers), and ``swing_break_event`` is added (this 15m bar closes the first
4h close beyond the swing). ``swing_break_direction`` keeps its name; only its
comment changes. Written by hand: autogenerate would render the rename as a
drop and an add. ``feature_row`` held no rows when this was written.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1c329301cc23'
down_revision: Union[str, None] = '7cd7d7f2e800'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STATE_COMMENT = '4h close still beyond the broken 2-bar fractal swing and the chandelier stop not breached.'
EVENT_COMMENT = 'This 15m bar closes the first 4h close beyond the swing (the previous 4h close was not).'
DIRECTION_COMMENT = 'long | short: side of the 4h close beyond the swing.'

OLD_STATE_COMMENT = '4h close beyond the last 2-bar fractal.'
OLD_DIRECTION_COMMENT = 'long | short'


def upgrade() -> None:
    op.alter_column(
        'feature_row',
        'swing_break',
        new_column_name='swing_break_state',
        existing_type=sa.Boolean(),
        existing_nullable=True,
        comment=STATE_COMMENT,
        existing_comment=OLD_STATE_COMMENT,
    )
    op.add_column('feature_row', sa.Column('swing_break_event', sa.Boolean(), nullable=True, comment=EVENT_COMMENT))
    op.alter_column(
        'feature_row',
        'swing_break_direction',
        existing_type=sa.Text(),
        existing_nullable=True,
        comment=DIRECTION_COMMENT,
        existing_comment=OLD_DIRECTION_COMMENT,
    )


def downgrade() -> None:
    op.alter_column(
        'feature_row',
        'swing_break_direction',
        existing_type=sa.Text(),
        existing_nullable=True,
        comment=OLD_DIRECTION_COMMENT,
        existing_comment=DIRECTION_COMMENT,
    )
    op.drop_column('feature_row', 'swing_break_event')
    op.alter_column(
        'feature_row',
        'swing_break_state',
        new_column_name='swing_break',
        existing_type=sa.Boolean(),
        existing_nullable=True,
        comment=OLD_STATE_COMMENT,
        existing_comment=STATE_COMMENT,
    )
