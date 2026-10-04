"""Guards for the Milestone 5 constants: they are frozen into rule_hash, serializable, and the spec's numbers are untouched."""

from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal

import pytest

from app.core import constants as C
from app.core import hashing as H
from app.models import enums as E

M5_NAMES = (
    'OPTIONS_CURRENCY_BY_COIN',
    'OPTIONS_NEAR_DATED_HOURS',
    'LARGE_EXPIRY_MIN_OI_SHARE',
    'OPTIONS_IGNORE_BLOCKS_ALERT_ON_COVERED_COINS',
    'OPTIONS_GAMMA_PROXY',
    'OPTIONS_GAMMA_SCAN_RANGE',
    'OPTIONS_GAMMA_SCAN_STEP',
    'OPTIONS_SKEW_DELTA',
    'OPTIONS_SKEW_DELTA_TOLERANCE',
    'OPTIONS_RV_MAX_BARS',
    'OPTIONS_PERCENTILE_SAMPLE_MINUTES',
    'OPTIONS_CONFIDENCE_FULL_AT_FLOOR_MULTIPLE',
    'OPTIONS_INFLUENCE_BY_VERDICT',
    'STATUS_REASON_OPTIONS_IGNORED_PREFIX',
    'OPTIONS_IGNORE_NO_BOOK_FOR_COIN',
    'OPTIONS_IGNORE_NO_SNAPSHOT',
    'OPTIONS_IGNORE_NO_HISTORY',
    'OPTIONS_IGNORE_SNAPSHOT_TOO_OLD',
    'OPTIONS_IGNORE_OI_FLOOR_UNSET',
    'OPTIONS_IGNORE_OI_BELOW_FLOOR',
    'OPTIONS_IGNORE_METRIC_INCOMPLETE',
    'STATUS_REASON_OPTIONS_EXPIRY_PIN',
    'STATUS_REASON_OPTIONS_HEADWIND_ROOM',
)

#: The numbers the spec states. Milestone 5 reads them and changes none.
SPEC_NUMBERS = {
    'OPTIONS_SNAPSHOT_MAX_AGE_MINUTES': 15,
    'EXPIRY_PIN_WINDOW_HOURS': 48,
    'EXPIRY_PIN_MIN_STRIKE_OI_SHARE': 0.15,
    'HEADWIND_MIN_STRIKE_OI_SHARE': 0.10,
    'HEADWIND_MIN_ROOM_R': 0.5,
    'AMPLIFY_FLIP_MAX_ATR': 1.0,
    'AMPLIFY_STOP_WIDEN_ATR': 0.5,
    'AMPLIFY_SIZE_MULTIPLIER': 1.15,
    'DAMPEN_SIZE_MULTIPLIER': 0.70,
    'DAMPEN_TARGET_CAP_R': 1.0,
    'ALIGN_SIZE_MULTIPLIER': 1.00,
    'VOL_RICH_QUANTILE': 0.80,
    'VOL_CHEAP_QUANTILE': 0.20,
}


def test_the_spec_numbers_are_unchanged():
    for name, value in SPEC_NUMBERS.items():
        assert getattr(C, name) == value, name
    assert C.OPTIONS_VERDICT_PRECEDENCE == ('ignore', 'expiry_pin', 'headwind', 'amplify', 'dampen', 'align')


def test_the_oi_floor_holds_the_owner_set_numbers():
    assert dict(C.OPTIONS_OI_FLOOR_BY_COIN) == {'BTCUSDT': 100_000.0, 'ETHUSDT': 400_000.0}


def test_every_m5_constant_is_in_the_rule_hash_payload_and_serializable():
    frozen = H.frozen_constants()
    for name in M5_NAMES + tuple(SPEC_NUMBERS) + ('OPTIONS_OI_FLOOR_BY_COIN',):
        assert name in frozen, name
    H.canonical_serialize(frozen)


def test_changing_any_m5_constant_changes_the_rule_hash():
    base = dict(H.frozen_constants())
    reference = H.rule_hash(base)
    swaps = {
        'OPTIONS_CURRENCY_BY_COIN': {'BTCUSDT': 'BTC'},
        'OPTIONS_NEAR_DATED_HOURS': 48,
        'LARGE_EXPIRY_MIN_OI_SHARE': 0.10,
        'OPTIONS_IGNORE_BLOCKS_ALERT_ON_COVERED_COINS': False,
        'OPTIONS_GAMMA_PROXY': 'another_proxy',
        'OPTIONS_GAMMA_SCAN_RANGE': 0.20,
        'OPTIONS_GAMMA_SCAN_STEP': 0.01,
        'OPTIONS_SKEW_DELTA': 0.20,
        'OPTIONS_SKEW_DELTA_TOLERANCE': 0.05,
        'OPTIONS_RV_MAX_BARS': 360,
        'OPTIONS_PERCENTILE_SAMPLE_MINUTES': 30,
        'OPTIONS_CONFIDENCE_FULL_AT_FLOOR_MULTIPLE': 3.0,
        'OPTIONS_INFLUENCE_BY_VERDICT': {**C.OPTIONS_INFLUENCE_BY_VERDICT, 'align': 1.0},
        'OPTIONS_OI_FLOOR_BY_COIN': {'BTCUSDT': 100},
        'OPTIONS_IGNORE_OI_FLOOR_UNSET': 'floor_missing',
        'STATUS_REASON_OPTIONS_EXPIRY_PIN': 'options:pin',
    }
    assert set(swaps) <= set(M5_NAMES) | {'OPTIONS_OI_FLOOR_BY_COIN'}
    for name, value in swaps.items():
        assert H.rule_hash(dict(base, **{name: value})) != reference, name


