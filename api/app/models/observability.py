"""Job runs and the promotion gate's decision log."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import enums as E
from app.models.base import Base, CreatedAt, Quantile, Ratio, RValue, SurrogateKey


class JobRun(SurrogateKey, CreatedAt, Base):
    """One execution of a scheduled job, for observability."""

    __tablename__ = 'job_run'

    job_name: Mapped[str] = mapped_column(nullable=False)
    started_at: Mapped[datetime] = mapped_column(nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True, comment='NULL while the job is running.')
    status: Mapped[str] = mapped_column(nullable=False, comment='running | ok | error | skipped')
    rows_touched: Mapped[int | None] = mapped_column(nullable=True)
    source: Mapped[str | None] = mapped_column(nullable=True, comment='live | replay, for jobs that write rows.')
    code_version: Mapped[str | None] = mapped_column(nullable=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    error: Mapped[str | None] = mapped_column(nullable=True)

    __table_args__ = (
        E.one_of('status', E.JOB_STATUSES),
        E.one_of('source', E.SOURCES),
        CheckConstraint('finished_at IS NULL OR finished_at >= started_at', name='finished_after_started'),
        CheckConstraint('rows_touched IS NULL OR rows_touched >= 0', name='rows_touched_non_negative'),
        Index('ix_job_run_job_name_started_at', 'job_name', 'started_at'),
        Index('ix_job_run_status_started_at', 'status', 'started_at'),
    )


class PromotionLog(SurrogateKey, CreatedAt, Base):
    """Every promotion-gate decision, including the rejections.

    The spec is specific about why this table exists: "Log the rejection,
    including the bin edges you used, so a later pass cannot quietly move them."
    ``bin_edges`` is therefore NOT NULL and must be a json array or object - a
    decision recorded without its edges is not a record of anything.

    The four gate criteria get their own boolean columns so that a rejection says
    which condition failed without anyone parsing jsonb to find out.
    """


    __tablename__ = 'promotion_log'

    layer: Mapped[str] = mapped_column(nullable=False, comment='Metric, indicator or context layer under test.')
    decided_at: Mapped[datetime] = mapped_column(nullable=False)
    decision: Mapped[str] = mapped_column(nullable=False, comment='promoted | rejected')

    shadow_window_start: Mapped[datetime | None] = mapped_column(nullable=True)
    shadow_window_end: Mapped[datetime | None] = mapped_column(nullable=True)
    shadow_weeks: Mapped[Ratio | None] = mapped_column(
        nullable=True,
        comment='Length of the shadow window in weeks, as measured.',
    )

    paired_outcomes: Mapped[int | None] = mapped_column(
        nullable=True,
        comment='Independent PAIRED outcomes in the locked book, not rows.',
    )
    mean_paired_diff_r: Mapped[RValue | None] = mapped_column(nullable=True)
    diff_ci_low: Mapped[RValue | None] = mapped_column(nullable=True)
    diff_ci_high: Mapped[RValue | None] = mapped_column(nullable=True)
    interval_confidence: Mapped[Quantile | None] = mapped_column(nullable=True)
    embargo_hours: Mapped[int | None] = mapped_column(nullable=True, comment='Embargo used; equals the horizon.')
    locked_book_only: Mapped[bool | None] = mapped_column(nullable=True)

    criterion_window_ok: Mapped[bool | None] = mapped_column(nullable=True)
    criterion_paired_outcomes_ok: Mapped[bool | None] = mapped_column(nullable=True)
    criterion_mean_diff_positive: Mapped[bool | None] = mapped_column(nullable=True)
    criterion_interval_above_zero: Mapped[bool | None] = mapped_column(nullable=True)

    bin_edges: Mapped[dict[str, Any]] = mapped_column(
        nullable=False,
        comment='The calibration bin edges used, verbatim, so a later pass cannot quietly move them.',
    )
    rule_hash: Mapped[str | None] = mapped_column(nullable=True)
    code_version: Mapped[str | None] = mapped_column(nullable=True)
    notes: Mapped[str | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint('layer', 'decided_at', name='uq_promotion_log_layer_decided_at'),
        E.one_of('decision', E.PROMOTION_DECISIONS),
        E.in_range('interval_confidence', 0, 1),
        CheckConstraint('paired_outcomes IS NULL OR paired_outcomes >= 0', name='paired_outcomes_non_negative'),
        CheckConstraint(
            'diff_ci_low IS NULL OR diff_ci_high IS NULL OR diff_ci_low <= diff_ci_high',
            name='diff_interval_ordered',
        ),
        CheckConstraint(
            "jsonb_typeof(bin_edges) IN ('array', 'object')",
            name='bin_edges_is_array_or_object',
        ),
        CheckConstraint(
            'shadow_window_end IS NULL OR shadow_window_start IS NULL OR shadow_window_end >= shadow_window_start',
            name='shadow_window_ordered',
        ),
        Index('ix_promotion_log_layer_decided_at', 'layer', 'decided_at'),
    )


__all__ = ['JobRun', 'PromotionLog']
