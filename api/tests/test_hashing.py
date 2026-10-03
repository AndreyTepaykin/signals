import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import MappingProxyType, ModuleType

import numpy as np
import pytest

from app.core import constants as C
from app.core import hashing as H
from app.core.features import params as P
from app.core.features.params import HistoryPolicy


# --- feature_hash ---------------------------------------------------------
def test_key_order_does_not_matter():
    a = {'z': 1.5, 'a': None, 'm': {'y': 1, 'x': [1, 2]}}
    b = {'m': {'x': [1, 2], 'y': 1}, 'a': None, 'z': 1.5}
    assert H.feature_hash(a) == H.feature_hash(b)


def test_changed_value_changes_hash():
    assert H.feature_hash({'a': 1.0}) != H.feature_hash({'a': 1.0000000000000002})
    assert H.feature_hash({'a': 1}) != H.feature_hash({'a': 2})


def test_list_order_matters():
    assert H.feature_hash({'a': [1, 2]}) != H.feature_hash({'a': [2, 1]})


def test_digest_shape_is_sha256_hex():
    assert re.fullmatch(r'[0-9a-f]{64}', H.feature_hash({}))


def test_golden_digest_is_stable():
    # Pins the canonical form. If this changes, every stored feature_hash would
    # stop being reproducible: do NOT update it casually.
    features = {'atr': 1.25, 'family': 'trend', 'flag': True, 'miss': None, 'n': 3, 'px': Decimal('4.50')}
    assert H.canonical_serialize(features) == (
        '{"atr":1.25,"family":"trend","flag":true,"miss":null,"n":3,"px":D"4.5"}'
    )
    assert H.feature_hash(features) == H.feature_hash(dict(reversed(list(features.items()))))


def test_float_text_is_single_canonical_form():
    assert H.canonical_serialize(0.1) == H.canonical_serialize(0.10) == '0.1'
    assert H.canonical_serialize(float('0.1000000000000000055511151231257827')) == '0.1'
    assert H.canonical_serialize(-0.0) == H.canonical_serialize(0.0) == '0.0'
    assert H.canonical_serialize(1e-7) == '1e-07'
    assert H.canonical_serialize(float('nan')) == 'nan'
    assert H.canonical_serialize(float('inf')) == 'inf'
    assert H.canonical_serialize(float('-inf')) == '-inf'


def test_float_that_round_trips_differently_is_a_different_value():
    assert H.feature_hash({'a': 0.1 + 0.2}) != H.feature_hash({'a': 0.3})


def test_numpy_scalars_hash_like_python_scalars():
    assert H.feature_hash({'a': np.float64(0.1), 'b': np.int64(3), 'c': np.bool_(True)}) == H.feature_hash(
        {'a': 0.1, 'b': 3, 'c': True}
    )


def test_decimal_is_normalized_and_distinct_from_float_and_int():
    assert H.canonical_serialize(Decimal('1.50')) == H.canonical_serialize(Decimal('1.5')) == 'D"1.5"'
    assert H.canonical_serialize(Decimal('100')) == 'D"100"'
    assert H.canonical_serialize(Decimal('0.00')) == H.canonical_serialize(Decimal('-0')) == 'D"0"'
    assert len({H.canonical_serialize(1), H.canonical_serialize(1.0), H.canonical_serialize(Decimal(1))}) == 3
    with pytest.raises(TypeError):
        H.canonical_serialize(Decimal('NaN'))


def test_bool_is_not_confused_with_int():
    assert H.canonical_serialize(True) == 'true'
    assert H.canonical_serialize(1) == '1'
    assert H.feature_hash({'a': True}) != H.feature_hash({'a': 1})


def test_none_and_nested_values():
    a = {'a': None, 'b': [{'c': Decimal('2.0'), 'd': (1, 2)}], 'e': MappingProxyType({'k': False})}
    b = {'e': {'k': False}, 'b': [{'d': [1, 2], 'c': Decimal('2')}], 'a': None}
    assert H.feature_hash(a) == H.feature_hash(b)


def test_strings_are_escaped_unambiguously():
    assert H.feature_hash({'a': 'x"y'}) != H.feature_hash({'a': 'x', 'y': ''})
    assert H.canonical_serialize('é') == '"\\u00e9"'


def test_datetimes_must_be_aware_and_are_normalized_to_utc():
    plus3 = timezone(timedelta(hours=3))
    a = datetime(2026, 1, 14, 13, 0, tzinfo=timezone.utc)
    b = datetime(2026, 1, 14, 16, 0, tzinfo=plus3)
    assert H.canonical_serialize(a) == H.canonical_serialize(b)
    assert H.canonical_serialize(date(2026, 1, 14)) == 'd"2026-01-14"'
    with pytest.raises(ValueError):
        H.canonical_serialize(datetime(2026, 1, 14, 13, 0))


@pytest.mark.parametrize('bad', [{1, 2}, frozenset({1}), object(), b'bytes', 1 + 2j, lambda: 0])
def test_unsupported_types_raise(bad):
    with pytest.raises(TypeError):
        H.feature_hash({'a': bad})


def test_non_string_keys_and_non_mapping_raise():
    with pytest.raises(TypeError):
        H.feature_hash({1: 'a'})
    with pytest.raises(TypeError):
        H.feature_hash([('a', 1)])  # type: ignore[arg-type]


