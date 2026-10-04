"""barrier labeler: label hashes, no_entry labels, funding_partial, write-once fill values

Revision ID: d8b2f6a31e05
Revises: c52e9b8a4f17
Create Date: 2026-10-04 16:30:00.000000+00:00

Milestone 6 writes ``candidate.entry_price``, ``one_r``, both barrier prices and ``vertical_deadline``
exactly once, and one ``label`` per candidate.

* ``label.feature_hash`` and ``label.rule_hash`` are new and NOT NULL (the label carries all three hashes);
  ``label.code_version`` becomes NOT NULL. Existing rows, if any, take the hashes of their candidate and
  ``unknown`` for a missing code version.
* ``label.first_barrier`` gains ``no_entry`` (the next open was at or through the stop or the cap: no trade).
  Every R column and ``hours_to_resolution`` become nullable, with CHECKs that a ``no_entry`` label has none of
  them and every other label has all of them.
* ``label.funding_partial`` (NOT NULL, default false): written after the funding stall cap with the prints seen so far.
* ``ck_candidate_fill_values_written_together``: an entry price implies the deadline and either all of 1R and both
  barriers or none of them (a ``no_entry`` fill).
* Trigger ``candidate_freeze_fill_values``: once the entry price is set, none of the five fill values may change; a
  non-NULL deadline may never change.

Written by hand: autogenerate does not see CHECK constraints or triggers.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd8b2f6a31e05'
down_revision: Union[str, None] = 'c52e9b8a4f17'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_FIRST_BARRIER_COMMENT = 'upper | lower | vertical - whichever traded first.'
NEW_FIRST_BARRIER_COMMENT = (
    'upper | lower | vertical - whichever traded first - or no_entry: the next open was at or through the stop '
    '(or through the cap), so there is no trade and every R column is NULL.'
)
R_COLUMNS = ('price_r', 'fees_r', 'funding_r', 'stored_r', 'mae_r')

FREEZE_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION candidate_freeze_fill_values() RETURNS trigger AS $$
BEGIN
    IF (OLD.vertical_deadline IS NOT NULL AND NEW.vertical_deadline IS DISTINCT FROM OLD.vertical_deadline)
        OR (OLD.entry_price IS NOT NULL AND (
            NEW.entry_price IS DISTINCT FROM OLD.entry_price
            OR NEW.one_r IS DISTINCT FROM OLD.one_r
            OR NEW.upper_barrier_price IS DISTINCT FROM OLD.upper_barrier_price
            OR NEW.lower_barrier_price IS DISTINCT FROM OLD.lower_barrier_price))
    THEN
        RAISE EXCEPTION USING MESSAGE = 'candidate fill values are write-once, id ' || OLD.id, ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""
FREEZE_TRIGGER_SQL = (
    'CREATE TRIGGER candidate_freeze_fill_values BEFORE UPDATE ON candidate '
    'FOR EACH ROW EXECUTE FUNCTION candidate_freeze_fill_values()'
)

FILL_TOGETHER_CHECK = (
    'entry_price IS NULL OR (vertical_deadline IS NOT NULL AND ('
    '(one_r IS NOT NULL AND upper_barrier_price IS NOT NULL AND lower_barrier_price IS NOT NULL)'
    ' OR (one_r IS NULL AND upper_barrier_price IS NULL AND lower_barrier_price IS NULL)))'
)
NO_ENTRY_HAS_NO_R = (
    "first_barrier <> 'no_entry' OR (price_r IS NULL AND fees_r IS NULL AND funding_r IS NULL"
    ' AND stored_r IS NULL AND mae_r IS NULL AND mfe_r IS NULL AND hours_to_mfe IS NULL AND hours_to_resolution IS NULL'
    ' AND NOT barrier_ambiguous AND NOT gapped_stop AND NOT funding_partial)'
)
FILLED_HAS_R = (
    "first_barrier = 'no_entry' OR (price_r IS NOT NULL AND fees_r IS NOT NULL AND funding_r IS NOT NULL"
    ' AND stored_r IS NOT NULL AND mae_r IS NOT NULL AND hours_to_resolution IS NOT NULL)'
)


def upgrade() -> None:
    op.add_column(
        'label',
        sa.Column('feature_hash', sa.Text(), nullable=True, comment="The candidate's feature_hash: the features behind the decision."),
    )
    op.add_column(
        'label',
        sa.Column('rule_hash', sa.Text(), nullable=True, comment="The candidate's rule_hash: the frozen rules that took the decision."),
    )
    op.execute(
        'UPDATE label SET feature_hash = c.feature_hash, rule_hash = c.rule_hash '
        'FROM candidate c WHERE c.id = label.candidate_id'
    )
    op.execute("UPDATE label SET code_version = 'unknown' WHERE code_version IS NULL")
    op.alter_column('label', 'feature_hash', existing_type=sa.Text(), nullable=False)
    op.alter_column('label', 'rule_hash', existing_type=sa.Text(), nullable=False)
    op.alter_column(
        'label',
        'code_version',
        existing_type=sa.Text(),
        nullable=False,
        existing_comment='Resolver version that wrote this row.',
    )
    op.add_column(
        'label',
        sa.Column(
            'funding_partial',
            sa.Boolean(),
            server_default='false',
            nullable=False,
            comment='Funding was still incomplete when the stall cap passed: funding_r holds the prints seen so far.',
        ),
    )

    op.drop_constraint(op.f('ck_label_first_barrier'), 'label', type_='check')
    op.create_check_constraint(
        op.f('ck_label_first_barrier'), 'label', "first_barrier IN ('upper', 'lower', 'vertical', 'no_entry')"
    )
    op.alter_column(
        'label',
        'first_barrier',
        existing_type=sa.Text(),
        existing_nullable=False,
        comment=NEW_FIRST_BARRIER_COMMENT,
        existing_comment=OLD_FIRST_BARRIER_COMMENT,
    )
    for column in R_COLUMNS:
        op.alter_column('label', column, existing_type=sa.Numeric(14, 6), nullable=True)
    op.alter_column('label', 'hours_to_resolution', existing_type=sa.Numeric(12, 4), nullable=True)
    op.create_check_constraint(op.f('ck_label_no_entry_has_no_r'), 'label', NO_ENTRY_HAS_NO_R)
    op.create_check_constraint(op.f('ck_label_filled_label_has_r'), 'label', FILLED_HAS_R)

    op.create_check_constraint(op.f('ck_candidate_fill_values_written_together'), 'candidate', FILL_TOGETHER_CHECK)
    op.execute(FREEZE_FUNCTION_SQL)
    op.execute(FREEZE_TRIGGER_SQL)


def downgrade() -> None:
    op.execute('DROP TRIGGER IF EXISTS candidate_freeze_fill_values ON candidate')
    op.execute('DROP FUNCTION IF EXISTS candidate_freeze_fill_values()')
    op.drop_constraint(op.f('ck_candidate_fill_values_written_together'), 'candidate', type_='check')

    op.drop_constraint(op.f('ck_label_filled_label_has_r'), 'label', type_='check')
    op.drop_constraint(op.f('ck_label_no_entry_has_no_r'), 'label', type_='check')
    op.execute("DELETE FROM label WHERE first_barrier = 'no_entry'")
    op.alter_column('label', 'hours_to_resolution', existing_type=sa.Numeric(12, 4), nullable=False)
    for column in R_COLUMNS:
        op.alter_column('label', column, existing_type=sa.Numeric(14, 6), nullable=False)
    op.alter_column(
        'label',
        'first_barrier',
        existing_type=sa.Text(),
        existing_nullable=False,
        comment=OLD_FIRST_BARRIER_COMMENT,
        existing_comment=NEW_FIRST_BARRIER_COMMENT,
    )
    op.drop_constraint(op.f('ck_label_first_barrier'), 'label', type_='check')
    op.create_check_constraint(op.f('ck_label_first_barrier'), 'label', "first_barrier IN ('upper', 'lower', 'vertical')")

    op.drop_column('label', 'funding_partial')
    op.alter_column(
        'label',
        'code_version',
        existing_type=sa.Text(),
        nullable=True,
        existing_comment='Resolver version that wrote this row.',
    )
    op.drop_column('label', 'rule_hash')
    op.drop_column('label', 'feature_hash')
