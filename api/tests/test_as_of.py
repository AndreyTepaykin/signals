"""The available-at guard rail.

This is the test the whole project leans on. If a feed row that was published
after the signal bar closed can reach a decision-time feature, every label the
system ever collects is contaminated and nothing downstream will say so.

The decisive cases are the ones where the ECONOMIC time is at or before the bar
close but ``available_at`` is after it - a funding print that settles at 12:00
and publishes at 12:00:30, an open-interest poll whose bar closed at 12:00 and
landed at 12:05. Filtering on the economic time would let those in.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm import aliased

from app.models import enums as E
from app.models.as_of import FEED_MODELS, AsOfError, as_of
from app.models.base import Base, FeedRow
from app.models.decisions import Candidate
from app.models.features import FeatureRow
from app.models.feeds import Candle, EventCalendar, FeedHealth, ForceOrder, Funding, OpenInterest
from app.models.labels import Label
from app.models.options import OptionsMetric, OptionsSnapshot

UTC = timezone.utc
BAR_CLOSE = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# The API refuses to be used wrongly
# ---------------------------------------------------------------------------
def test_naive_instant_is_rejected():
    with pytest.raises(ValueError):
        as_of(datetime(2026, 1, 1, 12, 0))


def test_instant_is_normalised_to_utc():
    other_zone = timezone(timedelta(hours=3))
    reader = as_of(datetime(2026, 1, 1, 15, 0, tzinfo=other_zone))
    assert reader.bar_close == BAR_CLOSE
    assert reader.bar_close.tzinfo is UTC


def test_select_needs_an_entity():
    with pytest.raises(AsOfError):
        as_of(BAR_CLOSE).select()


@pytest.mark.parametrize('model', [FeatureRow, Candidate, Label, FeedHealth])
def test_select_refuses_non_feed_tables(model):
    """A label, a candidate, a feature row and feed_health are not gated feeds.

    This is what makes the label-only columns unreachable from the feature path:
    there is no argument to the sanctioned read helper that yields a label query.
    """
    with pytest.raises(AsOfError):
        as_of(BAR_CLOSE).select(model)


def test_select_refuses_a_label_column():
    with pytest.raises(AsOfError):
        as_of(BAR_CLOSE).select(Label.cmi_alt_residual_return)


def test_gate_refuses_a_statement_with_no_feed_table():
    with pytest.raises(AsOfError):
        as_of(BAR_CLOSE).gate(select(Candidate))


def test_gate_refuses_a_non_select():
    with pytest.raises(AsOfError):
        as_of(BAR_CLOSE).gate(Candle.__table__.delete())


def test_gate_refuses_a_feed_hidden_in_a_subquery():
    hidden = select(Candle.coin).subquery()
    with pytest.raises(AsOfError):
        as_of(BAR_CLOSE).gate(select(hidden.c.coin))


def test_gate_gates_an_alias():
    perp = aliased(Candle)
    gated = as_of(BAR_CLOSE).gate(select(perp.id))
    assert 'available_at <=' in str(gated)


def test_gate_applies_to_a_join_with_a_non_feed_table():
    statement = select(Candidate.id, Candle.id).join_from(
        Candidate, Candle, Candidate.coin == Candle.coin
    )
    gated = as_of(BAR_CLOSE).gate(statement)
    assert str(gated).count('available_at <=') == 1


def test_select_filter_is_in_the_sql():
    statement = as_of(BAR_CLOSE).select(Candle)
    assert 'candle.available_at <=' in str(statement)


def test_every_feed_model_is_enrolled():
    """A feed table added by a later milestone cannot escape the gate quietly."""
    mapped_feeds = {
        mapper.class_
        for mapper in Base.registry.mappers
        if issubclass(mapper.class_, FeedRow)
    }
    assert mapped_feeds == set(FEED_MODELS)


def test_every_feed_model_has_available_at():
    for model in FEED_MODELS:
        assert 'available_at' in inspect(model).columns
        assert inspect(model).columns['available_at'].type.timezone is True


# ---------------------------------------------------------------------------
# The gate against live rows
# ---------------------------------------------------------------------------
@pytest.fixture
def feed_rows(session):
    """Rows straddling BAR_CLOSE, labelled by whether the gate must see them.

    Every ``_late`` row is one whose economic time is at or before the bar close
    but which could not have been known then.
    """
    rows = {
        # ---- klines -------------------------------------------------------
        'candle_before': Candle(
            coin='BTCUSDT',
            market=E.MARKET_PERP,
            timeframe='15m',
            open_time=BAR_CLOSE - timedelta(minutes=30),
            close_time=BAR_CLOSE - timedelta(minutes=15),
            available_at=BAR_CLOSE - timedelta(minutes=15),
            open_price=Decimal('100'),
            high_price=Decimal('101'),
            low_price=Decimal('99'),
            close_price=Decimal('100.5'),
            base_volume=Decimal('10'),
            taker_buy_base_volume=Decimal('6'),
        ),
        'candle_signal_bar': Candle(
            coin='BTCUSDT',
            market=E.MARKET_PERP,
            timeframe='15m',
            open_time=BAR_CLOSE - timedelta(minutes=15),
            close_time=BAR_CLOSE,
            available_at=BAR_CLOSE,
            open_price=Decimal('100.5'),
            high_price=Decimal('102'),
            low_price=Decimal('100'),
            close_price=Decimal('101.5'),
            base_volume=Decimal('12'),
            taker_buy_base_volume=Decimal('9'),
        ),
        'candle_next_bar': Candle(
            coin='BTCUSDT',
            market=E.MARKET_PERP,
            timeframe='15m',
            open_time=BAR_CLOSE,
            close_time=BAR_CLOSE + timedelta(minutes=15),
            available_at=BAR_CLOSE + timedelta(minutes=15),
            open_price=Decimal('101.5'),
            high_price=Decimal('103'),
            low_price=Decimal('101'),
            close_price=Decimal('102.5'),
            base_volume=Decimal('14'),
            taker_buy_base_volume=Decimal('7'),
        ),
        # ---- funding ------------------------------------------------------
        'funding_settled_earlier': Funding(
            coin='BTCUSDT',
            kind=E.FUNDING_SETTLED,
            funding_time=BAR_CLOSE - timedelta(hours=4),
            available_at=BAR_CLOSE - timedelta(hours=4) + timedelta(seconds=5),
            funding_rate=Decimal('0.0001'),
        ),
        # Settles exactly at the bar close, published half a minute later: the
        # single most important row in this file.
        'funding_settled_at_bar_close_published_late': Funding(
            coin='BTCUSDT',
            kind=E.FUNDING_SETTLED,
            funding_time=BAR_CLOSE,
            available_at=BAR_CLOSE + timedelta(seconds=30),
            funding_rate=Decimal('0.0009'),
        ),
        'funding_settled_after': Funding(
            coin='BTCUSDT',
            kind=E.FUNDING_SETTLED,
            funding_time=BAR_CLOSE + timedelta(hours=4),
            available_at=BAR_CLOSE + timedelta(hours=4),
            funding_rate=Decimal('0.0002'),
        ),
        # ---- open interest -------------------------------------------------
        'oi_before': OpenInterest(
            coin='BTCUSDT',
            timeframe='15m',
            poll_time=BAR_CLOSE - timedelta(minutes=15),
            available_at=BAR_CLOSE - timedelta(minutes=15),
            open_interest_base=Decimal('1000'),
        ),
        # Poll bar closed at the signal bar close; the poll landed five minutes
        # later. Economic time passes a naive filter, availability does not.
        'oi_poll_bar_landed_late': OpenInterest(
            coin='BTCUSDT',
            timeframe='15m',
            poll_time=BAR_CLOSE,
            available_at=BAR_CLOSE + timedelta(minutes=5),
            open_interest_base=Decimal('1100'),
        ),
        # ---- force orders --------------------------------------------------
        'force_order_before': ForceOrder(
            coin='BTCUSDT',
            side='sell',
            traded_at=BAR_CLOSE - timedelta(minutes=2),
            available_at=BAR_CLOSE - timedelta(minutes=2),
            price=Decimal('99.5'),
            original_quantity=Decimal('3'),
        ),
        'force_order_after': ForceOrder(
            coin='BTCUSDT',
            side='buy',
            traded_at=BAR_CLOSE + timedelta(minutes=1),
            available_at=BAR_CLOSE + timedelta(minutes=1),
            price=Decimal('102'),
            original_quantity=Decimal('4'),
        ),
        # ---- options -------------------------------------------------------
        'snapshot_before': OptionsSnapshot(
            venue=E.OPTIONS_VENUE_DERIBIT,
            coin='BTC',
            snapshot_time=BAR_CLOSE - timedelta(minutes=5),
            available_at=BAR_CLOSE - timedelta(minutes=5),
            payload={'instruments': []},
        ),
        'snapshot_after': OptionsSnapshot(
            venue=E.OPTIONS_VENUE_DERIBIT,
            coin='BTC',
            snapshot_time=BAR_CLOSE + timedelta(minutes=1),
            available_at=BAR_CLOSE + timedelta(minutes=1),
            payload={'instruments': []},
        ),
        # ---- calendar ------------------------------------------------------
        'event_known_before': EventCalendar(
            kind=E.STAND_DOWN_EVENT_KINDS[0],
            event_time=BAR_CLOSE + timedelta(hours=2),
            available_at=BAR_CLOSE - timedelta(days=7),
        ),
        'event_published_after': EventCalendar(
            kind=E.EVENT_KIND_UNLOCK,
            coin='ETHUSDT',
            event_time=BAR_CLOSE + timedelta(hours=2),
            available_at=BAR_CLOSE + timedelta(minutes=1),
        ),
    }
    session.add_all(rows.values())
    session.flush()
    return rows


VISIBLE = {
    'candle_before',
    'candle_signal_bar',
    'funding_settled_earlier',
    'oi_before',
    'force_order_before',
    'snapshot_before',
    'event_known_before',
}


@pytest.mark.parametrize(
    ('model', 'keys'),
    [
        (Candle, ('candle_before', 'candle_signal_bar', 'candle_next_bar')),
        (
            Funding,
            (
                'funding_settled_earlier',
                'funding_settled_at_bar_close_published_late',
                'funding_settled_after',
            ),
        ),
        (OpenInterest, ('oi_before', 'oi_poll_bar_landed_late')),
        (ForceOrder, ('force_order_before', 'force_order_after')),
        (OptionsSnapshot, ('snapshot_before', 'snapshot_after')),
        (EventCalendar, ('event_known_before', 'event_published_after')),
    ],
)
def test_gate_excludes_rows_published_after_the_bar_close(session, feed_rows, model, keys):
    visible_ids = set(session.execute(as_of(BAR_CLOSE).select(model.id)).scalars().all())
    expected = {feed_rows[key].id for key in keys if key in VISIBLE}
    excluded = {feed_rows[key].id for key in keys if key not in VISIBLE}
    assert visible_ids == expected
    assert visible_ids.isdisjoint(excluded)


def test_the_signal_bar_itself_is_available(session, feed_rows):
    """available_at == bar_close is inclusive: the entry is the NEXT bar's open."""
    rows = session.execute(as_of(BAR_CLOSE).select(Candle).order_by(Candle.open_time)).scalars().all()
    assert [row.close_time for row in rows] == [
        BAR_CLOSE - timedelta(minutes=15),
        BAR_CLOSE,
    ]


