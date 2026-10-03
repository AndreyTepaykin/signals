"""The M4 vocabulary and thresholds are frozen into ``rule_hash``."""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from app.core import constants as C
from app.core import hashing as H
from app.models import enums as E

#: The hash at RULE_VERSION phase1-v3. If this changes, a frozen rule changed: bump the version on purpose.

M4_NAMES = (
    'TRIGGER_SWING_BREAK',
    'TRIGGER_PULLBACK_RESET',
    'TRIGGER_SQUEEZE',
    'TRIGGER_VWAP_TAG',
    'TRIGGER_PRIORITY',
    'TRIGGERS_BY_FAMILY',
    'SWING_BREAK_TRIGGER_FIRES_ON',
    'SETUP_FAMILIES',
    'SETUP_FAMILY_BY_TRIGGER',
    'DIRECTION_VOTE_Z_MIN',
    'DIRECTION_VOTE_MIN_NET',
    'VWAP_STRETCH_ATR',
    'STAND_DOWN_ON_UNKNOWN_EVENT_CALENDAR',
    'STAND_DOWN_ON_UNKNOWN_PERP_SPOT',
    'STATUS_REASON_PENDING_OPTIONS_CHECK',
    'STATUS_REASON_VOTE_CONFLICT',
    'STATUS_REASON_CVD_DIVERGENCE',
    'STATUS_REASON_STAND_DOWN_PREFIX',
    'STAND_DOWN_FEED_BREACH',
    'STAND_DOWN_EVENT_WINDOW',
    'STAND_DOWN_PERP_SPOT',
    'LOCKED_BOOK_HASH_BYTES',
    'LOCKED_BOOK_SELECTED_BUCKET',
)


def test_rule_version_is_phase1_v3():
    assert C.RULE_VERSION == 'phase1-v3'


def test_every_m4_constant_is_in_the_rule_hash_payload_and_serializable():
    frozen = H.frozen_constants()
    for name in M4_NAMES:
        assert name in frozen, name
    H.canonical_serialize(frozen)  # raises on an undefined type


def test_changing_any_m4_constant_changes_the_rule_hash():
    base = dict(H.frozen_constants())
    reference = H.rule_hash(base)
    swaps = {
        'DIRECTION_VOTE_Z_MIN': 1.5,
        'DIRECTION_VOTE_MIN_NET': 1,
        'VWAP_STRETCH_ATR': 0.5,
        'SWING_BREAK_TRIGGER_FIRES_ON': 'state',
        'STAND_DOWN_ON_UNKNOWN_EVENT_CALENDAR': True,
        'STAND_DOWN_ON_UNKNOWN_PERP_SPOT': False,
        'LOCKED_BOOK_SELECTED_BUCKET': 1,
        'LOCKED_BOOK_HASH_BYTES': 4,
        'SETUP_FAMILIES': (*C.SETUP_FAMILIES, 'extra'),
        'TRIGGER_PRIORITY': tuple(reversed(C.TRIGGER_PRIORITY)),
        'STATUS_REASON_PENDING_OPTIONS_CHECK': 'pending',
    }
    for name, value in swaps.items():
        assert H.rule_hash(dict(base, **{name: value})) != reference, name


def test_the_frozen_setup_family_vocabulary():
    assert C.SETUP_FAMILIES == ('breakout', 'pullback', 'squeeze', 'reversion')
    assert E.SETUP_FAMILIES is C.SETUP_FAMILIES
    assert set(C.SETUP_FAMILY_BY_TRIGGER) == set(E.TRIGGERS)
    assert set(C.SETUP_FAMILY_BY_TRIGGER.values()) == set(C.SETUP_FAMILIES)


def test_trigger_tables_match_the_trigger_vocabulary_in_the_schema():
    assert set(C.TRIGGER_PRIORITY) == set(E.TRIGGERS) and len(C.TRIGGER_PRIORITY) == len(E.TRIGGERS)
    assert set(C.TRIGGERS_BY_FAMILY) == set(C.FAMILIES)
    for family, triggers in C.TRIGGERS_BY_FAMILY.items():
        assert set(triggers) <= set(E.TRIGGERS), family


def test_only_that_family_may_fire_the_table():
    assert C.TRIGGERS_BY_FAMILY[C.FAMILY_TREND] == ('swing_break', 'pullback_reset', 'squeeze')
    assert C.TRIGGERS_BY_FAMILY[C.FAMILY_CHOP] == ('squeeze', 'vwap_tag')
    assert 'pullback_reset' not in C.TRIGGERS_BY_FAMILY[C.FAMILY_CHOP]  # spec: "Only while the efficiency family is trend"


def test_status_reason_codes_are_machine_readable_snake_case():
    for name in M4_NAMES:
        if name.startswith('STATUS_REASON_') and name != 'STATUS_REASON_STAND_DOWN_PREFIX':
            assert re.fullmatch(r'[a-z_]+', getattr(C, name)), name
    assert C.STATUS_REASON_STAND_DOWN_PREFIX == 'stand_down:'


def test_the_new_thresholds_have_the_documented_types():
    assert isinstance(C.DIRECTION_VOTE_Z_MIN, float) and isinstance(C.VWAP_STRETCH_ATR, float)
    assert isinstance(C.DIRECTION_VOTE_MIN_NET, int) and not isinstance(C.DIRECTION_VOTE_MIN_NET, bool)
    assert isinstance(C.LOCKED_BOOK_HASH_BYTES, int) and isinstance(C.LOCKED_BOOK_SELECTED_BUCKET, int)
    assert 0 <= C.LOCKED_BOOK_SELECTED_BUCKET < C.LOCKED_BOOK_ONE_IN
    assert C.DIRECTION_VOTE_Z_MIN == C.ALT_RESIDUAL_Z_GATE == 1.0
    assert not any(isinstance(getattr(C, n), Decimal) for n in M4_NAMES)


def test_rule_hash_is_a_stable_64_char_digest_and_is_pinned():
    value = H.rule_hash()
    assert re.fullmatch(r'[0-9a-f]{64}', value)
    assert value == H.rule_hash()
    assert value == PINNED_RULE_HASH, 'a frozen rule changed: bump RULE_VERSION deliberately and update the pin'


def test_engine_params_guard_still_covers_params_py():
    # the M3.1 guard: nothing in params.py may be missing from the payload (M4 added no engine parameter)
    from app.core.features import params as P

    public = {n for n, v in vars(P).items() if n.isupper() and not n.startswith('_') and not isinstance(v, type(C))}
    renames = {'FUNDING_MAX_AGE': 'FUNDING_MAX_AGE_SECONDS'}
    assert set(H.frozen_engine_params()) == {renames.get(n, n) for n in public}


def test_the_candidate_setup_family_column_uses_the_frozen_vocabulary():
    from app.models.decisions import Candidate

    text = ' '.join(str(c.sqltext) for c in Candidate.__table__.constraints if c.__class__.__name__ == 'CheckConstraint')
    for family in C.SETUP_FAMILIES:
        assert f"'{family}'" in text
    assert re.search(r'\bsetup_family IN \(', text)


@pytest.mark.parametrize('name', ['SWING_BREAK_TRIGGER_FIRES_ON'])
def test_the_swing_break_trigger_decision_is_recorded_as_a_constant(name):
    assert getattr(C, name) == 'event'


PINNED_RULE_HASH = 'eb32a9d4140c5c55691d74d8c278adcb83860e3d68cf42d07ad8b3a7a3923dff'
