"""promotion decision log: append-only promotion_decision table

Revision ID: e3f9a1c47b20
Revises: d8b2f6a31e05
Create Date: 2026-10-14 10:15:00.000000+00:00

Phase 2 prep. ``python -m app.worker.promotion_job --record`` writes one row per (layer, cohort, horizon) verdict of
the shadow promotion gate, with every parameter it was judged by and every failed criterion. The trigger
``promotion_decision_append_only`` refuses UPDATE and DELETE, so a later pass cannot quietly move a logged edge.

Written by hand: autogenerate does not see CHECK constraints or triggers.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e3f9a1c47b20'
down_revision: Union[str, None] = 'd8b2f6a31e05'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APPEND_ONLY_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION promotion_decision_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION USING MESSAGE = 'promotion_decision is append-only: ' || TG_OP || ' refused', ERRCODE = 'check_violation';
END;
$$ LANGUAGE plpgsql
"""
APPEND_ONLY_TRIGGER_SQL = (
    'CREATE TRIGGER promotion_decision_append_only BEFORE UPDATE OR DELETE ON promotion_decision '
    'FOR EACH ROW EXECUTE FUNCTION promotion_decision_append_only()'
)


def upgrade() -> None:
    op.create_table('promotion_decision',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('layer', sa.Text(), nullable=False, comment='Layer under test (app.core.promotion.layers).'),
    sa.Column('source', sa.Text(), nullable=False, comment='live | replay. Only live binds.'),
    sa.Column('options_path', sa.Text(), nullable=False, comment='Cohort: options path.'),
    sa.Column('lookback', sa.Text(), nullable=False, comment='Cohort: thin | full.'),
    sa.Column('horizon', sa.Text(), nullable=False),
    sa.Column('verdict', sa.Text(), nullable=False, comment='promote | reject | insufficient; preview_* on a replay cell (advisory, never promote).'),
    sa.Column('binding', sa.Boolean(), nullable=False, comment='True on live cells only.'),
    sa.Column('n_pairs', sa.Integer(), nullable=False, comment='Independent, embargoed, locked-book pairs.'),
    sa.Column('mean_diff_r', sa.Numeric(precision=14, scale=6), nullable=True, comment='Mean of R with the layer minus R without.'),
    sa.Column('ci_low', sa.Numeric(precision=14, scale=6), nullable=True),
    sa.Column('ci_high', sa.Numeric(precision=14, scale=6), nullable=True),
    sa.Column('shadow_start', sa.DateTime(timezone=True), nullable=True, comment='Decision time of the first kept pair.'),
    sa.Column('as_of', sa.DateTime(timezone=True), nullable=False, comment='The instant the gate was evaluated at.'),
    sa.Column('window_days', sa.Numeric(precision=10, scale=4), nullable=False, comment='as_of - shadow_start, in days.'),
    sa.Column('params', postgresql.JSONB(astext_type=sa.Text()), nullable=False, comment='Every parameter used, bin edges included.'),
    sa.Column('reasons', postgresql.JSONB(astext_type=sa.Text()), nullable=False, comment='Every failed criterion, or all_criteria_met.'),
    sa.Column('rule_hash', sa.Text(), nullable=False, comment='rule_hash of the observations judged.'),
    sa.Column('code_version', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("source IN ('live', 'replay')", name=op.f('ck_promotion_decision_source')),
    sa.CheckConstraint("horizon IN ('scalp', 'swing', 'position')", name=op.f('ck_promotion_decision_horizon')),
    sa.CheckConstraint(
        "options_path IN ('no_options_history', 'no_options_book', 'options_layer', 'options_layer_ignored')",
        name=op.f('ck_promotion_decision_options_path'),
    ),
    sa.CheckConstraint("lookback IN ('thin', 'full')", name=op.f('ck_promotion_decision_lookback')),
    sa.CheckConstraint(
        "verdict IN ('promote', 'reject', 'insufficient', 'preview_promote', 'preview_reject', 'preview_insufficient')",
        name=op.f('ck_promotion_decision_verdict'),
    ),
    sa.CheckConstraint("binding = (source = 'live')", name=op.f('ck_promotion_decision_binding_only_on_live')),
    sa.CheckConstraint(
        "binding = (verdict IN ('promote', 'reject', 'insufficient'))",
        name=op.f('ck_promotion_decision_preview_verdict_iff_advisory'),
    ),
    sa.CheckConstraint('n_pairs >= 0', name=op.f('ck_promotion_decision_n_pairs_non_negative')),
    sa.CheckConstraint('window_days >= 0', name=op.f('ck_promotion_decision_window_days_non_negative')),
    sa.CheckConstraint('(n_pairs = 0) = (mean_diff_r IS NULL)', name=op.f('ck_promotion_decision_mean_present_iff_pairs')),
    sa.CheckConstraint(
        '(n_pairs = 0) = (shadow_start IS NULL)', name=op.f('ck_promotion_decision_shadow_start_present_iff_pairs')
    ),
    sa.CheckConstraint('ci_low IS NULL OR ci_high IS NULL OR ci_low <= ci_high', name=op.f('ck_promotion_decision_ci_ordered')),
    sa.CheckConstraint(
        'shadow_start IS NULL OR shadow_start <= as_of', name=op.f('ck_promotion_decision_shadow_start_not_after_as_of')
    ),
    sa.CheckConstraint("jsonb_typeof(params) = 'object'", name=op.f('ck_promotion_decision_params_is_object')),
    sa.CheckConstraint("jsonb_typeof(reasons) = 'array'", name=op.f('ck_promotion_decision_reasons_is_array')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_promotion_decision'))
    )
    op.create_index('ix_promotion_decision_layer_created_at', 'promotion_decision', ['layer', 'created_at'], unique=False)
    op.execute(APPEND_ONLY_FUNCTION_SQL)
    op.execute(APPEND_ONLY_TRIGGER_SQL)


def downgrade() -> None:
    op.execute('DROP TRIGGER IF EXISTS promotion_decision_append_only ON promotion_decision')
    op.execute('DROP FUNCTION IF EXISTS promotion_decision_append_only()')
    op.drop_index('ix_promotion_decision_layer_created_at', table_name='promotion_decision')
    op.drop_table('promotion_decision')