def test_operating_parameters_are_configuration_and_never_in_the_rule_hash():
    frozen = H.frozen_constants()
    assert not [name for name in frozen if 'DERIBIT' in name or 'PUBLICATION_LAG' in name or 'POLL' in name]


def test_the_influence_of_each_verdict_is_its_sign_and_covers_every_verdict():
    assert set(C.OPTIONS_INFLUENCE_BY_VERDICT) == set(C.OPTIONS_VERDICT_PRECEDENCE)
    assert dict(C.OPTIONS_INFLUENCE_BY_VERDICT) == {'ignore': 0, 'expiry_pin': -1, 'headwind': -1, 'amplify': 1, 'dampen': -1, 'align': 0}
    assert all(-1 <= v <= 1 for v in C.OPTIONS_INFLUENCE_BY_VERDICT.values())


def test_the_types_are_the_documented_ones():
    for name in ('LARGE_EXPIRY_MIN_OI_SHARE', 'OPTIONS_GAMMA_SCAN_RANGE', 'OPTIONS_GAMMA_SCAN_STEP', 'OPTIONS_SKEW_DELTA', 'OPTIONS_SKEW_DELTA_TOLERANCE', 'OPTIONS_CONFIDENCE_FULL_AT_FLOOR_MULTIPLE'):
        assert isinstance(getattr(C, name), float), name
    for name in ('OPTIONS_NEAR_DATED_HOURS', 'OPTIONS_RV_MAX_BARS', 'OPTIONS_PERCENTILE_SAMPLE_MINUTES'):
        assert isinstance(getattr(C, name), int) and not isinstance(getattr(C, name), bool), name
    assert not any(isinstance(getattr(C, n), (Decimal, timedelta)) for n in M5_NAMES)


def test_the_currency_map_covers_exactly_the_coins_deribit_lists():
    assert dict(C.OPTIONS_CURRENCY_BY_COIN) == {'BTCUSDT': 'BTC', 'ETHUSDT': 'ETH'}
    assert C.OPTIONS_NEAR_DATED_HOURS == C.LONGEST_HORIZON_HOURS == 72


def test_status_reason_codes_are_machine_readable():
    for name in M5_NAMES:
        if name.startswith('OPTIONS_IGNORE_') and isinstance(getattr(C, name), str):
            assert re.fullmatch(r'[a-z_]+', getattr(C, name)), name
    assert C.STATUS_REASON_OPTIONS_IGNORED_PREFIX == 'options_ignored:'
    assert re.fullmatch(r'options:[a-z_]+', C.STATUS_REASON_OPTIONS_EXPIRY_PIN)
    assert re.fullmatch(r'options:[a-z_]+', C.STATUS_REASON_OPTIONS_HEADWIND_ROOM)


def test_every_status_the_check_can_write_needs_a_reason_the_schema_knows_about():
    for status in ('delayed', 'cancelled', 'watch'):
        assert status in E.STATUSES_REQUIRING_REASON
    assert E.STATUS_ALERT not in E.STATUSES_REQUIRING_REASON


def test_the_pending_marker_check_is_on_the_candidate_table():
    from app.models.decisions import Candidate

    names = {c.name for c in Candidate.__table__.constraints if getattr(c, 'name', None)}
    assert any('pending_has_no_verdict' in str(n) for n in names)


def test_the_migration_creates_that_check_and_is_reversible():
    from pathlib import Path

    text = next((Path(__file__).resolve().parents[1] / 'alembic' / 'versions').glob('*c52e9b8a4f17*.py')).read_text(encoding='utf-8')
    assert "down_revision: Union[str, None] = 'a41f7c05d2e9'" in text
    assert 'ck_candidate_pending_has_no_verdict' in text and text.count('ck_candidate_pending_has_no_verdict') >= 2  # created and dropped
    assert 'def downgrade' in text and 'drop_constraint' in text


def test_the_empty_floor_is_not_a_way_to_say_no_floor_needed():
    with pytest.raises(TypeError):
        C.OPTIONS_OI_FLOOR_BY_COIN['BTCUSDT'] = 1  # type: ignore[index]  # read-only: a floor is configured in constants.py, deliberately
