"""The abstain rule (M10): pure arithmetic, hashed conventions."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core import constants as C
from app.core import hashing as H
from app.core.rules import abstain as AB

REF = Decimal('96500')
STOP = Decimal('95000')  # 1R = 1,500, so 1 bps of carry is 0.006433 R


def check(**kw):
    values = dict(status='alert', pool_n=30, expected_r_ci_low=0.1, funding_carry_bps=None, reference_price=REF, stop_price=STOP)
    values.update(kw)
    return AB.check_abstain(**values)


@pytest.mark.parametrize('status', ['watch', 'cancelled', 'delayed', 'abstained'])
def test_only_an_alert_can_abstain(status):
    result = check(status=status, expected_r_ci_low=-5.0)
    assert not result.abstain and not result.tested


@pytest.mark.parametrize('pool_n', [None, 0, 29])
def test_below_the_pool_floor_nothing_abstains(pool_n):
    result = check(pool_n=pool_n, expected_r_ci_low=-5.0)
    assert not result.abstain and not result.tested


def test_an_unknown_carry_tests_the_lower_bound_against_zero():
    assert check(expected_r_ci_low=-0.0001).abstain
    assert not check(expected_r_ci_low=0.0).abstain  # strictly below
    assert check(expected_r_ci_low=0.0).cost_edge_r == 0.0


def test_the_funding_edge_is_the_absolute_carry_in_r_on_the_signal_close_and_post_options_stop():
    result = check(funding_carry_bps=Decimal('-30'), expected_r_ci_low=0.19)
    assert result.funding_edge_bps == Decimal('30')
    assert result.cost_edge_r == pytest.approx(30 / 1e4 * 96500 / 1500)  # 0.193
    assert result.abstain
    assert not check(funding_carry_bps=30.0, expected_r_ci_low=0.2).abstain
    widened = check(funding_carry_bps=Decimal('30'), expected_r_ci_low=0.15, stop_price=Decimal('94500'))  # amplify: 1R = 2,000
    assert widened.cost_edge_r == pytest.approx(30 / 1e4 * 96500 / 2000) and not widened.abstain


def test_a_pool_at_the_floor_without_an_interval_abstains():
    assert C.ABSTAIN_WITHOUT_INTERVAL is True
    assert check(expected_r_ci_low=None).abstain


def test_a_zero_one_r_is_refused():
    with pytest.raises(ValueError):
        check(stop_price=REF)


def test_the_floor_is_the_edge_line_floor():
    assert C.ABSTAIN_MIN_POOL_INDEPENDENT == C.POOL_MIN_INDEPENDENT_OUTCOMES == 30


@pytest.mark.parametrize(
    ('name', 'bad'),
    [
        ('ABSTAIN_APPLIES_TO_STATUS', 'watch'),
        ('ABSTAIN_TESTED_BOUND', 'expected_r'),
        ('ABSTAIN_COST_EDGE', 'round_trip_fees'),
        ('ABSTAIN_FUNDING_EDGE_NOTIONAL', 'entry_price'),
        ('ABSTAIN_WITHOUT_INTERVAL', 'yes'),
        ('ABSTAIN_UNKNOWN_FUNDING_EDGE_BPS', Decimal('-1')),
    ],
)
def test_a_convention_the_code_does_not_implement_is_refused(monkeypatch, name, bad):
    monkeypatch.setattr(C, name, bad)
    with pytest.raises(ValueError):
        AB.check_conventions()


ABSTAIN_AND_ESTIMATE_NAMES = (
    'ABSTAIN_APPLIES_TO_STATUS',
    'ABSTAIN_MIN_POOL_INDEPENDENT',
    'ABSTAIN_TESTED_BOUND',
    'ABSTAIN_COST_EDGE',
    'ABSTAIN_FUNDING_EDGE_NOTIONAL',
    'ABSTAIN_UNKNOWN_FUNDING_EDGE_BPS',
    'ABSTAIN_WITHOUT_INTERVAL',
    'STATUS_REASON_ABSTAIN_EDGE_BELOW_COST',
    'ESTIMATE_BOOTSTRAP_RESAMPLES',
    'ESTIMATE_BOOTSTRAP_SEED',
    'ESTIMATE_VOL_BUCKET_EDGES',
    'ESTIMATE_VOL_BUCKET_NAMES',
    'ESTIMATE_THIN_LOOKBACK_CUTOFF_UTC',
)


@pytest.mark.parametrize('name', ABSTAIN_AND_ESTIMATE_NAMES)
def test_every_abstain_and_estimate_value_moves_the_rule_hash(name):
    frozen = H.frozen_constants()
    assert name in frozen
    changed = dict(frozen)
    value = frozen[name]
    changed[name] = (not value) if isinstance(value, bool) else ([*value, 'x'] if isinstance(value, (list, tuple)) else f'{value}x')
    assert H.rule_hash(constants=changed) != H.rule_hash()
