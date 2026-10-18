"""CLI: append-only trials.jsonl, param bounds, freeze, and the holdout lock.

The data source is monkeypatched. These tests never open the database and never
ask the train command for a bar after 2026-07-01.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from app.research.elder.load import COINS, TRAIN_END, CoinData
from app.research.elder.params import ElderParams
from app.research.elder.run import (
    SPACE_PATH,
    load_params,
    main,
    validate_params,
)

UTC = timezone.utc
DEFAULT = SPACE_PATH.with_name('params_default.json')


def _coins():
    return [CoinData(coin, [], [], []) for coin in COINS]


def _install(monkeypatch, seen):
    def fake(period_end):
        seen.append(period_end)
        return _coins()

    monkeypatch.setattr('app.research.elder.run.load_research_data', fake)


def _write_params(directory: Path, **overrides) -> Path:
    data = json.loads(DEFAULT.read_text(encoding='utf-8'))
    data.update(overrides)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'params.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def _train(tmp_path, monkeypatch, seen, **overrides):
    _install(monkeypatch, seen)
    params = _write_params(tmp_path, **overrides)
    out = tmp_path / 'out'
    code = main(['--period', 'train', '--params', str(params), '--out-dir', str(out)])
    return code, out, params


def test_default_params_match_the_space_and_endpoints_are_inclusive():
    params = load_params(DEFAULT)
    assert params.tide_mode == 'both'
    assert params.wave_osc == 'force2'
    assert params.wave_threshold == 0.0
    assert params.params_hash() == ElderParams.from_dict(json.loads(DEFAULT.read_text(encoding='utf-8'))).params_hash()
    space = json.loads(SPACE_PATH.read_text(encoding='utf-8'))
    base = json.loads(DEFAULT.read_text(encoding='utf-8'))

    def check(**overrides):
        data = dict(base)
        data.update(overrides)
        return validate_params(data, space)

    assert check(trail_atr=1.0).trail_atr == 1.0
    assert check(trail_atr=6.0).trail_atr == 6.0
    assert check(t1_share=0.2).t1_share == 0.2
    assert check(t1_share=0.8).t1_share == 0.8
    assert check(ripple_expiry_bars=1).ripple_expiry_bars == 1
    assert check(ripple_expiry_bars=48).ripple_expiry_bars == 48
    assert check(stop_lookback_bars=12).stop_lookback_bars == 12
    assert check(wave_osc='stoch', wave_threshold=5.0).wave_threshold == 5.0
    assert check(wave_osc='stoch', wave_threshold=50.0).wave_threshold == 50.0
    assert check(wave_osc='force2', wave_threshold=2.0).wave_threshold == 2.0
    assert check(wave_osc='elder_ray', wave_threshold=0.0).wave_osc == 'elder_ray'
    for bad in (
        {'trail_atr': 6.1},
        {'trail_atr': 0.9},
        {'t1_share': 0.19},
        {'ripple_expiry_bars': 49},
        {'stop_lookback_bars': 13},
        {'wave_osc': 'stoch', 'wave_threshold': 4.9},
        {'wave_osc': 'stoch', 'wave_threshold': 50.1},
        {'wave_osc': 'force2', 'wave_threshold': 2.1},
        {'wave_osc': 'elder_ray', 'wave_threshold': 2.01},
        {'tide_mode': 'weekly'},
    ):
        try:
            check(**bad)
        except ValueError:
            continue
        raise AssertionError(f'accepted out-of-space params {bad}')


def test_trials_jsonl_is_append_only_and_a_broken_chain_is_refused(tmp_path, monkeypatch):
    seen = []
    code, out, _params = _train(tmp_path, monkeypatch, seen)
    assert code == 0
    assert seen == [TRAIN_END]
    trials = out / 'trials.jsonl'
    first = trials.read_bytes()
    lines = first.splitlines(keepends=True)
    assert len(lines) == 1 and lines[0].endswith(b'\n')
    obj = json.loads(lines[0])
    assert obj['trial_id'] == 1 and obj['prev_sha256'] is None and obj['period'] == 'train'
    assert obj['start'].startswith('2026-01-01')
    assert obj['end'].startswith('2026-07-01')
    assert (out / obj['report']).is_file()
    assert (out / obj['report']).with_suffix('.md').is_file()

    code, out, _params = _train(tmp_path, monkeypatch, seen)
    assert code == 0
    assert all(end <= TRAIN_END for end in seen)
    second = trials.read_bytes()
    assert second.startswith(first)
    lines = second.splitlines(keepends=True)
    assert len(lines) == 2 and lines[0] == first.splitlines(keepends=True)[0]
    nxt = json.loads(lines[1])
    assert nxt['trial_id'] == 2
    assert nxt['prev_sha256'] == hashlib.sha256(lines[0]).hexdigest()

    tampered = dict(nxt)
    tampered['prev_sha256'] = '0' * 64
    broken = lines[0] + (json.dumps(tampered, sort_keys=True, separators=(',', ':')).encode('utf-8') + b'\n')
    trials.write_bytes(broken)
    before = trials.read_bytes()
    assert main(['--period', 'train', '--params', str(_write_params(tmp_path / 'again')), '--out-dir', str(out)]) == 3
    assert trials.read_bytes() == before
    assert len(seen) == 2  # a broken chain is refused before any load


def test_train_refuses_an_end_inside_holdout(tmp_path, monkeypatch):
    seen = []
    _install(monkeypatch, seen)
    params = _write_params(tmp_path)
    out = tmp_path / 'out'
    assert main(['--period', 'train', '--params', str(params), '--out-dir', str(out), '--end', '2026-08-01']) == 2
    assert seen == []
    assert not (out / 'trials.jsonl').exists()


def test_params_outside_the_space_exit_2(tmp_path, monkeypatch):
    seen = []
    _install(monkeypatch, seen)
    params = _write_params(tmp_path, trail_atr=9.0)
    out = tmp_path / 'out'
    assert main(['--period', 'train', '--params', str(params), '--out-dir', str(out)]) == 2
    assert seen == []
    assert not out.exists() or not (out / 'trials.jsonl').exists()


def test_freeze_refuses_to_overwrite(tmp_path, monkeypatch):
    seen = []
    code, out, _params = _train(tmp_path, monkeypatch, seen)
    assert code == 0
    assert main(['--freeze', '--trial', '1', '--out-dir', str(out)]) == 0
    frozen = (out / 'frozen.json').read_bytes()
    stored = json.loads(frozen)
    assert stored['tide_mode'] == 'both'
    assert ElderParams.from_dict(stored).params_hash() == json.loads((out / 'trials.jsonl').read_text().splitlines()[0])['params_hash']
    assert main(['--freeze', '--trial', '1', '--out-dir', str(out)]) == 3
    assert (out / 'frozen.json').read_bytes() == frozen
    assert main(['--freeze', '--trial', '9', '--out-dir', str(out)]) == 3


def test_holdout_refusals_and_the_lock(tmp_path, monkeypatch):
    seen = []
    code, out, params = _train(tmp_path, monkeypatch, seen)
    assert code == 0
    trials_before = (out / 'trials.jsonl').read_bytes()
    holdout = ['--period', 'holdout', '--params', str(params), '--out-dir', str(out), '--end', '2026-08-01']

    assert main(holdout) == 3  # no frozen.json
    assert (out / 'trials.jsonl').read_bytes() == trials_before
    assert not (out / 'holdout.lock').exists()

    other = _write_params(tmp_path / 'other', trail_atr=2.0)
    (out / 'frozen.json').write_text(other.read_text(encoding='utf-8'), encoding='utf-8')
    assert main(['--period', 'holdout', '--params', str(other), '--out-dir', str(out), '--end', '2026-08-01']) == 3
    assert (out / 'trials.jsonl').read_bytes() == trials_before
    (out / 'frozen.json').unlink()

    assert main(['--freeze', '--trial', '1', '--out-dir', str(out)]) == 0
    assert main(['--period', 'holdout', '--params', str(other), '--out-dir', str(out), '--end', '2026-08-01']) == 3
    assert (out / 'trials.jsonl').read_bytes() == trials_before

    seen.clear()
    assert main(holdout) == 0
    assert seen == [TRAIN_END, datetime(2026, 8, 1, tzinfo=UTC)]
    lock = json.loads((out / 'holdout.lock').read_text(encoding='utf-8'))
    trained = json.loads(trials_before.splitlines()[0])
    assert lock['params_hash'] == trained['params_hash']
    assert lock['end'].startswith('2026-08-01')
    report = json.loads(next(out.glob('holdout_*.json')).read_text(encoding='utf-8'))
    assert set(report) >= {'train', 'holdout', 'params_hash'}
    for side in ('train', 'holdout'):
        assert set(report[side]['baselines']) == {'no_trade', 'random_tide'}
    log_lines = (out / 'holdout_log.jsonl').read_bytes().splitlines(keepends=True)
    assert len(log_lines) == 1
    assert json.loads(log_lines[0])['forced'] is False
    assert (out / 'trials.jsonl').read_bytes() == trials_before

    log_before = (out / 'holdout_log.jsonl').read_bytes()
    assert main(holdout) == 3
    assert main(holdout + ['--force']) == 3
    assert main(holdout + ['--force', '--reason', '   ']) == 3
    assert (out / 'holdout_log.jsonl').read_bytes() == log_before
    assert (out / 'trials.jsonl').read_bytes() == trials_before

    seen.clear()
    assert main(holdout + ['--force', '--reason', 'recheck after a bugfix']) == 0
    logged = [json.loads(line) for line in (out / 'holdout_log.jsonl').read_bytes().splitlines()]
    assert len(logged) == 2
    assert logged[1]['forced'] is True
    assert logged[1]['reason'] == 'recheck after a bugfix'
    assert logged[1]['params_hash'] == trained['params_hash']
    assert (out / 'trials.jsonl').read_bytes() == trials_before
    assert all(end <= datetime(2026, 8, 1, tzinfo=UTC) for end in seen)
