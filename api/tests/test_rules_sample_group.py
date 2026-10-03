"""Sample-group assignment and the one-open-signal test (pure)."""

from __future__ import annotations

import uuid
from datetime import timedelta

from app.core import constants as C
from app.core.rules import sample_group as G
from app.core.rules.sample_group import PriorCandidate, assign_sample_group, has_open_signal, occupies_slot
from tests.rules_helpers import T0

REF = 'BTCUSDT'
PENDING = C.STATUS_REASON_PENDING_OPTIONS_CHECK


def prior(pid, coin=REF, horizon='swing', hours=0, direction='long', status='watch', reason=PENDING, group=None):
    bar = T0 + timedelta(hours=hours)
    return PriorCandidate(
        id=pid,
        coin=coin,
        horizon=horizon,
        bar_close=bar,
        decision_time=bar + timedelta(seconds=40),
        direction=direction,
        status=status,
        status_reason=reason,
        sample_group_id=group or uuid.UUID(int=pid),
    )


def assign(priors, coin=REF, hours=2, horizon='swing', direction='long'):
    return assign_sample_group(
        coin=coin,
        horizon=horizon,
        source='live',
        bar_close=T0 + timedelta(hours=hours),
        direction=direction,
        priors=priors,
        reference_coin=REF,
    )


# ---------------------------------------------------------------------------
# new
# ---------------------------------------------------------------------------
def test_with_nothing_before_it_the_candidate_is_new():
    a = assign([])
    assert (a.reason, a.leader_id) == ('new', None)
    assert isinstance(a.sample_group_id, uuid.UUID)


def test_new_group_id_is_deterministic_and_unique_per_candidate_identity():
    a = G.new_group_id('BTCUSDT', 'swing', T0, 'live')
    assert a == G.new_group_id('BTCUSDT', 'swing', T0, 'live')
    others = {
        G.new_group_id('ETHUSDT', 'swing', T0, 'live'),
        G.new_group_id('BTCUSDT', 'scalp', T0, 'live'),
        G.new_group_id('BTCUSDT', 'swing', T0 + timedelta(minutes=15), 'live'),
        G.new_group_id('BTCUSDT', 'swing', T0, 'replay'),
    }
    assert a not in others and len(others) == 4
    assert a.version == 5


# ---------------------------------------------------------------------------
# overlap
# ---------------------------------------------------------------------------
def test_an_open_hold_on_the_same_coin_is_an_overlap_and_inherits_its_group():
    earlier = prior(1, hours=0)
    a = assign([earlier], hours=2)
    assert (a.reason, a.leader_id, a.sample_group_id) == ('overlap', 1, earlier.sample_group_id)


def test_a_shadow_row_still_overlaps_for_grouping_even_though_it_holds_no_slot():
    shadow = prior(1, hours=0, status='cancelled', reason='cvd_divergence_against')
    assert assign([shadow], hours=2).reason == 'overlap'
    assert not occupies_slot(shadow)


def test_the_hold_is_the_frozen_deadline_not_the_actual_resolution():
    # a swing hold is 24h: still open at 23h45, over at exactly 24h (half-open)
    earlier = prior(1, hours=0)
    assert assign([earlier], hours=23.75).reason == 'overlap'
    assert assign([earlier], hours=24).reason == 'new'
    assert assign([earlier], hours=30).reason == 'new'


def test_each_horizon_has_its_own_window():
    scalp = prior(1, horizon='scalp', hours=0)
    assert assign([scalp], hours=3.75, horizon='scalp').reason == 'overlap'
    assert assign([scalp], hours=4, horizon='scalp').reason == 'new'
    position = prior(2, horizon='position', hours=0)
    assert assign([position], hours=71, horizon='position').reason == 'overlap'


def test_priors_of_another_horizon_are_ignored():
    assert assign([prior(1, horizon='position', hours=0)], hours=2, horizon='swing').reason == 'new'


def test_a_candidate_on_the_same_bar_or_later_is_not_a_prior():
    assert assign([prior(1, hours=2)], hours=2).reason == 'new'
    assert assign([prior(1, hours=3)], hours=2).reason == 'new'