def test_funding_settled_at_the_bar_close_but_published_later_is_excluded(session, feed_rows):
    """The clearest available-at case in the spec, asserted on its own.

    The print describes a period that ended exactly at the bar close. A join on
    economic time would include it. A join on available-at must not.
    """
    late = feed_rows['funding_settled_at_bar_close_published_late']
    assert late.funding_time <= BAR_CLOSE
    assert late.available_at > BAR_CLOSE

    visible = session.execute(as_of(BAR_CLOSE).select(Funding.id)).scalars().all()
    assert late.id not in visible

    economic_only = session.execute(
        select(Funding.id).where(Funding.funding_time <= BAR_CLOSE)
    ).scalars().all()
    assert late.id in economic_only, 'fixture no longer exercises the leak it exists to catch'


def test_latest_settled_funding_print(session, feed_rows):
    newest = as_of(BAR_CLOSE).latest(
        session,
        Funding,
        coin='BTCUSDT',
        kind=E.FUNDING_SETTLED,
        order_by=Funding.funding_time,
    )
    assert newest is not None
    assert newest.id == feed_rows['funding_settled_earlier'].id


def test_options_metric_is_gated_too(session, feed_rows):
    snapshot = feed_rows['snapshot_after']
    metric = OptionsMetric(
        snapshot_id=snapshot.id,
        venue=snapshot.venue,
        coin=snapshot.coin,
        snapshot_time=snapshot.snapshot_time,
        horizon=E.HORIZONS[1],
        available_at=snapshot.available_at,
        dealer_gamma_sign=-1,
    )
    session.add(metric)
    session.flush()
    visible = session.execute(as_of(BAR_CLOSE).select(OptionsMetric.id)).scalars().all()
    assert metric.id not in visible


def test_a_later_bar_close_sees_more(session, feed_rows):
    later = session.execute(
        as_of(BAR_CLOSE + timedelta(hours=1)).select(Candle.id)
    ).scalars().all()
    assert feed_rows['candle_next_bar'].id in later


def test_gate_on_a_hand_built_statement(session, feed_rows):
    statement = as_of(BAR_CLOSE).gate(
        select(Candle.close_price).where(Candle.coin == 'BTCUSDT').order_by(Candle.open_time.desc())
    )
    assert session.execute(statement).scalars().first() == Decimal('101.5000000000')