# --- rule_hash ------------------------------------------------------------
def test_every_frozen_constant_is_serializable():
    consts = H.frozen_constants()
    assert consts  # non-empty
    H.canonical_serialize(consts)  # raises TypeError if any value type is undefined
    assert 'RULE_VERSION' not in consts
    assert all(name.isupper() and not name.startswith('_') for name in consts)


def test_rule_hash_is_deterministic():
    assert H.rule_hash() == H.rule_hash()
    assert re.fullmatch(r'[0-9a-f]{64}', H.rule_hash())


def test_rule_hash_changes_when_a_constant_changes():
    base = dict(H.frozen_constants())
    changed = dict(base, CHANDELIER_ATR_MULT=3.5)
    assert H.rule_hash(base) != H.rule_hash(changed)
    changed = dict(base, TAKER_FEE_BPS_PER_SIDE=Decimal('5'))
    assert H.rule_hash(base) != H.rule_hash(changed)
    assert H.rule_hash(base) == H.rule_hash()  # unchanged input -> unchanged hash


def test_rule_hash_changes_when_a_constant_is_added_or_removed():
    base = dict(H.frozen_constants())
    assert H.rule_hash(base) != H.rule_hash(dict(base, NEW_THRESHOLD=1))
    base.pop('ALT_RESIDUAL_Z_GATE')
    assert H.rule_hash(base) != H.rule_hash()


def test_rule_hash_changes_with_rule_version():
    assert H.rule_hash(rule_version='phase1-v1') != H.rule_hash(rule_version='phase1-v2')
    assert H.rule_hash() == H.rule_hash(rule_version=C.RULE_VERSION)


def test_rule_hash_ignores_key_order_of_constants():
    base = H.frozen_constants()
    assert H.rule_hash(base) == H.rule_hash(dict(reversed(list(base.items()))))


# --- engine params in the rule hash ----------------------------------------
# params.py names stored under a different payload key because their value is converted.
ENGINE_PARAM_RENAMES = {'FUNDING_MAX_AGE': 'FUNDING_MAX_AGE_SECONDS'}


def _public_params_names():
    return {
        name
        for name, value in vars(P).items()
        if name.isupper() and not name.startswith('_') and not isinstance(value, ModuleType)
    }


def test_every_public_engine_param_is_in_the_rule_hash_payload():
    expected = {ENGINE_PARAM_RENAMES.get(name, name) for name in _public_params_names()}
    assert set(H.frozen_engine_params()) == expected


def test_the_guard_sees_a_new_public_name_in_params(monkeypatch):
    monkeypatch.setattr(P, 'NEW_ENGINE_CHOICE', 7, raising=False)
    assert 'NEW_ENGINE_CHOICE' in _public_params_names()
    assert 'NEW_ENGINE_CHOICE' not in H.frozen_engine_params()


def test_engine_params_are_serialized_with_defined_types():
    params = H.frozen_engine_params()
    H.canonical_serialize(params)
    assert params['FUNDING_MAX_AGE_SECONDS'] == 16 * 3600 and type(params['FUNDING_MAX_AGE_SECONDS']) is int
    assert params['PRODUCTION_POLICY'] == {'window_days': 90, 'min_span_days': 7.0, 'min_samples': 30}
    assert 'FUNDING_MAX_AGE' not in params


def test_engine_params_sit_under_their_own_key_beside_the_constants():
    text = H.rule_hash_input()
    assert text.startswith('{"constants":{') and ',"engine_params":{' in text and ',"rule_version":' in text
    assert set(H.frozen_engine_params()).isdisjoint(H.frozen_constants())


def test_rule_hash_changes_when_an_engine_param_changes(monkeypatch):
    before = H.rule_hash()
    monkeypatch.setattr(P, 'ATR_HISTORY_BARS', P.ATR_HISTORY_BARS + 1)
    assert H.rule_hash() != before
    monkeypatch.undo()
    assert H.rule_hash() == before
    monkeypatch.setattr(P, 'PRODUCTION_POLICY', HistoryPolicy(min_samples=31))
    assert H.rule_hash() != before
    monkeypatch.undo()
    monkeypatch.setattr(P, 'FUNDING_MAX_AGE', timedelta(hours=17))
    assert H.rule_hash() != before


def test_a_fractional_second_funding_age_is_refused(monkeypatch):
    monkeypatch.setattr(P, 'FUNDING_MAX_AGE', timedelta(hours=16, microseconds=1))
    with pytest.raises(ValueError):
        H.frozen_engine_params()


def test_rule_version_is_phase1_v3():
    assert C.RULE_VERSION == 'phase1-v3'


# --- code_version ---------------------------------------------------------
def test_code_version_shape():
    value = H.code_version()
    assert re.fullmatch(r'[0-9a-f]{4,40}(-dirty)?|unknown', value)


def test_code_version_degrades_to_sentinel_without_git(monkeypatch):
    def boom(*args, **kwargs):
        raise FileNotFoundError('git')

    monkeypatch.setattr(H.subprocess, 'run', boom)
    assert H.code_version() == H.CODE_VERSION_UNKNOWN


def test_code_version_dirty_marker(monkeypatch):
    outputs = {'rev-parse': 'abc1234\n', 'status': ' M file.py\n'}
    monkeypatch.setattr(H, '_git', lambda *args: outputs[args[0]])
    assert H.code_version() == 'abc1234-dirty'
    outputs['status'] = ''
    assert H.code_version() == 'abc1234'
