"""``feature_hash`` determinism.

The hash is the audit trail: the same inputs must give the same hash on every
run, in every process, with any dict or list ordering, with no wall clock and
no hidden state. And it must be SENSITIVE: a different value, or NULL where there
was 0.0, must hash differently.
"""

from __future__ import annotations

import ast
import os
import random
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from app.core import constants as C
from app.core.features import columns, levels
from app.core.features.assemble import compute_features
from app.core.features.series import FundingPrint
from app.core.hashing import feature_hash

from feature_helpers import H4, dt, synthetic_inputs, truncate_inputs

API_ROOT = Path(__file__).resolve().parents[1]
FEATURES_DIR = API_ROOT / 'app' / 'core' / 'features'


@pytest.fixture(scope='module')
def market():
    return synthetic_inputs(with_alt=True, end=dt(2026, 6, 1, 8, 0))


@pytest.fixture(scope='module')
def baseline(market):
    return compute_features(market)


def test_repeated_computation_gives_the_same_values_and_hash(market, baseline):
    again = compute_features(market)
    third = compute_features(replace(market))
    assert again.values == baseline.values and third.values == baseline.values
    assert again.feature_hash == baseline.feature_hash == third.feature_hash
    assert len(baseline.feature_hash) == 64


def test_the_hash_is_of_exactly_the_stored_mapping(baseline):
    assert set(baseline.values) == set(columns.FEATURE_COLUMNS)
    assert feature_hash(baseline.values) == baseline.feature_hash


def test_dict_key_insertion_order_does_not_matter(baseline):
    reversed_order = dict(reversed(list(baseline.values.items())))
    shuffled = list(baseline.values.items())
    random.Random(3).shuffle(shuffled)
    assert list(reversed_order) != list(baseline.values)
    assert feature_hash(reversed_order) == baseline.feature_hash
    assert feature_hash(dict(shuffled)) == baseline.feature_hash
    # nested mapping too (shadow_metrics)
    nested = {**baseline.values, 'shadow_metrics': {'b': 1.0, 'a': 2.0}}
    nested_reordered = {**baseline.values, 'shadow_metrics': {'a': 2.0, 'b': 1.0}}
    assert feature_hash(nested) == feature_hash(nested_reordered)


def test_input_row_order_does_not_matter(market, baseline):
    rng = random.Random(11)

    def shuffled(seq):
        seq = list(seq)
        rng.shuffle(seq)
        return seq

    scrambled = replace(
        market,
        perp_15m=shuffled(market.perp_15m),
        spot_15m=shuffled(market.spot_15m),
        perp_1h=shuffled(market.perp_1h),
        spot_1h=shuffled(market.spot_1h),
        perp_4h=shuffled(market.perp_4h),
        btc_perp_15m=shuffled(market.btc_perp_15m),
        oi_1h=shuffled(market.oi_1h),
        funding_settled=shuffled(market.funding_settled),
    )
    assert compute_features(scrambled).feature_hash == baseline.feature_hash


