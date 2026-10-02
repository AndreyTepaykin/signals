"""Outcomes: one label per candidate, written at resolution.

The primary key **is** ``candidate_id``, so a candidate cannot acquire two
labels and a label cannot exist without its candidate. Every status resolves
here, not just alerts: a cancelled or delayed candidate is resolved on the same
barriers, which is how the promotion gate can tell whether a filter blocked a
winner.

Label-only columns
------------------
``LABEL_ONLY_COLUMNS`` is the cross-market impulse, including the alt residual
over the hold. The spec says it twice: "Residual return over the hold is stored
on the label and is not a feature at decision time", and "Label-only columns are
written at resolution and are forbidden as decision-time features."

They are unreachable from the feature path by construction, not by discipline:

* They exist only on this table. ``feature_row`` has no column of any of these
  names, and a test asserts that.
* ``app.models.as_of`` is the only sanctioned read path for a decision-time
  feature, and it accepts only :class:`~app.models.base.FeedRow` subclasses.
  ``Label`` is not one, so ``as_of(bar_close).select(Label)`` raises rather than
  returning a selectable. There is no argument that makes it work.
* The decision-time counterpart of the residual lives on ``feature_row`` under a
  different, self-describing name - ``alt_residual_z_signal_bar`` - so nobody has
  to reach for the hold-period figure to find a residual.

``MFE_DIAGNOSTIC_COLUMNS`` are a weaker category: they are legitimate label
columns but "stay on the tracker as diagnostics. They do not redefine a miss."

Stored R
--------
``stored_r = price_r - fees_r + funding_r``, with ``fees_r`` a non-negative cost
magnitude and ``funding_r`` signed (positive when funding was received). A CHECK
enforces that decomposition to within one unit in the last place of
``numeric(14, 6)``, so a label whose parts do not add up cannot be stored.

The side exit scores are scored the same way entries are, and they never rewrite
``stored_r``. They are separate columns for exactly that reason.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.models import enums as E
from app.models.base import Base, Bps, CreatedAt, Hours, Price, Quantile, Rate, RValue

#: Written at resolution, forbidden as decision-time features. See the module
#: docstring for how that is enforced rather than merely stated.
LABEL_ONLY_COLUMNS = (
    'cmi_btc_return',
    'cmi_eth_btc_return',
    'cmi_dxy_return',
    'cmi_nasdaq_return',
    'cmi_alt_residual_return',
)

#: Diagnostics. They may be shown on the tracker and must not redefine a miss
#: or extend the adverse excursion.
MFE_DIAGNOSTIC_COLUMNS = ('mfe_r', 'hours_to_mfe')

#: One unit in the last place of numeric(14, 6); the tolerance of the stored-R
#: decomposition CHECK, there only to absorb per-component rounding.
_R_ULP = '0.000001'


class Label(CreatedAt, Base):
    """Resolved outcome of one candidate."""

    __tablename__ = 'label'

    candidate_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey('candidate.id', ondelete='CASCADE'),
        primary_key=True,
        comment='One label per candidate: the primary key IS the candidate.',
    )

    # ---- Fill and resolution ----
    fill_time: Mapped[datetime] = mapped_column(nullable=False, comment="Next bar's open after the signal bar.")
    fill_price: Mapped[Price] = mapped_column(nullable=False, comment='Must equal candidate.entry_price.')
    resolved_at: Mapped[datetime] = mapped_column(nullable=False)
    first_barrier: Mapped[str] = mapped_column(
        nullable=False,
        comment='upper | lower | vertical - whichever traded first.',
    )
    barrier_ambiguous: Mapped[bool] = mapped_column(
        nullable=False,
        server_default='false',
        comment='One bar touched both barriers. Resolved to the stop; flagged so the ambiguity is countable.',
    )
    gapped_stop: Mapped[bool] = mapped_column(
        nullable=False,
        server_default='false',
        comment='Stop gapped, so it filled at the worse of the stop and the open.',
    )
    hours_to_resolution: Mapped[Hours] = mapped_column(nullable=False)

    # ---- Stored R and its decomposition ----
    price_r: Mapped[RValue] = mapped_column(nullable=False, comment='Price move at resolution, in R, before costs.')
    fees_r: Mapped[RValue] = mapped_column(
        nullable=False,
        comment='Frozen taker fee plus slippage, both sides, in R. Non-negative cost magnitude.',
    )
    funding_r: Mapped[RValue] = mapped_column(
        nullable=False,
        comment='Funding that ACTUALLY printed during the hold, in R. Signed; positive means received.',
    )
    stored_r: Mapped[RValue] = mapped_column(
        nullable=False,
        comment='price_r - fees_r + funding_r. The one number expectancy is computed from.',
    )

    # ---- Excursions ----
    mae_r: Mapped[RValue] = mapped_column(
        nullable=False,
        comment='Maximum adverse excursion until the FIRST barrier, as a non-negative magnitude in R.',
    )
    mfe_r: Mapped[RValue | None] = mapped_column(nullable=True, comment='Diagnostic only.')
    hours_to_mfe: Mapped[Hours | None] = mapped_column(nullable=True, comment='Diagnostic only.')

    # ---- Regime slice (the pool key) ----
    regime_trend_family: Mapped[str | None] = mapped_column(nullable=True, comment='trend | chop')
    regime_vol_pct: Mapped[Quantile | None] = mapped_column(nullable=True, comment='Realized-vol percentile at decision.')
    regime_session: Mapped[str | None] = mapped_column(nullable=True, comment='asia | europe | us')
    regime_event_flag: Mapped[bool | None] = mapped_column(nullable=True)
    regime_is_weekend: Mapped[bool | None] = mapped_column(nullable=True)
    expiry_during_hold: Mapped[bool | None] = mapped_column(
        nullable=True,
        comment='Whether an expiry landed inside the hold.',
    )

    # ---- Lead venue ----
    lead_venue: Mapped[str | None] = mapped_column(nullable=True, comment='spot | perp | cme, on the signal bar.')
    lead_venue_oi_rose: Mapped[bool | None] = mapped_column(
        nullable=True,
        comment='Whether open interest rose with that bar.',
    )

    # ---- Liquidity and the cost assumption actually used ----
    spread_bps: Mapped[Bps | None] = mapped_column(nullable=True)
    cost_assumption_bps: Mapped[Bps] = mapped_column(
        nullable=False,
        comment='Round-trip cost actually charged to this label, recorded so a later change is visible.',
    )
    taker_fee_bps_per_side: Mapped[Bps | None] = mapped_column(nullable=True)
    slippage_bps_per_side: Mapped[Bps | None] = mapped_column(nullable=True)
    realized_funding_bps: Mapped[Bps | None] = mapped_column(
        nullable=True,
        comment='The funding that printed during the hold, in bps, before conversion to R.',
    )

    # ---- Your book at decision time ----
    hours_since_last_signal: Mapped[Hours | None] = mapped_column(nullable=True)
    book_overlap: Mapped[dict[str, Any] | None] = mapped_column(
        nullable=True,
        comment='Open directions, correlation bucket, last 20 outcomes of this setup family.',
    )

    # ---- Side exit scores: never rewrite stored_r ----
    side_exit_partials_r: Mapped[RValue | None] = mapped_column(nullable=True)
    side_exit_move_to_flat_r: Mapped[RValue | None] = mapped_column(nullable=True)
    side_exit_time_stop_r: Mapped[RValue | None] = mapped_column(nullable=True)
    side_exit_detail: Mapped[dict[str, Any] | None] = mapped_column(
        nullable=True,
        comment='Parameters each side score was computed with. Diagnostic only.',
    )

    # ---- Label-only cross-market impulse ----
    cmi_btc_return: Mapped[Rate | None] = mapped_column(nullable=True, comment='LABEL ONLY. BTC return over the hold.')
    cmi_eth_btc_return: Mapped[Rate | None] = mapped_column(nullable=True, comment='LABEL ONLY.')
    cmi_dxy_return: Mapped[Rate | None] = mapped_column(
        nullable=True,
        comment='LABEL ONLY. No feed yet; NULL when the print is missing, never zero.',
    )
    cmi_nasdaq_return: Mapped[Rate | None] = mapped_column(
        nullable=True,
        comment='LABEL ONLY. No feed yet; NULL when the print is missing, never zero.',
    )
    cmi_alt_residual_return: Mapped[Rate | None] = mapped_column(
        nullable=True,
        comment='LABEL ONLY. Residual over the hold. A flat residual marks an alt duplicate.',
    )

    # ---- Provenance of the resolution itself ----
    code_version: Mapped[str | None] = mapped_column(nullable=True, comment='Resolver version that wrote this row.')

    __table_args__ = (
        E.one_of('first_barrier', E.FIRST_BARRIERS),
        E.one_of('regime_trend_family', E.TREND_FAMILIES),
        E.one_of('regime_session', E.REGIME_SESSIONS),
        E.one_of('lead_venue', E.LEAD_VENUES),
        E.in_range('regime_vol_pct', 0, 1),
        E.non_negative('fees_r'),
        E.non_negative('mae_r'),
        E.non_negative('hours_to_resolution'),
        CheckConstraint('mfe_r IS NULL OR mfe_r >= 0', name='mfe_r_non_negative'),
        CheckConstraint(
            'hours_to_mfe IS NULL OR (hours_to_mfe >= 0 AND hours_to_mfe <= hours_to_resolution)',
            name='hours_to_mfe_within_hold',
        ),
        CheckConstraint('resolved_at >= fill_time', name='resolved_after_fill'),
        CheckConstraint(
            f'abs(stored_r - (price_r - fees_r + funding_r)) <= {_R_ULP}',
            name='stored_r_decomposition',
        ),
        Index('ix_label_resolved_at', 'resolved_at'),
        Index('ix_label_first_barrier_resolved_at', 'first_barrier', 'resolved_at'),
        Index('ix_label_regime_slice', 'regime_trend_family', 'regime_session', 'regime_event_flag'),
    )


__all__ = ['LABEL_ONLY_COLUMNS', 'MFE_DIAGNOSTIC_COLUMNS', 'Label']
