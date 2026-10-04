"""Candidates: one frozen decision per coin, horizon and signal bar.

Independence and ``sample_group_id``
------------------------------------
The spec repeats one rule in several places: "One decision per coin per
horizon. The next signal on that coin waits until this one resolves or the
horizon ends. Overlapping holds, and alt signals that share one BTC impulse,
count as one independent sample. Card counts use that sample, not the raw alert
count." Every floor in the project - 30 for a pool, 50 paired, 300 for
LightGBM, 2,000 for sequence models - counts those samples.

``sample_group_id`` is that sample. It is a UUID assigned **at candidate
creation, by the rules engine (M4), and never rewritten afterwards** - not by
the labeler, not by the stats engine. Three consequences follow from that
choice and they are the reason for it:

* It cannot depend on an outcome. If the group were resolved later from how the
  trades turned out, independence would become a function of results and every
  sample floor would be contaminated by hindsight.
* It is known while the card is on screen, so "n" on the card is the same
  number the stats engine will later count.
* Replay reproduces the grouping structure, because it walks bars in order and
  applies the same two inheritance rules.

A candidate gets its group in exactly one of three ways, recorded in
``sample_group_reason``:

``new``
    No reason to share. Mint a fresh UUID and leave ``sample_group_leader_id``
    NULL.
``overlap``
    A candidate already exists on the same coin, horizon and source whose
    ``vertical_deadline`` is later than this candidate's ``decision_time`` - the
    two holds can overlap in time. Inherit that candidate's group. Note the test
    is the frozen deadline, not the actual resolution: a prior trade that
    resolved early still swallows the new one. That is deliberately
    conservative, because under-counting independent samples delays a promotion
    whereas over-counting invalidates it.
``btc_impulse``
    This is an alt candidate that only exists because the one-BTC-view gate let
    it through on a BTC impulse. Inherit the BTC candidate's group, so a fifth
    long in the same BTC move is not a fifth sample.

Both rules are inheritance from a named row, so the group is a transitive
closure and ``COUNT(DISTINCT sample_group_id)`` is the independent-sample count.
The grouping key therefore does not need to encode "coin + horizon + window" or
"impulse id" as data: the leader pointer expresses both shapes, and a cluster
that is both an overlap and a BTC impulse collapses into one group for free.

Three invariants are enforced by the database rather than by convention,
through one composite self-referencing foreign key against
``uq_candidate_sample_group_identity``: a leader must carry the **same**
``sample_group_id``, the **same** ``horizon`` and the **same** ``source``.
So a replay row cannot join a live group, a scalp cannot join a swing group
(horizons are independent by the "one decision per coin per horizon" rule), and
nobody can point at a leader while minting a different group id. When
``sample_group_leader_id`` is NULL the foreign key is satisfied automatically
(Postgres MATCH SIMPLE), which is exactly the ``new`` case.

How M8 must count
-----------------
Independent outcomes::

    SELECT count(DISTINCT c.sample_group_id)
    FROM label l JOIN candidate c ON c.id = l.candidate_id
    WHERE c.source = 'live' AND ...

and to collapse a group to one observation before averaging stored R, take the
earliest decision time in the group::

    ROW_NUMBER() OVER (PARTITION BY c.sample_group_id ORDER BY c.decision_time, c.id) = 1

M8 owns the choice between "first of group" and "mean of group"; the schema
supports both and neither needs a schema change.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DDL, BigInteger, CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from app.core import constants as C
from app.models import enums as E
from app.models.base import (
    Base,
    CreatedAt,
    Hours,
    Influence,
    Multiplier,
    Price,
    Quantile,
    RValue,
    Sign,
    SurrogateKey,
)

_REASON_REQUIRED = ', '.join("'" + status + "'" for status in E.STATUSES_REQUIRING_REASON)


class Candidate(SurrogateKey, CreatedAt, Base):
    """A frozen decision. Paper only: nothing here is an order.

    Price columns come in two layers. ``pre_options_*`` is what the frozen rules
    produced; ``stop_price`` and ``target_cap_price`` are what the options check
    left behind (amplify may have widened the stop by half an ATR, headwind may
    have capped the target at a strike). The label barriers are frozen only
    after that step.

    ``entry_price``, ``one_r``, ``upper_barrier_price``, ``lower_barrier_price``
    and ``vertical_deadline`` are NULL at creation and written exactly once, by
    whoever observes the next bar's open, because the entry **is** that open and
    is not knowable when the decision is taken. ``lower_barrier_price`` always
    equals ``stop_price`` (the stop is -1R by definition) and a CHECK says so.

    ``upper_barrier_price`` is the spec's name and means the +1R barrier **in R
    space**: for a short it sits at a LOWER price than the entry. The
    direction-consistency CHECK below exists so that this cannot be stored the
    wrong way round.
    """

    __tablename__ = 'candidate'

    # ---- Identity and time ----
    decision_time: Mapped[datetime] = mapped_column(
        nullable=False,
        comment='When the rules ran. Printed on the tracker.',
    )
    bar_close: Mapped[datetime] = mapped_column(
        nullable=False,
        comment='Close of the signal bar. The as_of instant for every feed read behind this row.',
    )
    coin: Mapped[str] = mapped_column(nullable=False)
    horizon: Mapped[str] = mapped_column(nullable=False)
    direction: Mapped[str] = mapped_column(nullable=False, comment='long | short')
    source: Mapped[str] = mapped_column(nullable=False, comment='live | replay')

    # ---- Setup identity ----
    trigger: Mapped[str] = mapped_column(nullable=False, comment='swing_break | squeeze | vwap_tag | pullback_reset')
    setup_family: Mapped[str] = mapped_column(
        nullable=False,
        comment='Pool key dimension. Closed vocabulary frozen by M4: breakout | pullback | squeeze | reversion.',
    )
    trend_family: Mapped[str] = mapped_column(nullable=False, comment='trend | chop, the family that was allowed to fire.')

    # ---- The three hashes ----
    feature_hash: Mapped[str] = mapped_column(nullable=False)
    rule_hash: Mapped[str] = mapped_column(nullable=False)
    code_version: Mapped[str] = mapped_column(nullable=False)
    feature_row_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment='Features this decision was taken from. The composite FK pins coin, horizon, bar and source too.',
    )

    # ---- Pre-options rule output ----
    pre_options_stop_price: Mapped[Price] = mapped_column(
        nullable=False,
        comment='Chandelier stop from the frozen rules. Absolute price, known at bar close.',
    )
    pre_options_target_price: Mapped[Price | None] = mapped_column(nullable=True, comment='Model target.')

    # ---- Post-options, pre-fill ----
    stop_price: Mapped[Price] = mapped_column(
        nullable=False,
        comment='Final stop after the options check. Equals pre_options_stop_price unless amplify widened it.',
    )
    target_cap_price: Mapped[Price | None] = mapped_column(
        nullable=True,
        comment='Strike cap from headwind / expiry_pin. NULL = no cap.',
    )

    # ---- Frozen at the fill, written once ----
    entry_price: Mapped[Price | None] = mapped_column(nullable=True, comment="Next bar's open after the signal bar.")
    one_r: Mapped[Price | None] = mapped_column(nullable=True, comment='Entry to final stop, in price terms.')
    upper_barrier_price: Mapped[Price | None] = mapped_column(
        nullable=True,
        comment='+1R in the trade direction, or the cap if nearer. Below entry for a short.',
    )
    lower_barrier_price: Mapped[Price | None] = mapped_column(nullable=True, comment='The stop. -1R by definition.')
    vertical_deadline: Mapped[datetime | None] = mapped_column(
        nullable=True,
        comment='Fill time plus the horizon. Not the bar close plus the horizon.',
    )

    # ---- Options verdict (the alert line) ----
    options_verdict: Mapped[str | None] = mapped_column(
        nullable=True,
        comment='NULL only before the options check has run. An alert may not have a NULL verdict.',
    )
    options_venue: Mapped[str | None] = mapped_column(nullable=True, comment='Never a blend of two books.')
    options_vol_tag: Mapped[str | None] = mapped_column(nullable=True, comment='vol_rich | vol_cheap, beside the verdict.')
    options_dealer_gamma_sign: Mapped[Sign | None] = mapped_column(
        nullable=True,
        comment='-1 dealers short gamma, +1 dealers long gamma, 0 undetermined.',
    )
    options_nearest_path_strike: Mapped[Price | None] = mapped_column(nullable=True)
    options_hours_to_expiry: Mapped[Hours | None] = mapped_column(nullable=True)
    options_expression: Mapped[str | None] = mapped_column(
        nullable=True,
        comment='perp | spot | defined_risk_option | wait_for_expiry. Advice beside the perp label.',
    )
    size_multiplier: Mapped[Multiplier | None] = mapped_column(nullable=True)
    options_influence: Mapped[Influence | None] = mapped_column(
        nullable=True,
        comment='-1 to +1, the structural verdict recorded for later tests. NOT a multiplier on P.',
    )
    options_confidence: Mapped[Quantile | None] = mapped_column(
        nullable=True,
        comment='Book depth, so a thin book cannot look like a strong verdict.',
    )
    options_metric_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey('options_metric.id'),
        nullable=True,
        comment='The derived row the verdict was taken from; the tracker reads flip distance and skew through it.',
    )
    options_snapshot_time: Mapped[datetime | None] = mapped_column(nullable=True)

    # ---- The four estimates and their provenance ----
    p_upper_before_stop: Mapped[Quantile | None] = mapped_column(
        nullable=True,
        comment='Pooled share where the upper barrier traded first. The confidence on the card.',
    )
    expected_r: Mapped[RValue | None] = mapped_column(nullable=True, comment='Mean stored R in that pool.')
    expected_mae_r: Mapped[RValue | None] = mapped_column(
        nullable=True,
        comment='Mean MAE until the FIRST barrier, in R.',
    )
    median_hours_to_resolution: Mapped[Hours | None] = mapped_column(nullable=True)
    pool_level: Mapped[str | None] = mapped_column(nullable=True, comment='Walk-up level that supplied the estimates.')
    pool_n: Mapped[int | None] = mapped_column(nullable=True, comment='Independent outcomes in that pool, not rows.')
    expected_r_ci_low: Mapped[RValue | None] = mapped_column(nullable=True)
    expected_r_ci_high: Mapped[RValue | None] = mapped_column(nullable=True)
    pool_interval_confidence: Mapped[Quantile | None] = mapped_column(
        nullable=True,
        comment='Confidence level the interval was computed at, recorded rather than assumed.',
    )

    # ---- Status ----
    status: Mapped[str] = mapped_column(
        nullable=False,
        comment='alert | watch | cancelled | delayed | abstained. All five resolve on the same barriers.',
    )
    status_reason: Mapped[str | None] = mapped_column(
        nullable=True,
        comment='Which gate suppressed it, or which abstain rule fired. Required unless status is alert.',
    )

    # ---- Independence and shadow bookkeeping ----
    locked_book: Mapped[bool] = mapped_column(
        nullable=False,
        comment='One in five, by hash of coin and bar_close, decided before any outcome exists.',
    )
    sample_group_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
        comment='Independent-sample key. Assigned at creation by the rules engine, never rewritten.',
    )
    sample_group_leader_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment='The candidate this row inherited its group from. NULL only when the reason is new.',
    )
    sample_group_reason: Mapped[str] = mapped_column(nullable=False, comment='new | overlap | btc_impulse')

    __table_args__ = (
        UniqueConstraint('coin', 'horizon', 'bar_close', 'source', name='uq_candidate_series_bar_close'),
        # Target of the composite self-FK below. Also the cheapest way to assert
        # that a group never spans a horizon or a source.
        UniqueConstraint(
            'id',
            'sample_group_id',
            'horizon',
            'source',
            name='uq_candidate_sample_group_identity',
        ),
        ForeignKeyConstraint(
            ['sample_group_leader_id', 'sample_group_id', 'horizon', 'source'],
            ['candidate.id', 'candidate.sample_group_id', 'candidate.horizon', 'candidate.source'],
            name='fk_candidate_sample_group_leader',
        ),
        # The features behind a decision must be the features of THIS coin,
        # horizon, signal bar and source. Enforced, not assumed.
        ForeignKeyConstraint(
            ['feature_row_id', 'coin', 'horizon', 'bar_close', 'source'],
            [
                'feature_row.id',
                'feature_row.coin',
                'feature_row.horizon',
                'feature_row.bar_close',
                'feature_row.source',
            ],
            name='fk_candidate_feature_row',
        ),
        CheckConstraint(
            "(sample_group_leader_id IS NULL) = (sample_group_reason = '" + E.SAMPLE_GROUP_NEW + "')",
            name='sample_group_leader_matches_reason',
        ),
        E.one_of('horizon', E.HORIZONS),
        E.one_of('direction', E.DIRECTIONS),
        E.one_of('source', E.SOURCES),
        E.one_of('trigger', E.TRIGGERS),
        E.one_of('setup_family', E.SETUP_FAMILIES),
        E.one_of('trend_family', E.TREND_FAMILIES),
        E.one_of('status', E.CANDIDATE_STATUSES),
        E.one_of('sample_group_reason', E.SAMPLE_GROUP_REASONS),
        E.one_of('options_verdict', E.OPTIONS_VERDICTS),
        E.one_of('options_venue', E.OPTIONS_VENUES),
        E.one_of('options_vol_tag', E.OPTIONS_VOL_TAGS),
        E.one_of('options_expression', E.OPTIONS_EXPRESSIONS),
        E.one_of('pool_level', E.POOL_LEVELS),
        CheckConstraint(f'status NOT IN ({_REASON_REQUIRED}) OR status_reason IS NOT NULL', name='status_reason_present'),
        # "Freeze entry, 1R, upper and lower barriers only after this step":
        # an alert that never went through the options check is a bug.
        CheckConstraint(
            f"status <> '{E.STATUS_ALERT}' OR options_verdict IS NOT NULL",
            name='alert_has_options_verdict',
        ),
        # Milestone 5: the pending marker means "the options check has not run", so a row that still carries it
        # may not have a verdict. (That every non-ignore verdict names its options_metric row is enforced by the
        # options job and its tests, not here: the shared test fixtures build verdicts without a metric row.)
        CheckConstraint(
            f"status_reason IS DISTINCT FROM '{C.STATUS_REASON_PENDING_OPTIONS_CHECK}' OR options_verdict IS NULL",
            name='pending_has_no_verdict',
        ),
        CheckConstraint('options_dealer_gamma_sign IN (-1, 0, 1)', name='options_dealer_gamma_sign'),
        E.in_range('options_influence', -1, 1),
        E.in_range('options_confidence', 0, 1),
        E.in_range('p_upper_before_stop', 0, 1),
        E.in_range('pool_interval_confidence', 0, 1),
        CheckConstraint('size_multiplier IS NULL OR size_multiplier > 0', name='size_multiplier_positive'),
        CheckConstraint('pool_n IS NULL OR pool_n >= 0', name='pool_n_non_negative'),
        CheckConstraint('one_r IS NULL OR one_r > 0', name='one_r_positive'),
        CheckConstraint(
            'expected_r_ci_low IS NULL OR expected_r_ci_high IS NULL OR expected_r_ci_low <= expected_r_ci_high',
            name='expected_r_interval_ordered',
        ),
        # "Show expected R, n, the 90% interval, and the level you actually
        # used": an edge number without its provenance may not be stored.
        CheckConstraint(
            'expected_r IS NULL OR (pool_level IS NOT NULL AND pool_n IS NOT NULL)',
            name='expected_r_has_pool_provenance',
        ),
        CheckConstraint(
            'lower_barrier_price IS NULL OR lower_barrier_price = stop_price',
            name='lower_barrier_is_the_stop',
        ),
        # Milestone 6 writes the fill-time values in one statement, so a row never holds some of them. An entry price
        # comes with the deadline and with either all of 1R and both barriers, or none of them (a `no_entry` fill:
        # the open was at or through the stop or the cap, so the observed open is recorded and nothing else).
        CheckConstraint(
            'entry_price IS NULL OR (vertical_deadline IS NOT NULL AND ('
            '(one_r IS NOT NULL AND upper_barrier_price IS NOT NULL AND lower_barrier_price IS NOT NULL)'
            ' OR (one_r IS NULL AND upper_barrier_price IS NULL AND lower_barrier_price IS NULL)))',
            name='fill_values_written_together',
        ),
        # The upper barrier is +1R in R space, which for a short is a lower
        # price. This is the guard against storing it the wrong way round.
        CheckConstraint(
            'entry_price IS NULL OR upper_barrier_price IS NULL OR lower_barrier_price IS NULL OR ('
            f"(direction = '{E.DIRECTION_LONG}'"
            ' AND upper_barrier_price > entry_price AND lower_barrier_price < entry_price)'
            f" OR (direction = '{E.DIRECTION_SHORT}'"
            ' AND upper_barrier_price < entry_price AND lower_barrier_price > entry_price))',
            name='barriers_match_direction',
        ),
        CheckConstraint('bar_close <= decision_time', name='decision_not_before_bar_close'),
        CheckConstraint(
            'vertical_deadline IS NULL OR vertical_deadline > decision_time',
            name='deadline_after_decision',
        ),
        Index('ix_candidate_coin_horizon_decision_time', 'coin', 'horizon', 'decision_time'),
        Index('ix_candidate_sample_group_id', 'sample_group_id'),
        Index('ix_candidate_status_decision_time', 'status', 'decision_time'),
        Index('ix_candidate_source_decision_time', 'source', 'decision_time'),
        Index('ix_candidate_vertical_deadline', 'vertical_deadline'),
        Index('ix_candidate_coin_horizon_source_deadline', 'coin', 'horizon', 'source', 'vertical_deadline'),
        # Postgres does not index foreign keys for you, and all three of these
        # are joined or cascaded through.
        Index('ix_candidate_feature_row_id', 'feature_row_id'),
        Index('ix_candidate_options_metric_id', 'options_metric_id'),
        Index('ix_candidate_sample_group_leader_id', 'sample_group_leader_id'),
    )


#: The fill-time values are written once, in one statement (NULL -> value). Once the entry price is set, none of the five
#: may change again, including a `no_entry` row's NULL 1R and barriers; nothing can clear the deadline either. It fails
#: inside the database, whoever the caller is. The migration carries a copy of this SQL.
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
event.listen(Candidate.__table__, 'after_create', DDL(FREEZE_FUNCTION_SQL).execute_if(dialect='postgresql'))
event.listen(Candidate.__table__, 'after_create', DDL(FREEZE_TRIGGER_SQL).execute_if(dialect='postgresql'))


__all__ = ['FREEZE_FUNCTION_SQL', 'FREEZE_TRIGGER_SQL', 'Candidate']