def test_a_different_process_with_other_hash_seed_and_timezone_gives_the_same_hash(baseline):
    script = (
        'import sys; sys.path.insert(0, "tests");'
        'from feature_helpers import synthetic_inputs, dt;'
        'from app.core.features.assemble import compute_features;'
        'print(compute_features(synthetic_inputs(with_alt=True, end=dt(2026,6,1,8,0))).feature_hash)'
    )
    seen = []
    for seed, tz in (('1', 'Asia/Tokyo'), ('12345', 'America/Los_Angeles')):
        env = {**os.environ, 'PYTHONHASHSEED': seed, 'TZ': tz, 'PYTHONPATH': str(API_ROOT)}
        out = subprocess.run([sys.executable, '-c', script], cwd=API_ROOT, env=env, capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr
        seen.append(out.stdout.strip())
    assert seen == [baseline.feature_hash, baseline.feature_hash]


def test_no_wall_clock_randomness_or_environment_in_the_engine(monkeypatch, market, baseline):
    """The engine never asks what time it is: patch every clock to explode and recompute."""
    import time

    import app.core.time as time_module

    def boom(*_a, **_k):
        raise AssertionError('the feature engine read the wall clock')

    monkeypatch.setattr(time_module, 'utc_now', boom)
    for name in ('time', 'time_ns'):
        monkeypatch.setattr(time, name, boom)
    assert compute_features(market).feature_hash == baseline.feature_hash


FORBIDDEN_CALLS = {
    ('datetime', 'now'),
    ('datetime', 'utcnow'),
    ('datetime', 'today'),
    ('date', 'today'),
    ('time', 'time'),
    ('time', 'time_ns'),
    ('time', 'monotonic'),
    ('time', 'perf_counter'),
    ('os', 'environ'),
    ('os', 'getenv'),
    ('random', 'random'),
    ('random', 'seed'),
    ('uuid', 'uuid4'),
}


def _engine_sources():
    return sorted(FEATURES_DIR.glob('*.py'))


def _offences(label: str, source: str) -> list[str]:
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if (node.value.id, node.attr) in FORBIDDEN_CALLS:
                offenders.append(f'{label}:{node.lineno} {node.value.id}.{node.attr}')
        if isinstance(node, ast.Name) and node.id == 'utc_now':
            offenders.append(f'{label}:{node.lineno} utc_now')
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, 'module', None) or '']
            if any(n in ('random', 'uuid', 'secrets') for n in names):
                offenders.append(f'{label}:{node.lineno} imports {names}')
    return offenders


def test_source_scan_the_engine_has_no_clock_random_or_environment_reads():
    offenders = []
    for path in _engine_sources():
        offenders += _offences(path.name, path.read_text(encoding='utf-8'))
    assert offenders == []


def test_the_source_scan_would_catch_a_clock_read():
    """Negative control: the scanner is not vacuous."""
    assert _offences('x', 'from datetime import datetime\nx = datetime.now()') != []
    assert _offences('x', 'import time\nx = time.time()') != []
    assert _offences('x', 'from app.core.time import utc_now\nx = utc_now()') != []
    assert _offences('x', 'import random\n') != []


# ---------------------------------------------------------------------------
# Sensitivity: the hash must move when the facts move
# ---------------------------------------------------------------------------
def test_one_changed_value_changes_the_hash(baseline):
    changed = {**baseline.values, 'rsi': baseline.values['rsi'] + 0.5}
    assert feature_hash(changed) != baseline.feature_hash


def test_null_and_zero_hash_differently(baseline):
    as_null = {**baseline.values, 'cvd_delta': None}
    as_zero = {**baseline.values, 'cvd_delta': 0.0}
    assert feature_hash(as_null) != feature_hash(as_zero)


def test_different_data_gives_a_different_hash(market, baseline):
    nudged = replace(market, perp_15m=[replace(b, close=b.close * 1.0005, high=max(b.high, b.close * 1.0005)) if b.close_time == market.bar_close else b for b in market.perp_15m])
    assert compute_features(nudged).feature_hash != baseline.feature_hash


def test_float_noise_below_the_column_scale_does_not_change_the_hash():
    a = columns.normalise_value('rsi', 0.1 + 0.2)  # 0.30000000000000004
    b = columns.normalise_value('rsi', 0.3)
    assert a == b == 0.3 and repr(a) == repr(b)
    assert columns.normalise_value('rsi', 55) == 55.0 and isinstance(columns.normalise_value('rsi', 55), float)
    assert columns.normalise_value('rsi', -0.0) == 0.0 and repr(columns.normalise_value('rsi', -1e-12)) == '0.0'
    keys = {name: None for name in columns.FEATURE_COLUMNS}
    keys['shadow_metrics'] = {}
    one = {**keys, 'rsi': columns.normalise_value('rsi', 0.1 + 0.2)}
    two = {**keys, 'rsi': columns.normalise_value('rsi', 0.3)}
    assert feature_hash(one) == feature_hash(two)