def test_overlap_inherits_from_the_earliest_open_candidate():
    first, second = prior(5, hours=0), prior(2, hours=1, group=uuid.UUID(int=999))
    a = assign([second, first], hours=2)
    assert a.leader_id == 5 and a.sample_group_id == first.sample_group_id


def test_an_alt_overlap_is_scoped_to_its_own_coin():
    eth_prior = prior(1, coin='ETHUSDT', hours=0)
    assert assign([eth_prior], coin='SOLUSDT', hours=2).reason == 'new'


# ---------------------------------------------------------------------------
# btc_impulse
# ---------------------------------------------------------------------------
def test_an_alt_riding_an_open_btc_candidate_in_the_same_direction_joins_its_group():
    btc = prior(1, coin=REF, hours=0, direction='long')
    a = assign([btc], coin='ETHUSDT', hours=0.25)
    assert (a.reason, a.leader_id, a.sample_group_id) == ('btc_impulse', 1, btc.sample_group_id)


def test_a_fifth_alt_long_in_the_same_btc_move_is_not_a_fifth_sample():
    btc = prior(1, coin=REF, hours=0)
    groups = {assign([btc], coin=coin, hours=0.25).sample_group_id for coin in ('ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'ADAUSDT')}
    assert groups == {btc.sample_group_id}


def test_an_opposite_direction_btc_candidate_is_not_the_same_impulse():
    btc = prior(1, coin=REF, hours=0, direction='short')
    assert assign([btc], coin='ETHUSDT', hours=0.25, direction='long').reason == 'new'


def test_btc_itself_never_takes_the_btc_impulse_branch():
    other_btc = prior(1, coin=REF, hours=0)
    assert assign([other_btc], coin=REF, hours=1).reason == 'overlap'


def test_a_btc_hold_that_has_ended_no_longer_groups_alts():
    btc = prior(1, coin=REF, hours=0)
    assert assign([btc], coin='ETHUSDT', hours=24).reason == 'new'


def test_a_shadow_btc_candidate_still_defines_the_impulse():
    btc = prior(1, coin=REF, hours=0, status='cancelled', reason='stand_down:event_window')
    assert assign([btc], coin='ETHUSDT', hours=1).reason == 'btc_impulse'


def test_overlap_wins_over_btc_impulse_and_is_documented_as_a_known_limit():
    own = prior(1, coin='ETHUSDT', hours=0)
    btc = prior(2, coin=REF, hours=1)
    a = assign([own, btc], coin='ETHUSDT', hours=2)
    assert (a.reason, a.leader_id) == ('overlap', 1)


# ---------------------------------------------------------------------------
# one open signal
# ---------------------------------------------------------------------------
def test_an_alert_and_a_pending_candidate_hold_the_slot():
    assert occupies_slot(prior(1, status='alert', reason=None))
    assert occupies_slot(prior(1, status='watch', reason=PENDING))


def test_shadow_and_resolved_statuses_do_not_hold_the_slot():
    for status, reason in (
        ('cancelled', 'cvd_divergence_against'),
        ('delayed', 'expiry_pin'),
        ('abstained', 'edge below cost'),
        ('watch', 'vote_conflict'),
        ('watch', 'expiry_pin'),
    ):
        assert not occupies_slot(prior(1, status=status, reason=reason)), (status, reason)


def test_open_signal_is_per_coin_and_per_horizon_over_the_frozen_hold():
    p = prior(1, coin='ETHUSDT', hours=0)
    when = T0 + timedelta(hours=2)
    assert has_open_signal([p], 'ETHUSDT', 'swing', when)
    assert not has_open_signal([p], 'SOLUSDT', 'swing', when)  # another coin
    assert not has_open_signal([p], 'ETHUSDT', 'scalp', when)  # another horizon (the prior is a swing)
    assert not has_open_signal([p], 'ETHUSDT', 'swing', T0 + timedelta(hours=24))  # hold over
    assert not has_open_signal([p], 'ETHUSDT', 'swing', T0)  # not earlier than itself


def test_a_btc_alert_does_not_block_an_alt():
    assert not has_open_signal([prior(1, coin=REF, status='alert', reason=None)], 'ETHUSDT', 'swing', T0 + timedelta(hours=1))
