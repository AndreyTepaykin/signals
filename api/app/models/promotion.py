"""The promotion gate's append-only decision log (Phase 2 prep).

One row per (layer, cohort, horizon) each time ``python -m app.worker.promotion_job --record`` runs. The spec: "Log the
rejection, including the bin edges you used, so a later pass cannot quietly move them." So every row carries the
parameters it was judged by (``params``: floors, confidence, bootstrap seed and resamples, embargo per horizon, bin
edges, the layer's definition) and every failed criterion (``reasons``).

Append-only, in the database: the trigger ``promotion_decision_append_only`` refuses UPDATE and DELETE whoever the
caller is. A changed mind is a new row with a later ``created_at``. Nothing here is read by a live
decision; promoting a layer into the rules is a human ``RULE_VERSION`` bump.

The vocabularies are restated here (not imported) because ``app.core.stats`` and ``app.core.promotion`` import
``app.models``; a test pins them to their sources.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DDL, CheckConstraint, Index, Numeric, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models import enums as E
from app.models.base import Base, CreatedAt, RValue, SurrogateKey

OPTIONS_PATHS = ('no_options_history', 'no_options_book', 'options_layer', 'options_layer_ignored')
LOOKBACKS = ('thin', 'full')
BINDING_VERDICTS = ('promote', 'reject', 'insufficient')
PREVIEW_VERDICTS = ('preview_promote', 'preview_reject', 'preview_insufficient')
VERDICTS = BINDING_VERDICTS + PREVIEW_VERDICTS

_BINDING_RENDERED = ', '.join("'" + v + "'" for v in BINDING_VERDICTS)


class PromotionDecision(SurrogateKey, CreatedAt, Base):
    """One gate verdict for one layer in one cohort and horizon. Written once, never changed."""

    __tablename__ = 'promotion_decision'

    layer: Mapped[str] = mapped_column(nullable=False, comment='Layer under test (app.core.promotion.layers).')
    source: Mapped[str] = mapped_column(nullable=False, comment='live | replay. Only live binds.')
    options_path: Mapped[str] = mapped_column(nullable=False, comment='Cohort: options path.')
    lookback: Mapped[str] = mapped_column(nullable=False, comment='Cohort: thin | full.')
    horizon: Mapped[str] = mapped_column(nullable=False)
    verdict: Mapped[str] = mapped_column(
        nullable=False,
        comment='promote | reject | insufficient; preview_* on a replay cell (advisory, never promote).',
    )
    binding: Mapped[bool] = mapped_column(nullable=False, comment='True on live cells only.')
    n_pairs: Mapped[int] = mapped_column(nullable=False, comment='Independent, embargoed, locked-book pairs.')
    mean_diff_r: Mapped[RValue | None] = mapped_column(nullable=True, comment='Mean of R with the layer minus R without.')
    ci_low: Mapped[RValue | None] = mapped_column(nullable=True)
    ci_high: Mapped[RValue | None] = mapped_column(nullable=True)
    shadow_start: Mapped[datetime | None] = mapped_column(nullable=True, comment='Decision time of the first kept pair.')
    as_of: Mapped[datetime] = mapped_column(nullable=False, comment='The instant the gate was evaluated at.')
    window_days: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False, comment='as_of - shadow_start, in days.')
    params: Mapped[dict[str, Any]] = mapped_column(nullable=False, comment='Every parameter used, bin edges included.')
    reasons: Mapped[list[Any]] = mapped_column(JSONB(), nullable=False, comment='Every failed criterion, or all_criteria_met.')
    rule_hash: Mapped[str] = mapped_column(nullable=False, comment='rule_hash of the observations judged.')
    code_version: Mapped[str] = mapped_column(nullable=False)

    __table_args__ = (
        E.one_of('source', E.SOURCES),
        E.one_of('horizon', E.HORIZONS),
        E.one_of('options_path', OPTIONS_PATHS),
        E.one_of('lookback', LOOKBACKS),
        E.one_of('verdict', VERDICTS),
        CheckConstraint(f"binding = (source = '{E.SOURCE_LIVE}')", name='binding_only_on_live'),
        CheckConstraint(f'binding = (verdict IN ({_BINDING_RENDERED}))', name='preview_verdict_iff_advisory'),
        E.non_negative('n_pairs'),
        E.non_negative('window_days'),
        CheckConstraint('(n_pairs = 0) = (mean_diff_r IS NULL)', name='mean_present_iff_pairs'),
        CheckConstraint('(n_pairs = 0) = (shadow_start IS NULL)', name='shadow_start_present_iff_pairs'),
        CheckConstraint('ci_low IS NULL OR ci_high IS NULL OR ci_low <= ci_high', name='ci_ordered'),
        CheckConstraint('shadow_start IS NULL OR shadow_start <= as_of', name='shadow_start_not_after_as_of'),
        CheckConstraint("jsonb_typeof(params) = 'object'", name='params_is_object'),
        CheckConstraint("jsonb_typeof(reasons) = 'array'", name='reasons_is_array'),
        Index('ix_promotion_decision_layer_created_at', 'layer', 'created_at'),
    )


#: Refuses every UPDATE and DELETE. TRUNCATE is left alone: the test fixtures empty every table with it. The migration
#: carries a copy.
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
event.listen(PromotionDecision.__table__, 'after_create', DDL(APPEND_ONLY_FUNCTION_SQL).execute_if(dialect='postgresql'))
event.listen(PromotionDecision.__table__, 'after_create', DDL(APPEND_ONLY_TRIGGER_SQL).execute_if(dialect='postgresql'))


__all__ = [
    'APPEND_ONLY_FUNCTION_SQL',
    'APPEND_ONLY_TRIGGER_SQL',
    'PromotionDecision',
    'VERDICTS',
]