# ---------------------------------------------------------------------------
# What may and may not change the hash across source and horizon
# ---------------------------------------------------------------------------
def test_replay_hash_is_the_same_for_every_horizon_and_ignores_a_predicted_print(market, baseline):
    replay_hashes = {h: compute_features(replace(market, horizon=h)).feature_hash for h in C.HORIZONS}
    assert set(replay_hashes.values()) == {baseline.feature_hash}
    stamp = market.bar_close + timedelta(seconds=20)
    with_predicted = replace(market, funding_predicted=FundingPrint(stamp, stamp, 0.0002, 8))
    assert compute_features(with_predicted).feature_hash == baseline.feature_hash  # replay: not point-in-time, ignored


SWING_COLUMNS = ('swing_break_state', 'swing_break_event', 'swing_break_direction', 'chandelier_stop_price', 'chandelier_stop_distance_atr')


def _first_swing_break_event(market):
    t = dt(2026, 5, 10)
    while t < dt(2026, 5, 31):
        state, _ = levels.swing_state(market.perp_4h, t, H4)
        if state is not None and levels.swing_break_event(state, t):
            return t
        t += H4
    raise AssertionError('the synthetic market has no swing-break event: pick another seed')


def test_swing_break_is_stateless_cold_start_equals_warm_start_and_live_equals_replay(market):
    """No memory between bars: a process that started 40 days later, or a live row, sees the same swing break."""
    t = _first_swing_break_event(market)
    for bar_close in (t, t + timedelta(minutes=15), t + timedelta(hours=4)):
        warm = truncate_inputs(market, bar_close)
        cutoff = bar_close - timedelta(days=40)  # still more than SWING_HISTORY_BARS and ATR_HISTORY_BARS of 4h bars
        cold = replace(
            warm,
            perp_15m=[b for b in warm.perp_15m if b.open_time >= cutoff],
            spot_15m=[b for b in warm.spot_15m if b.open_time >= cutoff],
            perp_1h=[b for b in warm.perp_1h if b.open_time >= cutoff],
            spot_1h=[b for b in warm.spot_1h if b.open_time >= cutoff],
            perp_4h=[b for b in warm.perp_4h if b.open_time >= cutoff],
        )
        replay = compute_features(warm).values
        assert replay['swing_break_event'] is (bar_close == t)
        assert {n: compute_features(cold).values[n] for n in SWING_COLUMNS} == {n: replay[n] for n in SWING_COLUMNS}
        live = compute_features(replace(warm, source='live')).values
        assert {n: live[n] for n in SWING_COLUMNS} == {n: replay[n] for n in SWING_COLUMNS}


def test_swing_break_event_and_state_are_in_the_hash(baseline):
    for name in ('swing_break_event', 'swing_break_state'):
        flipped = {**baseline.values, name: not baseline.values[name]}
        assert feature_hash(flipped) != baseline.feature_hash
        assert feature_hash({**baseline.values, name: None}) != baseline.feature_hash


def test_live_carry_band_depends_on_the_horizon_and_is_the_hand_value(market):
    stamp = market.bar_close + timedelta(seconds=20)
    predicted = FundingPrint(stamp, stamp, 0.0001, 8)  # 1 bp per 8h
    live = replace(market, source='live', funding_predicted=predicted)
    by_horizon = {h: compute_features(replace(live, horizon=h)) for h in C.HORIZONS}
    # 1 bp per 8h * (hours / 8) : 4h -> 0.5 bp, 24h -> 3 bp, 72h -> 9 bp
    assert by_horizon['scalp'].values['funding_carry_bps_horizon'] == pytest.approx(0.5)
    assert by_horizon['swing'].values['funding_carry_bps_horizon'] == pytest.approx(3.0)
    assert by_horizon['position'].values['funding_carry_bps_horizon'] == pytest.approx(9.0)
    assert all(c.values['funding_rate_current'] == 0.0001 for c in by_horizon.values())
    assert len({c.feature_hash for c in by_horizon.values()}) == 3
