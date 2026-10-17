"""ElderParams: validation, dict round trip, canonical hash."""

import json

import pytest

from app.research.elder import params as params_module
from app.research.elder.params import ElderParams


def base(**overrides):
    values = {
        'tide_mode': 'ema',
        'wave_osc': 'force2',
        'wave_threshold': 0.3,
        'wave_block_impulse': True,
        'ripple_expiry_bars': 8,
        'stop_lookback_bars': 3,
        'stop_atr_buffer': 0.5,
        'min_stop_atr': 0.8,
        't1_r': 1.5,
        't1_share': 0.5,
        'be_after_t1': True,
        'trail_atr': 3.0,
    }
    values.update(overrides)
    return values


def test_fixed_constants():
    assert params_module.T2_R == 10.0
    assert params_module.EMA_PERIOD == 13
    assert (params_module.MACD_FAST, params_module.MACD_SLOW, params_module.MACD_SIGNAL) == (12, 26, 9)
    assert params_module.ATR_PERIOD == 14
    assert params_module.STOCH_PERIOD == 5
    assert params_module.STOCH_SMOOTH == 3
    assert params_module.FORCE_PERIOD == 2
    assert params_module.VOLUME_SMA == 20
    assert params_module.COST_ROUND_TRIP == 0.0012
    assert params_module.MAX_COST_R == 0.1


def test_from_dict_round_trip():
    values = base()
    parsed = ElderParams.from_dict(values)
    assert parsed.to_dict() == values
    assert ElderParams.from_dict(parsed.to_dict()) == parsed


def test_json_round_trip():
    parsed = ElderParams.from_dict(base())
    assert ElderParams.from_dict(json.loads(json.dumps(parsed.to_dict()))) == parsed


def test_frozen():
    parsed = ElderParams.from_dict(base())
    with pytest.raises(Exception):
        parsed.trail_atr = 1.0  # type: ignore[misc]


def test_unknown_key_named():
    with pytest.raises(ValueError, match='bogus'):
        ElderParams.from_dict(base(bogus=1))


def test_missing_key_named():
    values = base()
    del values['trail_atr']
    del values['t1_r']
    with pytest.raises(ValueError, match='trail_atr') as info:
        ElderParams.from_dict(values)
    assert 't1_r' in str(info.value)


def test_ints_become_floats_so_hash_is_stable():
    a = ElderParams.from_dict(base(t1_r=1, trail_atr=3, t1_share=1))
    b = ElderParams.from_dict(base(t1_r=1.0, trail_atr=3.0, t1_share=1.0))
    assert a == b
    assert isinstance(a.t1_r, float)
    assert a.params_hash() == b.params_hash()


@pytest.mark.parametrize('field,value', [('tide_mode', 'weekly'), ('wave_osc', 'rsi')])
def test_enum_errors(field, value):
    with pytest.raises(ValueError, match=field):
        ElderParams(**base(**{field: value}))


@pytest.mark.parametrize('mode', ['ema', 'macd', 'both'])
@pytest.mark.parametrize('osc', ['force2', 'stoch', 'elder_ray'])
def test_every_enum_value_accepted(mode, osc):
    ElderParams(**base(tide_mode=mode, wave_osc=osc, wave_threshold=10.0))


@pytest.mark.parametrize(
    'field,value',
    [
        ('tide_mode', 1),
        ('wave_block_impulse', 1),
        ('be_after_t1', 'yes'),
        ('ripple_expiry_bars', 2.0),
        ('ripple_expiry_bars', True),
        ('stop_lookback_bars', '3'),
        ('wave_threshold', '0.3'),
        ('wave_threshold', None),
        ('trail_atr', True),
    ],
)
def test_type_errors(field, value):
    with pytest.raises(ValueError, match=field):
        ElderParams(**base(**{field: value}))


@pytest.mark.parametrize(
    'field,value',
    [
        ('ripple_expiry_bars', 0),
        ('stop_lookback_bars', 0),
        ('ripple_expiry_bars', -3),
        ('t1_r', 0.99),
        ('t1_r', 2.01),
        ('t1_share', -0.01),
        ('t1_share', 1.01),
        ('wave_threshold', -0.1),
        ('stop_atr_buffer', -1.0),
        ('min_stop_atr', -0.1),
        ('trail_atr', -2.0),
        ('trail_atr', float('nan')),
        ('stop_atr_buffer', float('inf')),
    ],
)
def test_sanity_errors(field, value):
    with pytest.raises(ValueError, match=field):
        ElderParams(**base(**{field: value}))


def test_boundaries_accepted():
    ElderParams(**base(t1_r=1.0, t1_share=0.0, wave_threshold=0.0, ripple_expiry_bars=1))
    ElderParams(**base(t1_r=2.0, t1_share=1.0, stop_lookback_bars=1))


def test_stoch_threshold_range():
    ElderParams(**base(wave_osc='stoch', wave_threshold=0.0))
    ElderParams(**base(wave_osc='stoch', wave_threshold=100.0))
    with pytest.raises(ValueError, match='wave_threshold'):
        ElderParams(**base(wave_osc='stoch', wave_threshold=100.5))
    # other oscillators have other units
    ElderParams(**base(wave_osc='force2', wave_threshold=250.0))


def test_hash_stable_under_key_order_and_changes_with_value():
    values = base()
    shuffled = dict(reversed(list(values.items())))
    first = ElderParams.from_dict(values)
    second = ElderParams.from_dict(shuffled)
    assert first.params_hash() == second.params_hash()
    assert len(first.params_hash()) == 64
    changed = ElderParams.from_dict(base(trail_atr=3.5))
    assert changed.params_hash() != first.params_hash()
    flipped = ElderParams.from_dict(base(be_after_t1=False))
    assert flipped.params_hash() != first.params_hash()


def test_hash_is_sha256_of_canonical_json():
    import hashlib

    parsed = ElderParams.from_dict(base())
    canonical = json.dumps(parsed.to_dict(), sort_keys=True, separators=(',', ':'))
    assert parsed.params_hash() == hashlib.sha256(canonical.encode()).hexdigest()
